"""Selection and execution contracts for the credit-conscious CI cascade."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts import affected_tests as affected
from scripts import ci_tests as ci


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "REPO", tmp_path)
    monkeypatch.setattr(affected, "REPO", tmp_path)
    monkeypatch.setattr(affected, "SRC", tmp_path / "src")
    monkeypatch.setattr(affected, "TESTS", tmp_path / "src/tests")

    def write(name, content=""):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    for name in (*ci.SMOKE, *ci.DOC_TESTS):
        write(name, "def test_contract(): pass\n")
    write("src/agents/leaf.py", "value = 1\n")
    write("src/agents/bridge.py", "from agents import leaf\n")
    write("src/tests/test_bridge.py", "from agents import bridge\n")
    write("src/tests/test_dynamic.py", 'import importlib\nmodule = importlib.import_module("agents.leaf")\n')
    write("src/tests/test_unrelated.py", "def test_other(): pass\n")
    return write


def test_leaf_change_reaches_transitive_and_dynamic_consumers(project):
    plan = ci.make_plan(["src/agents/leaf.py"])
    assert plan["profile"] == "affected"
    assert set(plan["smoke"]) == set(ci.SMOKE)
    assert set(plan["tests"]) == {"src/tests/test_bridge.py", "src/tests/test_dynamic.py"}
    assert "src/tests/test_unrelated.py" not in plan["tests"]
    assert plan["experiment"] is True


def test_transitive_selection_follows_shared_test_helpers_too(project):
    project("src/tests/test_reexport.py", "from tests import test_bridge\n")
    project("src/tests/test_downstream.py", "from tests import test_reexport\n")
    plan = ci.make_plan(["src/agents/leaf.py"])
    assert plan["profile"] == "affected"
    assert "src/tests/test_downstream.py" in plan["tests"]


def test_each_changed_input_must_have_a_known_consumer(project):
    project("src/agents/uncovered.py", "value = 2\n")
    plan = ci.make_plan(["src/agents/leaf.py", "src/agents/uncovered.py"])
    assert plan["profile"] == "full"
    assert "uncovered.py" in plan["reason"]


@pytest.mark.parametrize("name", [
    "src/graph/routing.py", "src/storage/warehouse.py", "src/llm/providers.py",
    "src/config/taxonomy.yaml", "src/tests/nested/conftest.py", "pyproject.toml",
    "src/tests/fixtures/contract/sample.txt", "vendor/new_dependency.py",
    ".circleci/config.yml", "unknown.dat", "src/agents/deleted.py",
    "src/agents/base.py", "src/agents/sorter.py", "src/pipeline/config.py",
    "src/agents/__init__.py", "src/langchain_agents/prompts.py",
])
def test_uncertain_or_shared_impact_requires_full_suite(project, name):
    if name != "src/agents/deleted.py":
        project(name)
    assert ci.make_plan([name])["profile"] == "full"


def test_invalid_python_cannot_be_mistaken_for_no_consumers(project):
    project("src/agents/leaf.py", "def broken(:\n")
    plan = ci.make_plan(["src/agents/leaf.py"])
    assert plan["profile"] == "full"
    assert "cannot analyze" in plan["reason"]


def test_analysis_failure_falls_back_to_full(project, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("broken dependency analyzer")
    monkeypatch.setattr(affected, "select", fail)
    assert ci.make_plan(["src/agents/leaf.py"])["profile"] == "full"


def test_docs_and_test_only_changes_keep_the_cascade_small(project):
    project("docs/testing.md", "# Guide\n")
    docs = ci.make_plan(["docs/testing.md"])
    assert docs["profile"] == "affected"
    assert set(docs["tests"]) == set(ci.DOC_TESTS)
    assert docs["experiment"] is False
    tests = ci.make_plan(["src/tests/test_bridge.py"])
    assert tests["profile"] == "affected"
    assert tests["tests"] == ["src/tests/test_bridge.py"]


def test_wide_impact_and_manual_release_runs_escalate(project):
    for n in range(12):
        project(f"src/tests/test_consumer_{n}.py", "import agents.leaf\n")
    assert ci.make_plan(["src/agents/leaf.py"])["profile"] == "full"
    assert ci.make_plan([], full=True)["profile"] == "full"
    assert ci.make_plan([], branch="release/1.0")["profile"] == "full"


def test_new_graph_contract_joins_mandatory_smoke_when_available(project):
    project("src/tests/test_graph_contracts.py", "def test_handoff(): pass\n")
    assert "src/tests/test_graph_contracts.py" in ci.make_plan([])["smoke"]


def test_partial_and_full_successes_have_different_cache_scopes(project):
    partial, full = ci.make_plan([]), ci.make_plan([], full=True)
    assert ci.cache_key(partial) != ci.cache_key(full)
    changed_reason = {**partial, "reason": "different wording"}
    assert ci.cache_key(partial) == ci.cache_key(changed_reason)


@pytest.mark.parametrize("failure", [1, 5])
def test_failed_or_empty_smoke_stays_red_and_stops_later_work(project, monkeypatch, tmp_path, failure):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return failure
    monkeypatch.setattr(ci.subprocess, "call", run)
    plan = ci.make_plan(["src/agents/leaf.py"])
    result = ci.execute(plan, results=tmp_path / "evidence")
    assert result == failure
    assert len(calls) == 1
    evidence = json.loads((tmp_path / "evidence/execution.json").read_text())
    assert [(p["stage"], p["exit_code"]) for p in evidence] == [("smoke", failure)]


def test_scoped_execution_records_stages_without_duplicate_smoke(project, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 0)
    plan = ci.make_plan(["src/agents/leaf.py", ci.SMOKE[0]])
    assert ci.execute(plan, results=tmp_path / "evidence") == 0
    history = json.loads((tmp_path / "evidence/execution.json").read_text())
    assert [p["stage"] for p in history] == ["smoke", "affected", "experiment"]
    assert ci.SMOKE[0] in calls[0] and ci.SMOKE[0] not in calls[1]
    assert "--mock" in calls[2]


def test_full_execution_includes_branch_coverage_and_test_contexts(project, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 0)
    assert ci.execute(ci.make_plan([], full=True), results=tmp_path / "evidence") == 0
    assert "--cov-branch" in calls[0]
    assert "--cov-context=test" in calls[0]
    assert calls[0].count("src/tests") == 1


def test_unavailable_git_base_executes_full_instead_of_claiming_no_tests(project, monkeypatch, tmp_path):
    def fail(base):
        raise subprocess.CalledProcessError(128, ["git", "diff"])
    monkeypatch.setattr(affected, "changed_files", fail)
    plans = []
    monkeypatch.setattr(ci, "execute", lambda plan, **kw: plans.append(plan) or 0)
    assert ci.main(["--plan", str(tmp_path / "out/plan.json"), "--run"]) == 0
    assert plans[0]["profile"] == "full"
    assert "cannot establish" in plans[0]["reason"]


def test_git_rename_reports_old_and_new_inputs(project, tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    git("branch", "base")
    git("mv", "src/agents/leaf.py", "src/agents/renamed.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "rename")
    changed = affected.changed_files("base")
    assert "src/agents/leaf.py" in changed and "src/agents/renamed.py" in changed
    assert ci.make_plan(changed)["profile"] == "full"


def test_literal_subprocess_script_paths_reach_transitive_consumers(project):
    project("src/scripts/check.py", "value = 1\n")
    project("src/pipeline/launcher.py", 'script = "src/scripts/check.py"\n')
    project("src/tests/test_launcher.py", "from pipeline import launcher\n")
    project("src/tests/test_cli.py", 'script = "check.py"\n')
    plan = ci.make_plan(["src/scripts/check.py"])
    assert plan["profile"] == "affected"
    assert set(plan["tests"]) == {"src/tests/test_launcher.py", "src/tests/test_cli.py"}


def test_successful_baseline_keeps_earlier_commits_in_a_push_in_scope(project, tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)
    def commit(message):
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    git("init", "-q")
    commit("last successful build")
    git("branch", "last-success")
    project("src/storage/warehouse.py", "value = 2\n")
    commit("high risk change")
    project("docs/testing.md", "# Small followup\n")
    commit("documentation followup")
    changed = affected.changed_files("last-success")
    assert "src/storage/warehouse.py" in changed
    assert ci.make_plan(changed, branch="main")["profile"] == "full"


def test_plan_only_cli_persists_a_readable_scope_without_running_tests(project, monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("plan-only request attempted to execute tests")
    monkeypatch.setattr(ci, "execute", unexpected)
    target = tmp_path / "evidence/plan.json"
    assert ci.main(["--files", "src/agents/leaf.py", "--plan", str(target)]) == 0
    saved = json.loads(target.read_text())
    assert saved["profile"] == "affected"
    assert saved["tests"] == ["src/tests/test_bridge.py", "src/tests/test_dynamic.py"]
    assert (target.parent / "plan-key").read_text().strip() == ci.cache_key(saved)


def test_docs_skip_unrelated_experiment_but_runtime_experiment_failure_is_red(project, monkeypatch, tmp_path):
    project("docs/testing.md", "# Guide\n")
    calls = []
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
    docs = ci.make_plan(["docs/testing.md"])
    assert ci.execute(docs, results=tmp_path / "docs-evidence") == 0
    assert len(calls) == 2  # smoke + docs; no unrelated experiment
    codes = iter([0, 0, 2])
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kw: next(codes))
    assert ci.execute(ci.make_plan(["src/agents/leaf.py"]), results=tmp_path / "runtime-evidence") == 2
    history = json.loads((tmp_path / "runtime-evidence/execution.json").read_text())
    assert history[-1]["stage"] == "experiment" and history[-1]["exit_code"] == 2


def test_missing_smoke_and_untrusted_paths_force_full_validation(project, tmp_path):
    assert ci.make_plan(["../outside.py"])["profile"] == "full"
    (tmp_path / ci.SMOKE[0]).unlink()
    missing = ci.make_plan([])
    assert missing["profile"] == "full"
    assert missing["reason"] == "mandatory smoke test missing"


def test_broad_change_is_full_even_when_each_leaf_is_mapped(project):
    paths = []
    for n in range(8):
        paths.append(f"src/agents/leaf_{n}.py")
        project(paths[-1], f"value = {n}\n")
        project(f"src/tests/test_leaf_{n}.py", f"import agents.leaf_{n}\n")
    assert "8 or more" in ci.make_plan(paths)["reason"]


def test_raw_selector_refuses_to_claim_a_pass_for_an_empty_selection(project):
    assert affected.main(["--files", "missing-input.dat", "--run"]) == 5
