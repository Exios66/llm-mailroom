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
    "src/config/taxonomy.yaml", "src/tests/nested/conftest.py", "src/tests/conftest.py", "pyproject.toml",
    "pytest.ini", "src/scripts/ci_tests.py", "src/scripts/affected_tests.py",
    "src/agents/base.py", "src/agents/sorter.py", "src/pipeline/config.py", "src/pipeline/bins.py",
    "src/agents/__init__.py", ".circleci/config.yml", ".github/workflows/bump.yml", "vendor/new_dependency.py",
])
def test_global_contracts_require_the_full_suite(project, name):
    project(name)
    plan = ci.make_plan([name])
    assert plan["profile"] == "full" and plan["tier"] == 3
    assert plan["reason"] == f"global contract changed: {name}"


@pytest.mark.parametrize("name", [
    "src/tests/fixtures/contract/sample.txt",   # loaded through helpers; no text search finds its users
    "unknown.dat",                               # unmapped file type
    "src/agents/deleted.py",                     # missing on disk: deleted or renamed
])
def test_uncertain_inputs_require_the_full_suite(project, name):
    if name != "src/agents/deleted.py":
        project(name)
    assert ci.make_plan([name])["profile"] == "full"


@pytest.mark.parametrize("name", [
    "src/graph/build_graph.py", "src/schemas/manifest.py", "src/storage/catalog.py", "src/llm/providers.py",
    "src/observability/scores.py", "src/dojo_source/loader.py", "src/langchain_agents/prompts.py",
])
def test_formerly_shared_packages_use_the_import_graph_when_they_have_a_consumer(project, name):
    """graph/, schemas/, storage/, llm/, observability/ ... were blanket full-suite triggers."""
    module = name.removeprefix("src/").removesuffix(".py").replace("/", ".")
    project(name, "value = 1\n")
    project(f"src/tests/test_consumer_{module}.py", f"import {module}\n")
    plan = ci.make_plan([name])
    assert plan["profile"] == "affected" and plan["tier"] == 2
    assert plan["tests"] == [f"src/tests/test_consumer_{module}.py"]
    assert plan["experiment"] is True


def test_formerly_shared_package_without_a_consumer_is_still_full(project):
    project("src/storage/orphan.py", "value = 1\n")
    plan = ci.make_plan(["src/storage/orphan.py"])
    assert plan["profile"] == "full"
    assert plan["reason"] == "no known test consumer: src/storage/orphan.py"


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


def test_docs_only_change_is_tier_0_without_smoke_or_experiment(project):
    project("docs/testing.md", "# Guide\n")
    plan = ci.make_plan(["docs/testing.md"])
    assert (plan["profile"], plan["tier"]) == ("docs", 0)
    assert plan["smoke"] == [] and set(plan["tests"]) == set(ci.DOC_TESTS)
    assert plan["experiment"] is False and plan["checks"] == []
    assert plan["selected_test_files"] == len(ci.DOC_TESTS)


def test_test_only_change_is_tier_1_smoke_plus_the_changed_test(project):
    plan = ci.make_plan(["src/tests/test_bridge.py"])
    assert (plan["profile"], plan["tier"]) == ("tests", 1)
    assert set(plan["smoke"]) == set(ci.SMOKE)
    assert plan["tests"] == ["src/tests/test_bridge.py"]
    assert plan["experiment"] is False


def test_a_changed_test_also_selects_the_tests_that_import_it(project):
    project("src/tests/test_reexport.py", "from tests import test_bridge\n")
    plan = ci.make_plan(["src/tests/test_bridge.py"])
    assert plan["profile"] == "tests"
    assert plan["tests"] == ["src/tests/test_bridge.py", "src/tests/test_reexport.py"]


def test_a_shared_test_helper_escalates_to_tier_2_and_a_conftest_to_full(project):
    project("src/tests/helpers.py", "value = 1\n")
    project("src/tests/test_uses_helper.py", "from tests import helpers\n")
    helper = ci.make_plan(["src/tests/helpers.py"])
    assert (helper["profile"], helper["tests"]) == ("affected", ["src/tests/test_uses_helper.py"])
    assert helper["experiment"] is False  # no production code changed
    assert ci.make_plan(["src/tests/conftest.py"])["profile"] == "full"


