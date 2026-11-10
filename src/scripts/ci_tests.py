"""Plan and run a tiered, conservative test cascade; unknown impact means a full run.

Tiers (the plan's ``profile``; the CI log prints the tier and the reason first):

* 0 ``docs``     documentation / metadata only: the doc tests, no smoke, no experiment gate.
* 1 ``tests``    test files only: smoke, then the changed tests (and the tests that import them).
* 2 ``affected`` production code or mapped data: smoke, then every transitively affected test.
* 3 ``full``     anything global, deleted, unmapped or unanalyzable; release branches; the
                 weekly backstop; an explicit request; a change that spans most of the suite.

Local: PYTHONPATH=src python src/scripts/ci_tests.py --files src/agents/intake.py --run
CI: plan before installation, then re-plan with --run against the same diff base.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

from scripts import affected_tests

REPO = Path(__file__).resolve().parents[2]
PLAN_VERSION = 2
TIERS = {"docs": 0, "tests": 1, "affected": 2, "full": 3}
SMOKE = (
    "src/tests/test_routing.py",
    "src/tests/test_guards.py",
    "src/tests/test_graph_node_smoke.py",
    "src/tests/test_pipeline_e2e.py",
)

# --- Global contracts: a change here can alter every test, so it always runs the full suite. ---
# Everything that used to be a blanket "shared prefix" (graph, schemas, storage, llm,
# observability, dojo_source, langchain_agents, notebooks, deploy, contract) is NOT here any more:
# Python in those packages is selected through the import graph, and their data files through
# the consumer rules below.
GLOBAL_FILES = {
    "pyproject.toml",
    "src/config/taxonomy.yaml",          # single source of truth, read by nearly every test
    "src/scripts/ci_tests.py", "src/scripts/affected_tests.py",   # the planner itself
    "src/agents/base.py", "src/agents/sorter.py",
    "src/pipeline/config.py", "src/pipeline/env.py", "src/pipeline/bins.py", "src/pipeline/guards.py",
}
# Any directory. The pytest and coverage config files do not exist today; listing them by name
# makes adding one (which reconfigures every test run) a full-suite change.
GLOBAL_NAMES = {"conftest.py", "__init__.py", "pytest.ini", "tox.ini", "setup.cfg", ".coveragerc"}
GLOBAL_PREFIXES = (".circleci/", ".github/workflows/", "vendor/")   # CI config, fallback snapshot
# Shared test data that tests load through helpers and fixtures, so no text search can find
# its consumers.
SHARED_FIXTURES = "src/tests/fixtures/"

# --- Documentation and metadata: cannot change program behaviour. ---
DOC_ASSET_SUFFIXES = {".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}   # only under docs/
DOC_FILES = {".gitbook.yaml", "gitbook-docs.yaml", "docs/.gitbook.yaml", "LICENSE"}
DOC_TESTS = ("src/tests/test_docs_truth.py", "src/tests/test_landing.py")
CHANGELOG_INPUTS = ("CHANGELOG.md", "docs/changelog/")

# --- Data-driven inputs: prefix -> tests that consume them, in addition to every test whose
# text names the file, its directory, or (for Python outside src/) its module. ---
DATA_CONSUMERS = {
    "notebooks/": ("src/tests/test_notebook_suite.py",),
    "landing/": ("src/tests/test_landing.py",),
    "contract/": ("src/tests/test_contract_export.py",),
    "src/llm/frozen_v1/": ("src/tests/test_frozen_v1_prompts.py", "src/tests/test_prompt_mutation_lineage.py"),
    "docs/superpowers/baselines/": ("src/tests/test_experiment_gate_script.py", "src/tests/test_autoresearch_gate.py"),
}
# Inputs of the hermetic experiment gate besides production code under src/.
EXPERIMENT_INPUTS = ("docs/superpowers/baselines/", "experiments/")

# The pipeline-diagnostics workbench is a nested package with its own suite.
# Explicit src/tests runs do not reach it, so name both roots. Every path
# here is resolved relative to REPO (the planner stays testable in a tmp tree).
NESTED_ROOT = "tools/pipeline-diagnostics"
NESTED_TESTS = f"{NESTED_ROOT}/tests"
# Parent inputs that the nested suite verifies but the src import graph cannot
# see (the nested tests import the exporter outside `src.tests`). A change here
# must select the nested suite, not only its root consumers.
NESTED_PARENT_INPUTS = {"src/scripts/export_contract.py"}

BROAD_PRODUCTION_FILES = 8    # this many changed production files: stop reasoning file by file
BROAD_TEST_SHARE = 0.65       # a selection this large is cheaper to run as the full suite
# Soft per-tier wall-clock budgets (seconds) on the CI `small` executor. They are reported, never
# enforced: a run over budget prints a warning so the tier boundaries can be re-tuned from data.
TIER_BUDGET_SECONDS = {"docs": 120, "tests": 300, "affected": 480, "full": 900}


def nested_tests() -> list[str]:
    """Test files of the nested workbench; empty until that package lands."""
    root = REPO / NESTED_TESTS
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(REPO)) for p in root.rglob("test_*.py"))


def _is_doc(name: str) -> bool:
    """True for documentation or repository metadata that no program reads at run time."""
    path = PurePosixPath(name)
    suffix = path.suffix.lower()
    if name in DOC_FILES:
        return True
    if name.startswith("src/"):
        # Markdown under src/ is runtime data (agent skills, prompt candidates) unless a README.
        return path.name == "README.md"
    if suffix in {".md", ".rst"}:
        return True
    return name.startswith("docs/") and suffix in DOC_ASSET_SUFFIXES


def _mention_patterns(name: str) -> list[re.Pattern[str]]:
    """Patterns that show a test's source names ``name``: the path, the file name, its
    directories (``deploy/``, ``"litellm"``) and, for Python outside src/, the module name."""
    path = PurePosixPath(name)
    literals = {name, path.name}
    if path.parent.name and path.parent.name not in {"src", "docs", "tools"}:
        literals.update({f'"{path.parent.name}"', f"'{path.parent.name}'"})
    found = [re.compile(re.escape(text)) for text in literals if text]
    # A directory counts when a test names it as a whole (``deploy/``, ``skills/sorter``), not
    # when it is only the prefix of a sibling's longer path (``deploy/litellm/config.yaml``
    # says nothing about ``deploy/orphan.yaml``). Trailing parts of the file's own directory
    # (two or more components) count too, since tests often build paths from a package root.
    dirs = {p.as_posix() for p in path.parents if p.as_posix() not in {".", "src", "docs", "tools"}}
    parts = path.parent.parts
    dirs.update("/".join(parts[i:]) for i in range(1, len(parts) - 1))
    for d in dirs:
        slash = "/?" if "/" in d else "/"   # a bare one-word directory is too common a word
        found.append(re.compile(rf"(?<![\w.-]){re.escape(d)}{slash}(?![\w./-])"))
    if path.suffix == ".py":
        found.append(re.compile(rf"\b{re.escape(path.stem)}\b"))
    return found


def _deleted_test_is_orphaned(name: str, texts: dict[str, str]) -> bool:
    """A removed ``test_*.py`` is harmless when no remaining test source mentions its module."""
    path = PurePosixPath(name)
    if not (path.name.startswith("test_") and path.suffix == ".py"):
        return False
    return not any(path.stem in text for text in texts.values())


def make_plan(changed: list[str], *, full: bool = False, branch: str = "", coverage: bool = False) -> dict:
    """Map a set of changed paths to a tier and the tests, checks and gates that tier runs.

    Anything the planner cannot place with confidence escalates to the full suite, and every
    escalation trigger is recorded in ``escalations`` (``reason`` is the first one).
    """
    root_tests = sorted(str(p.relative_to(REPO)) for p in (REPO / "src/tests").rglob("test_*.py"))
    nested = nested_tests()
    all_tests = sorted([*root_tests, *nested])
    smoke = [*SMOKE]
    # PR #180 supplies stronger compiled-graph contracts; include them as soon
    # as they land, without making this CI change depend on that PR's branch.
    if (REPO / "src/tests/test_graph_contracts.py").is_file():
        smoke.append("src/tests/test_graph_contracts.py")
    escalations: list[str] = []

    def escalate(why: str) -> None:
        """Record a reason to run the full suite (the first one becomes the headline)."""
        if why not in escalations:
            escalations.append(why)

    if full:
        escalate("explicit full-suite request (also used by the weekly backstop)")
    if branch.startswith("release/"):
        escalate("release branch backstop")
    if any(not (REPO / path).is_file() for path in smoke):
        escalate("mandatory smoke test missing")

    texts: dict[str, str] = {}

    def test_texts() -> dict[str, str]:
        """Source of every test file, read once and only when a mention search needs it."""
        if not texts:
            for rel in all_tests:
                texts[rel] = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        return texts

    selected: set[str] = set()
    kinds: set[str] = set()          # what the change is made of: docs, test, code, data, nested
    code_inputs: list[str] = []      # src Python and helpers analyzed through the import graph
    data_inputs: list[str] = []      # data and non-src Python resolved through consumers
    for name in sorted(set(changed)):
        path = Path(name)
        exists = (REPO / path).is_file()
        if path.is_absolute() or ".." in path.parts:
            escalate(f"unrecognized path: {name}")
        elif name.startswith(f"{NESTED_ROOT}/"):
            kinds.add("nested")
            if path.suffix.lower() in {".md", ".rst"}:
                selected.update(p for p in DOC_TESTS if (REPO / p).is_file())
            if not exists:
                escalate(f"deleted, renamed, or missing input: {name}")
            elif not nested:
                escalate(f"nested tool change without a discovered test root: {name}")
            else:
                # Conservative explicit mapping: the nested tool's own suite is
                # the only test consumer of its backend, packaging, fixtures and
                # test helpers. A changed test module may be imported by other
                # nested tests, so select the whole nested suite rather than one
                # file and risk missing the dependent regression.
                selected.update(nested)
        elif name in GLOBAL_FILES or path.name in GLOBAL_NAMES or name.startswith(GLOBAL_PREFIXES):
            escalate(f"global contract changed: {name}")
        elif not exists:
            if _deleted_test_is_orphaned(name, test_texts()):
                kinds.add("test")        # nothing left refers to it, so nothing can break
            else:
                escalate(f"deleted, renamed, or missing input: {name}")
        elif name.startswith(SHARED_FIXTURES):
            escalate(f"shared fixture changed: {name}")
        elif _is_doc(name):
            kinds.add("docs")
            selected.update(p for p in DOC_TESTS if (REPO / p).is_file())
            data_inputs.append(name)     # tests that name the file (README.md, CHANGELOG.md ...) join
        elif name.startswith("src/") and name.endswith(".py"):
            code_inputs.append(name)
            is_test = name.startswith("src/tests/") and path.name.startswith("test_")
            kinds.add("test" if is_test else "code")
            if name in NESTED_PARENT_INPUTS:
                selected.update(nested)  # the nested parity test imports the exporter directly
        else:
            kinds.add("data")
            data_inputs.append(name)
    production = [p for p in changed if p.startswith("src/") and not p.startswith("src/tests/")]
    if len(production) >= BROAD_PRODUCTION_FILES:
        escalate(f"broad production change ({BROAD_PRODUCTION_FILES} or more files)")

    index = None
    if not escalations:
        try:
            index = affected_tests.Index()
        except Exception:
            escalate("dependency analysis failed")

    def data_consumers(name: str) -> set[str]:
        """Tests that read a non-Python or non-src input: the explicit map plus every test that
        names the file, its directory or its module. Empty means unmapped, which escalates."""
        found = set(affected_tests.select([name], depth=-1, index=index))
        for prefix, tests in DATA_CONSUMERS.items():
            if name.startswith(prefix):
                found.update(t for t in tests if (REPO / t).is_file())
                if prefix == "contract/":
                    found.update(nested)
        patterns = _mention_patterns(name)
        found.update(rel for rel, text in test_texts().items() if any(p.search(text) for p in patterns))
        return found

    if not escalations:
        # Analyze each changed input separately: an unmapped file must not
        # be hidden by a different file that does have known consumers.
        try:
            for name in code_inputs:
                try:
                    ast.parse((REPO / name).read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeError, OSError):
                    escalate(f"cannot analyze changed Python: {name}")
                    continue
                consumers = affected_tests.select([name], depth=-1, index=index)
                if not consumers:
                    escalate(f"no known test consumer: {name}")
                selected.update(consumers)
            for name in data_inputs:
                if _is_doc(name):
                    selected.update(affected_tests.select([name], depth=-1, index=index))
                    continue
                consumers = data_consumers(name)
                if not consumers:
                    escalate(f"no known test consumer: {name}")
                selected.update(consumers)
        except Exception:
            escalate("dependency analysis failed")
    if len(selected) >= max(1, len(all_tests) * BROAD_TEST_SHARE):
        escalate(f"affected area spans at least {BROAD_TEST_SHARE:.0%} of test files")

    if escalations:
        profile = "full"
    elif kinds == {"docs"}:
        profile = "docs"
    elif kinds <= {"docs", "test"}:
        profile = "tests"      # includes an empty change set: the mandatory smoke only
    else:
        profile = "affected"
    experiment = bool(escalations or production or any(n.startswith(EXPERIMENT_INPUTS) for n in changed))
    if profile in {"docs", "tests"}:
        experiment = False      # nothing the pipeline runs has changed
    if profile == "docs" and not selected:
        profile, experiment = "full", True
        escalations.append("documentation change but no documentation tests available")
    checks = []
    if any(n.startswith(CHANGELOG_INPUTS) for n in changed) or profile == "full":
        checks.append({"name": "changelog-sync", "cmd": ["src/scripts/sync_gitbook_changelog.py", "--check"]})
    run_smoke = profile in {"tests", "affected"}
    return {
        "version": PLAN_VERSION, "profile": profile, "tier": TIERS[profile],
        "reason": escalations[0] if escalations else _tier_reason(profile, bool(kinds)),
        "escalations": escalations,
        "changed": sorted(set(changed)),
        "smoke": smoke if run_smoke else [],
        "tests": (["src/tests", *([NESTED_TESTS] if nested else [])] if profile == "full"
                  else sorted(selected - set(smoke if run_smoke else ()))),
        "checks": checks,
        "experiment": experiment,
        "coverage": bool(coverage and profile == "full"),
        "budget_seconds": TIER_BUDGET_SECONDS[profile],
        "total_test_files": len(all_tests),
        "selected_test_files": (len(all_tests) if profile == "full"
                                else len(set(smoke if run_smoke else ()) | selected)),
    }


def _tier_reason(profile: str, has_changes: bool) -> str:
    """Why a plan below tier 3 is safe at its tier."""
    if profile == "tests" and not has_changes:
        return "no changed inputs: mandatory smoke only"
    return {
        "docs": "documentation and metadata only: documentation checks, no pipeline run",
        "tests": "test files only: mandatory smoke, then the changed tests and their importers",
        "affected": "mandatory smoke followed by transitively affected tests",
    }[profile]


def cache_key(plan: dict) -> str:
    """A scoped pass must never satisfy a full-suite cache lookup."""
    fields = {k: plan[k] for k in ("version", "profile", "smoke", "tests", "checks", "experiment")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def banner(plan: dict) -> str:
    """The tier and why, as the first thing a reader of the CI log sees."""
    escalated = f" ({len(plan['escalations'])} triggers)" if len(plan["escalations"]) > 1 else ""
    rule = "=" * 78
    return "\n".join([
        rule,
        f"CI TEST TIER {plan['tier']} ({plan['profile']}): {plan['reason']}{escalated}",
        f"  changed files: {len(plan['changed'])} | test files: {plan['selected_test_files']} of "
        f"{plan['total_test_files']} | experiment gate: {'yes' if plan['experiment'] else 'no'} | "
        f"coverage: {'yes' if plan['coverage'] else 'no'} | budget: {plan['budget_seconds']}s",
        rule,
    ])


def _env() -> dict:
    """Environment for scripts run from the repo: ``src`` first on PYTHONPATH."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def execute(plan: dict, *, results: Path) -> int:
    """Run the planned stages in order, stop at the first failure, and report time per stage."""
    results.mkdir(parents=True, exist_ok=True)
    print(banner(plan), flush=True)
    phases = []
    if plan["profile"] == "full":
        phases.append(("full", plan["tests"]))
    else:
        if plan["smoke"]:
            phases.append(("smoke", plan["smoke"]))
        if plan["tests"]:
            phases.append((plan["profile"], plan["tests"]))
    history: list[dict] = []

    def record(stage: str, code: int, started: float) -> None:
        """Append a stage result and persist the history after every stage."""
        history.append({"stage": stage, "exit_code": code, "seconds": round(time.monotonic() - started, 3)})
        (results / "execution.json").write_text(json.dumps(history, indent=2) + "\n")

    def finish(code: int) -> int:
        """Print the stage table against the tier budget, then return the exit code."""
        total = sum(h["seconds"] for h in history)
        lines = [f"  {h['stage']:<12} exit {h['exit_code']}  {h['seconds']:>8.1f}s" for h in history]
        over = f"  OVER the {plan['budget_seconds']}s tier budget: re-tune the tier or split the slow tests" \
            if total > plan["budget_seconds"] else ""
        print("\n".join([f"CI TEST TIER {plan['tier']} ({plan['profile']}) stages:", *lines,
                         f"  {'total':<12}         {total:>8.1f}s of {plan['budget_seconds']}s budget", *([over] if over else [])]),
              flush=True)
        return code

    for name, paths in phases:
        cmd = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider",
               "--durations=20", f"--junitxml={results / (name + '.xml')}"]
        if name == "full" and plan["coverage"]:
            cmd += ["--cov=src", "--cov-branch", "--cov-context=test",
                    f"--cov-report=xml:{results / 'coverage.xml'}", "--cov-report=term:skip-covered"]
        started = time.monotonic()
        code = subprocess.call(cmd, cwd=REPO)
        record(name, code, started)
        if code:
            return finish(code)  # Never hide a failed narrow run behind a later pass.
    for check in plan["checks"]:
        started = time.monotonic()
        code = subprocess.call([sys.executable, *check["cmd"]], cwd=REPO, env=_env())
        record(check["name"], code, started)
        if code:
            return finish(code)
    if plan["experiment"]:
        cmd = [sys.executable, "src/scripts/run_experiment.py", "--dataset", "mailroom-fixtures",
               "--mock", "--baseline", "docs/superpowers/baselines/experiment-baseline.json"]
        started = time.monotonic()
        code = subprocess.call(cmd, cwd=REPO, env=_env())
        record("experiment", code, started)
        return finish(code)
    return finish(0)


def main(argv: list[str] | None = None) -> int:
    """CLI: plan from git (or ``--files``), write ``test-plan.json`` and ``plan-key``, optionally run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--branch", default="")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--coverage", action="store_true",
                        help="on a full run, collect branch coverage with per-test contexts (weekly backstop)")
    parser.add_argument("--plan", type=Path, default=Path("test-results/test-plan.json"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = args.files if args.files is not None else affected_tests.changed_files(args.base)
        plan = make_plan(changed, full=args.full, branch=args.branch, coverage=args.coverage)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, AssertionError) as error:
        plan = make_plan([], full=True, coverage=args.coverage)
        plan["reason"] = f"cannot establish a safe selection: {type(error).__name__}"
        plan["escalations"] = [plan["reason"]]
    args.plan.parent.mkdir(parents=True, exist_ok=True)
    args.plan.write_text(json.dumps(plan, indent=2) + "\n")
    (args.plan.parent / "plan-key").write_text(cache_key(plan) + "\n")
    print(banner(plan), flush=True)
    print(json.dumps(plan, indent=2), flush=True)
    return execute(plan, results=args.plan.parent) if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
