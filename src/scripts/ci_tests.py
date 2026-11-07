"""Plan and run conservative test cascades; unknown impact means a full run.

Local: PYTHONPATH=src python src/scripts/ci_tests.py --files src/agents/intake.py --run
CI: plan before installation, then re-plan with --run against the same diff base.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts import affected_tests

REPO = Path(__file__).resolve().parents[2]
SMOKE = (
    "src/tests/test_routing.py",
    "src/tests/test_guards.py",
    "src/tests/test_graph_node_smoke.py",
    "src/tests/test_pipeline_e2e.py",
)
SHARED = ("src/graph/", "src/schemas/", "src/storage/", "src/llm/",
          "src/observability/", "src/config/", "src/dojo_source/", "src/langchain_agents/", "vendor/",
          "deploy/", "contract/", "notebooks/", ".circleci/", ".github/workflows/")
GLOBAL_FILES = {"pyproject.toml", "Dockerfile", "pytest.ini", "tox.ini", "setup.cfg",
                ".coveragerc", "src/scripts/ci_tests.py", "src/scripts/affected_tests.py",
                "src/agents/base.py", "src/agents/sorter.py", "src/pipeline/config.py",
                "src/pipeline/env.py", "src/pipeline/bins.py", "src/pipeline/guards.py"}
DOC_TESTS = ("src/tests/test_docs_truth.py", "src/tests/test_landing.py")


def make_plan(changed: list[str], *, full: bool = False, branch: str = "") -> dict:
    all_tests = sorted(str(p.relative_to(REPO)) for p in (REPO / "src/tests").rglob("test_*.py"))
    smoke = [*SMOKE]
    # PR #180 supplies stronger compiled-graph contracts; include them as soon
    # as they land, without making this CI change depend on that PR's branch.
    if (REPO / "src/tests/test_graph_contracts.py").is_file():
        smoke.append("src/tests/test_graph_contracts.py")
    reason = ""
    if full:
        reason = "explicit full-suite request (also used by the weekly backstop)"
    elif branch.startswith("release/"):
        reason = "release branch backstop"
    elif any(not (REPO / path).is_file() for path in smoke):
        reason = "mandatory smoke test missing"
    selected: set[str] = set()
    for name in changed:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            reason = reason or f"unrecognized path: {name}"
        elif name in GLOBAL_FILES or name.startswith(SHARED) or path.name in {"conftest.py", "__init__.py"}:
            reason = reason or f"shared contract/configuration changed: {name}"
        elif not (REPO / path).is_file():
            reason = reason or f"deleted, renamed, or missing input: {name}"
        elif name.startswith("src/tests/fixtures/"):
            reason = reason or f"shared fixture changed: {name}"
        elif name.endswith(".py") and name.startswith("src/"):
            try:
                ast.parse((REPO / path).read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeError, OSError):
                reason = reason or f"cannot analyze changed Python: {name}"
        elif path.suffix.lower() in {".md", ".rst"}:
            selected.update(p for p in DOC_TESTS if (REPO / p).is_file())
        else:
            reason = reason or f"unmapped input: {name}"
    production = [p for p in changed if p.startswith("src/") and not p.startswith("src/tests/")]
    if len(production) >= 8:
        reason = reason or "broad production change (8 or more files)"
    if not reason:
        try:
            # Analyze each changed input separately: an unmapped file must not
            # be hidden by a different file that does have known consumers.
            for name in changed:
                consumers = affected_tests.select([name], depth=-1)
                if name.endswith(".py") and not consumers:
                    reason = f"no known test consumer: {name}"
                    break
                selected.update(consumers)
        except Exception:
            reason = "dependency analysis failed"
    if len(selected) >= max(1, len(all_tests) * 0.65):
        reason = reason or "affected area spans at least 65% of test files"
    profile = "full" if reason else "affected"
    return {
        "version": 1, "profile": profile,
        "reason": reason or "mandatory smoke followed by transitive affected tests",
        "changed": sorted(set(changed)),
        "smoke": [] if reason else smoke,
        "tests": ["src/tests"] if reason else sorted(selected - set(smoke)),
        "experiment": bool(reason or production),
        "total_test_files": len(all_tests),
        "selected_test_files": len(all_tests) if reason else len(set(smoke) | selected),
    }


def cache_key(plan: dict) -> str:
    """A scoped pass must never satisfy a full-suite cache lookup."""
    fields = {k: plan[k] for k in ("version", "profile", "smoke", "tests", "experiment")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def execute(plan: dict, *, results: Path) -> int:
    results.mkdir(parents=True, exist_ok=True)
    phases = []
    if plan["profile"] == "full":
        phases.append(("full", plan["tests"]))
    else:
        phases.append(("smoke", plan["smoke"]))
        if plan["tests"]:
            phases.append(("affected", plan["tests"]))
    history = []
    for name, paths in phases:
        cmd = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider",
               "--durations=20", f"--junitxml={results / (name + '.xml')}"]
        if name == "full":
            cmd += ["--cov=src", "--cov-branch", "--cov-context=test",
                    f"--cov-report=xml:{results / 'coverage.xml'}", "--cov-report=term:skip-covered"]
        started = time.monotonic()
        code = subprocess.call(cmd, cwd=REPO)
        history.append({"stage": name, "exit_code": code, "seconds": round(time.monotonic() - started, 3)})
        (results / "execution.json").write_text(json.dumps(history, indent=2) + "\n")
        if code:
            return code  # Never hide a failed narrow run behind a later pass.
    if plan["experiment"]:
        cmd = [sys.executable, "src/scripts/run_experiment.py", "--dataset", "mailroom-fixtures",
               "--mock", "--baseline", "docs/superpowers/baselines/experiment-baseline.json"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
        started = time.monotonic()
        code = subprocess.call(cmd, cwd=REPO, env=env)
        history.append({"stage": "experiment", "exit_code": code, "seconds": round(time.monotonic() - started, 3)})
        (results / "execution.json").write_text(json.dumps(history, indent=2) + "\n")
        return code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--branch", default="")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--plan", type=Path, default=Path("test-results/test-plan.json"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = args.files if args.files is not None else affected_tests.changed_files(args.base)
        plan = make_plan(changed, full=args.full, branch=args.branch)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, AssertionError) as error:
        plan = make_plan([], full=True)
        plan["reason"] = f"cannot establish a safe selection: {type(error).__name__}"
    args.plan.parent.mkdir(parents=True, exist_ok=True)
    args.plan.write_text(json.dumps(plan, indent=2) + "\n")
    (args.plan.parent / "plan-key").write_text(cache_key(plan) + "\n")
    print(json.dumps(plan, indent=2), flush=True)
    return execute(plan, results=args.plan.parent) if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