def test_deleting_a_test_nothing_refers_to_stays_tier_1_but_a_referenced_one_is_full(project):
    gone = ci.make_plan(["src/tests/test_removed.py"])
    assert gone["profile"] == "tests" and gone["tests"] == []
    project("src/tests/test_mentions.py", "from tests import test_removed\n")
    referenced = ci.make_plan(["src/tests/test_removed.py"])
    assert referenced["profile"] == "full" and "deleted" in referenced["reason"]


def test_an_empty_diff_runs_the_smoke_only(project):
    plan = ci.make_plan([])
    assert plan["profile"] == "tests" and plan["tests"] == [] and set(plan["smoke"]) == set(ci.SMOKE)
    assert plan["reason"] == "no changed inputs: mandatory smoke only"


@pytest.mark.parametrize("name,expected", [
    ("docs/guide.md", True), ("README.md", True), ("CHANGELOG.md", True), (".claude/rules/x.md", True),
    ("docs/assets/diagram.png", True), (".gitbook.yaml", True), ("src/agents/README.md", True),
    ("src/langchain_agents/skills/sorter/calibration.md", False),   # loaded at run time
    ("landing/assets/mascot/fumi.png", False), ("docs/reports/eval.json", False), ("deploy/compose.yml", False),
])
def test_what_counts_as_documentation(name, expected):
    assert ci._is_doc(name) is expected


def test_runtime_markdown_under_src_is_data_not_documentation(project):
    project("src/langchain_agents/skills/sorter/calibration.md", "# skill\n")
    plan = ci.make_plan(["src/langchain_agents/skills/sorter/calibration.md"])
    assert plan["profile"] == "full" and "no known test consumer" in plan["reason"]
    project("src/tests/test_skills.py", 'text = "skills/sorter"\n')
    assert ci.make_plan(["src/langchain_agents/skills/sorter/calibration.md"])["tests"] == ["src/tests/test_skills.py"]


def test_docs_plan_without_any_documentation_test_runs_everything(project, tmp_path):
    for name in ci.DOC_TESTS:
        (tmp_path / name).unlink()
    project("docs/testing.md", "# Guide\n")
    plan = ci.make_plan(["docs/testing.md"])
    assert plan["profile"] == "full" and "no documentation tests" in plan["reason"]


def test_changelog_change_adds_the_sync_check_in_every_tier(project):
    project("CHANGELOG.md", "# Changes\n")
    project("docs/testing.md", "# Guide\n")
    docs = ci.make_plan(["CHANGELOG.md"])
    assert docs["profile"] == "docs"
    assert [c["name"] for c in docs["checks"]] == ["changelog-sync"]
    mixed = ci.make_plan(["CHANGELOG.md", "src/agents/leaf.py"])
    assert mixed["profile"] == "affected" and [c["name"] for c in mixed["checks"]] == ["changelog-sync"]
    assert ci.make_plan(["docs/testing.md"])["checks"] == []


def test_nested_documentation_is_not_tier_0(project):
    nested_tool(project)
    project("tools/pipeline-diagnostics/README.md", "# Native diagnostics\n")
    assert ci.make_plan(["tools/pipeline-diagnostics/README.md"])["profile"] == "affected"


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


def test_full_execution_collects_coverage_only_when_asked(project, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 0)
    assert ci.execute(ci.make_plan([], full=True), results=tmp_path / "plain") == 0
    assert not [a for a in calls[0] if a.startswith("--cov")]
    assert calls[0].count("src/tests") == 1
    calls.clear()
    plan = ci.make_plan([], full=True, coverage=True)
    assert plan["coverage"] is True
    assert ci.execute(plan, results=tmp_path / "weekly") == 0
    assert "--cov-branch" in calls[0] and "--cov-context=test" in calls[0]


def test_coverage_is_never_collected_for_a_scoped_tier(project):
    project("docs/testing.md", "# Guide\n")
    assert ci.make_plan(["src/agents/leaf.py"], coverage=True)["coverage"] is False
    assert ci.make_plan(["docs/testing.md"], coverage=True)["coverage"] is False


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
    assert len(calls) == 1  # the doc tests only: no smoke, no unrelated experiment
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


NESTED_TESTS = "tools/pipeline-diagnostics/tests"
NESTED_BACKEND = "tools/pipeline-diagnostics/backend/mailroom_diagnostics"


def nested_tool(project, count=2):
    for n in range(count):
        project(f"{NESTED_BACKEND}/module_{n}.py", f"value = {n}\n")
        project(f"{NESTED_TESTS}/test_module_{n}.py", f"from mailroom_diagnostics import module_{n}\n")


def test_tools_backend_change_runs_the_nested_tool_tests(project):
    nested_tool(project)
    plan = ci.make_plan([f"{NESTED_BACKEND}/module_0.py"])
    assert plan["profile"] == "affected"
    assert set(plan["tests"]) == {
        f"{NESTED_TESTS}/test_module_0.py",
        f"{NESTED_TESTS}/test_module_1.py",
    }
    assert set(plan["smoke"]) == set(ci.SMOKE)
    assert plan["selected_test_files"] == len(set(ci.SMOKE)) + 2
    assert plan["experiment"] is False


def test_nested_test_change_selects_the_nested_suite(project):
    nested_tool(project)
    target = f"{NESTED_TESTS}/test_module_0.py"
    plan = ci.make_plan([target])
    assert plan["profile"] == "affected"
    # A test module may be imported by a sibling; run the whole nested suite.
    assert set(plan["tests"]) == {
        f"{NESTED_TESTS}/test_module_0.py",
        f"{NESTED_TESTS}/test_module_1.py",
    }


def test_nested_fixture_named_like_a_test_is_not_passed_to_pytest(project):
    nested_tool(project)
    fixture = f"{NESTED_TESTS}/fixtures/test_sample.json"
    project(fixture, "{}\n")
    plan = ci.make_plan([fixture])
    assert plan["profile"] == "affected"
    assert fixture not in plan["tests"]
    assert f"{NESTED_TESTS}/test_module_0.py" in plan["tests"]


def test_exporter_change_selects_the_nested_parity_suite(project):
    nested_tool(project)
    project("src/scripts/export_contract.py", "value = 1\n")
    project("src/tests/test_contract_export.py", "from scripts import export_contract\n")
    plan = ci.make_plan(["src/scripts/export_contract.py"])
    assert plan["profile"] == "affected"
    assert "src/tests/test_contract_export.py" in plan["tests"]
    assert {
        f"{NESTED_TESTS}/test_module_0.py",
        f"{NESTED_TESTS}/test_module_1.py",
    } <= set(plan["tests"])


def test_nested_documentation_keeps_the_root_docs_truth_checks(project):
    nested_tool(project)
    project("tools/pipeline-diagnostics/README.md", "# Native diagnostics\n")
    plan = ci.make_plan(["tools/pipeline-diagnostics/README.md"])
    assert plan["profile"] == "affected"
    assert set(ci.DOC_TESTS) <= set(plan["tests"])
    assert f"{NESTED_TESTS}/test_module_0.py" in plan["tests"]


def test_full_plan_and_counts_include_the_nested_test_root(project):
    nested_tool(project)
    project("src/graph/routing.py", "value = 1\n")
    root_count = len(list((ci.REPO / "src/tests").rglob("test_*.py")))
    plan = ci.make_plan([], full=True)
    assert plan["profile"] == "full"
    assert plan["tests"] == ["src/tests", NESTED_TESTS]
    assert plan["total_test_files"] == root_count + 2
    assert plan["selected_test_files"] == plan["total_test_files"]
    for shared in (ci.make_plan(["src/graph/routing.py"]),
                   ci.make_plan([], branch="release/1.0")):
        assert shared["profile"] == "full"
        assert shared["tests"] == ["src/tests", NESTED_TESTS]


def test_full_executor_collects_both_test_roots(project, monkeypatch, tmp_path):
    nested_tool(project)
    calls = []
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 0)
    assert ci.execute(ci.make_plan([], full=True), results=tmp_path / "evidence") == 0
    assert calls[0].count("src/tests") == 1
    assert calls[0].count(NESTED_TESTS) == 1


def test_deleted_nested_tool_file_forces_full_with_nested_tests(project):
    nested_tool(project)
    plan = ci.make_plan([f"{NESTED_BACKEND}/removed.py"])
    assert plan["profile"] == "full"
    assert "deleted" in plan["reason"]
    assert NESTED_TESTS in plan["tests"]


def test_nested_root_without_tests_falls_back_to_full_without_an_empty_run(project):
    project(f"{NESTED_BACKEND}/module.py", "value = 1\n")
    project(f"{NESTED_TESTS}/.keep", "")
    plan = ci.make_plan([f"{NESTED_BACKEND}/module.py"])
    assert plan["profile"] == "full"
    assert "test root" in plan["reason"]
    assert plan["tests"] == ["src/tests"]


def test_full_plan_without_a_nested_tool_keeps_root_scope_only(project):
    plan = ci.make_plan([], full=True)
    assert plan["tests"] == ["src/tests"]
    assert plan["total_test_files"] == len(list((ci.REPO / "src/tests").rglob("test_*.py")))
    missing = ci.make_plan([f"{NESTED_BACKEND}/module.py"])
    assert missing["profile"] == "full"
    assert missing["tests"] == ["src/tests"]


def test_mixed_root_and_nested_changes_keep_both_selections(project):
    nested_tool(project)
    plan = ci.make_plan(["src/agents/leaf.py", f"{NESTED_BACKEND}/module_0.py"])
    assert plan["profile"] == "affected"
    assert {"src/tests/test_bridge.py", "src/tests/test_dynamic.py"} <= set(plan["tests"])
    assert f"{NESTED_TESTS}/test_module_0.py" in plan["tests"]


def test_promotion_to_full_selects_both_roots_and_reports_the_threshold(project):
    nested_tool(project)
    for n in range(18):
        project(f"src/tests/test_leaf_{n}.py", "from agents import leaf\n")
    plan = ci.make_plan(["src/agents/leaf.py"])
    assert plan["profile"] == "full"
    assert "65%" in plan["reason"]
    assert plan["tests"] == ["src/tests", NESTED_TESTS]


# --- data-driven inputs map to their consumers instead of forcing the full suite ---------------


def test_notebook_inputs_map_to_the_notebook_suite_not_to_full(project):
    project("src/tests/test_notebook_suite.py", 'names = "14_gmail_pilot.ipynb"\n')
    project("notebooks/14_gmail_pilot.ipynb", "{}\n")
    project("notebooks/pipeline_lab.py", "value = 1\n")
    project("src/tests/test_lab.py", "from notebooks import pipeline_lab\n")
    nb = ci.make_plan(["notebooks/14_gmail_pilot.ipynb"])
    assert (nb["profile"], nb["tests"]) == ("affected", ["src/tests/test_notebook_suite.py"])
    assert nb["experiment"] is False   # no pipeline code changed
    lab = ci.make_plan(["notebooks/pipeline_lab.py"])
    assert lab["profile"] == "affected"
    assert set(lab["tests"]) == {"src/tests/test_lab.py", "src/tests/test_notebook_suite.py"}


def test_a_data_file_maps_to_tests_that_name_it_its_directory_or_its_module(project):
    project("deploy/litellm/config.yaml", "model_list: []\n")
    project("src/tests/test_gateway.py", 'path = "deploy/litellm/config.yaml"\n')
    project("src/tests/test_by_dir.py", 'root = repo / "litellm"\n')
    project("src/tests/test_unrelated_gateway.py", "x = 1\n")
    plan = ci.make_plan(["deploy/litellm/config.yaml"])
    assert plan["profile"] == "affected"
    assert plan["tests"] == ["src/tests/test_by_dir.py", "src/tests/test_gateway.py"]


def test_frozen_prompt_and_baseline_inputs_use_the_explicit_consumer_map(project):
    project("src/llm/frozen_v1/lineage.json", "{}\n")
    project("src/tests/test_frozen_v1_prompts.py", "x = 1\n")
    project("src/tests/test_prompt_mutation_lineage.py", "x = 1\n")
    plan = ci.make_plan(["src/llm/frozen_v1/lineage.json"])
    assert plan["profile"] == "affected"
    assert {"src/tests/test_frozen_v1_prompts.py", "src/tests/test_prompt_mutation_lineage.py"} <= set(plan["tests"])
    assert plan["experiment"] is True   # prompts feed the pipeline
    project("docs/superpowers/baselines/experiment-baseline.json", "{}\n")
    project("src/tests/test_experiment_gate_script.py", "x = 1\n")
    baseline = ci.make_plan(["docs/superpowers/baselines/experiment-baseline.json"])
    assert baseline["profile"] == "affected" and baseline["experiment"] is True
    assert "src/tests/test_experiment_gate_script.py" in baseline["tests"]


def test_contract_json_selects_the_exporter_test_and_the_nested_suite(project):
    nested_tool(project)
    project("contract/mailroom-contract.json", "{}\n")
    project("src/tests/test_contract_export.py", "x = 1\n")
    plan = ci.make_plan(["contract/mailroom-contract.json"])
    assert plan["profile"] == "affected"
    assert {"src/tests/test_contract_export.py", f"{NESTED_TESTS}/test_module_0.py"} <= set(plan["tests"])


def test_a_data_file_nothing_reads_is_full_not_silently_green(project):
    project("deploy/orphan.yaml", "a: 1\n")
    plan = ci.make_plan(["deploy/orphan.yaml"])
    assert plan["profile"] == "full"
    assert plan["reason"] == "no known test consumer: deploy/orphan.yaml"


def test_data_inputs_with_consumers_do_not_hide_an_unmapped_one(project):
    project("deploy/litellm/config.yaml", "model_list: []\n")
    project("src/tests/test_gateway.py", 'path = "deploy/litellm/config.yaml"\n')
    project("deploy/orphan.yaml", "a: 1\n")
    plan = ci.make_plan(["deploy/litellm/config.yaml", "deploy/orphan.yaml"])
    assert plan["profile"] == "full" and "orphan.yaml" in plan["reason"]


def test_the_explicit_consumer_map_and_global_list_name_real_files():
    """Typos here would silently route changes to the wrong tests (this reads the real repo)."""
    repo = Path(__file__).resolve().parents[2]
    for prefix, tests in ci.DATA_CONSUMERS.items():
        assert (repo / prefix).exists(), prefix
        for test in tests:
            assert (repo / test).is_file(), test
    for name in (*ci.GLOBAL_FILES, *ci.SMOKE, *ci.DOC_TESTS):
        assert (repo / name).is_file(), name
    for prefix in ci.EXPERIMENT_INPUTS:
        assert (repo / prefix).exists(), prefix


# --- escalation bookkeeping, tiers and reporting ----------------------------------------------


def test_every_escalation_trigger_is_recorded_and_the_first_is_the_headline(project):
    project("pyproject.toml")
    project("src/config/taxonomy.yaml")
    plan = ci.make_plan(["pyproject.toml", "src/config/taxonomy.yaml"], branch="release/1.0")
    assert plan["escalations"] == [
        "release branch backstop",
        "global contract changed: pyproject.toml",
        "global contract changed: src/config/taxonomy.yaml",
    ]
    assert plan["reason"] == "release branch backstop"


def test_breadth_thresholds_are_named_and_enforced(project, monkeypatch):
    paths = []
    for n in range(ci.BROAD_PRODUCTION_FILES - 1):
        paths.append(f"src/agents/leaf_{n}.py")
        project(paths[-1], "value = 1\n")
        project(f"src/tests/test_leaf_{n}.py", f"import agents.leaf_{n}\n")
    assert ci.make_plan(paths)["profile"] == "affected"
    monkeypatch.setattr(ci, "BROAD_PRODUCTION_FILES", len(paths))
    assert "or more files" in ci.make_plan(paths)["reason"]


def test_each_tier_has_its_own_cache_scope(project):
    project("docs/testing.md", "# Guide\n")
    plans = [ci.make_plan(["docs/testing.md"]), ci.make_plan(["src/tests/test_bridge.py"]),
             ci.make_plan(["src/agents/leaf.py"]), ci.make_plan([], full=True)]
    assert [p["tier"] for p in plans] == [0, 1, 2, 3]
    assert len({ci.cache_key(p) for p in plans}) == 4
    # coverage only changes how a full run is observed, not what it proves
    assert ci.cache_key(ci.make_plan([], full=True)) == ci.cache_key(ci.make_plan([], full=True, coverage=True))


def test_the_banner_leads_with_the_tier_and_the_reason(project):
    project("pyproject.toml")
    banner = ci.banner(ci.make_plan(["pyproject.toml"]))
    assert "CI TEST TIER 3 (full): global contract changed: pyproject.toml" in banner
    docs = ci.banner(ci.make_plan(["src/agents/leaf.py"]))
    assert "CI TEST TIER 2 (affected)" in docs and "experiment gate: yes" in docs


def test_execution_prints_the_tier_and_a_stage_table_with_the_budget(project, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kw: 0)
    plan = ci.make_plan(["src/agents/leaf.py"])
    assert ci.execute(plan, results=tmp_path / "evidence") == 0
    out = capsys.readouterr().out
    assert out.startswith("=") and "CI TEST TIER 2 (affected)" in out
    assert "smoke" in out and "affected" in out and "experiment" in out
    assert f"of {plan['budget_seconds']}s budget" in out and "OVER" not in out


def test_a_run_over_its_tier_budget_is_reported_but_not_failed(project, monkeypatch, tmp_path, capsys):
    clock = iter(range(0, 10_000, 1000))
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ci.subprocess, "call", lambda cmd, **kw: 0)
    assert ci.execute(ci.make_plan(["docs/none.md"]) | {"budget_seconds": 1}, results=tmp_path / "e") == 0
    assert "OVER the 1s tier budget" in capsys.readouterr().out


def test_a_failing_changelog_check_is_red_and_stops_the_experiment(project, monkeypatch, tmp_path):
    project("CHANGELOG.md", "# Changes\n")
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return 3 if "sync_gitbook_changelog.py" in " ".join(map(str, cmd)) else 0
    monkeypatch.setattr(ci.subprocess, "call", run)
    plan = ci.make_plan(["CHANGELOG.md", "src/agents/leaf.py"])
    assert ci.execute(plan, results=tmp_path / "e") == 3
    assert not any("run_experiment.py" in " ".join(map(str, c)) for c in calls)
    assert json.loads((tmp_path / "e/execution.json").read_text())[-1]["stage"] == "changelog-sync"


def test_the_import_graph_is_built_once_per_plan(project, monkeypatch):
    builds = []
    real = affected.Index
    monkeypatch.setattr(affected, "Index", lambda: builds.append(1) or real())
    paths = ["src/agents/leaf.py", "src/agents/bridge.py"]
    project("src/tests/test_leaf_direct.py", "import agents.leaf\n")
    assert ci.make_plan(paths)["profile"] == "affected"
    assert len(builds) == 1


def test_cli_passes_the_coverage_flag_to_a_full_plan_only(project, tmp_path):
    out = tmp_path / "plan.json"
    assert ci.main(["--full", "--coverage", "--files", "--plan", str(out)]) == 0
    assert json.loads(out.read_text())["coverage"] is True
    assert ci.main(["--full", "--files", "--plan", str(out)]) == 0
    assert json.loads(out.read_text())["coverage"] is False
