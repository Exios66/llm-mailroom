"""CLI protocol: pure JSON, bounded input, honest errors and exit codes."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from mailroom_diagnostics import snapshot as snapshot_module
from mailroom_diagnostics.cli import EXIT_FINDINGS, EXIT_INVALID, EXIT_OK, main
from mailroom_diagnostics.snapshot import build_workspace_snapshot


def test_inspect_json_is_one_document_on_stdout(run_cli):
    proc = run_cli("inspect", "--json")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)  # a leaked log line would break this
    assert payload["provenance"]["kind"] == "workspace-preview"
    assert payload["provenance"]["active_worker"] is False
    assert payload["provenance"]["tracing"] == "disabled"
    assert payload["graph"]["nodes"] and payload["graph"]["edges"]
    assert payload["graph_hash"].startswith("sha256:")
    assert "score_configs_validated" not in proc.stdout


def test_inspect_artifact_is_labelled_as_artifact(run_cli, repo_contract, contract_path):
    proc = run_cli("inspect", "--contract", str(contract_path), "--json")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)
    provenance = payload["provenance"]
    assert provenance["kind"] == "artifact"
    assert provenance["active_worker"] is False
    assert provenance["git_sha"] is None
    assert provenance["contract_version"] == "1.0.0"
    assert provenance["artifact_bytes"] > 0
    assert len(provenance["artifact_sha256"]) == 64
    assert payload["graph"] == repo_contract["graph"]


def test_check_is_clean_and_declares_limited_coverage(run_cli, contract_path):
    proc = run_cli("check", "--contract", str(contract_path), "--json")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["findings"] == []
    assert payload["exit_code"] == EXIT_OK
    assert payload["coverage"]["scope"] == "structural"
    assert payload["coverage"]["live_observations"] is False
    assert set(payload["coverage"]["checks"]) == {"endpoint-consistency", "entry-reachability"}
    assert any("dead" in line for line in payload["coverage"]["limitations"])


def test_workspace_check_is_clean(run_cli):
    proc = run_cli("check", "--json")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["provenance"]["kind"] == "workspace-preview"
    assert payload["findings"] == []


def test_console_script_is_installed():
    scripts = {entry.name: entry.value for entry in entry_points(group="console_scripts")}
    assert scripts.get("mailroom-diagnostics") == "mailroom_diagnostics.cli:main"


@pytest.mark.parametrize(
    ("fixture_name", "code"),
    [
        ("unsupported-contract.json", "unsupported-version"),
        ("malformed-graph.json", "invalid-schema"),
    ],
)
def test_invalid_artifacts_fail_with_json_and_exit_2(run_cli, fixtures_dir, fixture_name, code):
    proc = run_cli("inspect", "--contract", str(fixtures_dir / fixture_name), "--json")
    assert proc.returncode == EXIT_INVALID, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == code


def test_invalid_json_fails_with_json_and_exit_2(run_cli, tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    proc = run_cli("inspect", "--contract", str(path), "--json")
    assert proc.returncode == EXIT_INVALID
    assert json.loads(proc.stdout)["error"]["code"] == "invalid-json"


def test_missing_artifact_fails_with_exit_2(run_cli, tmp_path: Path):
    proc = run_cli("check", "--contract", str(tmp_path / "absent.json"), "--json")
    assert proc.returncode == EXIT_INVALID
    assert json.loads(proc.stdout)["error"]["code"] == "io-error"


def test_input_size_is_bounded(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(snapshot_module, "MAX_ARTIFACT_BYTES", 64)
    path = tmp_path / "big.json"
    path.write_text(
        json.dumps({"contract_version": "1.0.0", "padding": "x" * 200}), encoding="utf-8"
    )
    assert main(["inspect", "--contract", str(path), "--json"]) == EXIT_INVALID
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "input-too-large"


def test_duplicate_node_ids_are_invalid_input(mutated_contract, capsys):
    path = mutated_contract(lambda data: data["graph"]["nodes"].append(data["graph"]["nodes"][0]))
    assert main(["check", "--contract", str(path), "--json"]) == EXIT_INVALID
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid-schema"
    assert "duplicate node ids" in payload["error"]["message"]


def test_dangling_edge_is_a_finding_with_exit_1(mutated_contract, capsys):
    def add_edge(data):
        data["graph"]["edges"].append(
            {"source": "archive", "target": "ghost", "conditional": False, "label": None}
        )

    path = mutated_contract(add_edge)
    assert main(["check", "--contract", str(path), "--json"]) == EXIT_FINDINGS
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_FINDINGS
    dangling = [f for f in payload["findings"] if f["rule"] == "dangling-edge"]
    assert len(dangling) == 1
    assert dangling[0]["proof_status"] == "confirmed"
    assert dangling[0]["severity"] == "error"
    assert dangling[0]["witness"]["missing_endpoints"] == ["target"]


def test_human_output_mentions_the_finding(mutated_contract, capsys):
    def add_edge(data):
        data["graph"]["edges"].append(
            {"source": "__start__", "target": "ghost", "conditional": True, "label": None}
        )

    path = mutated_contract(add_edge)
    assert main(["check", "--contract", str(path)]) == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "dangling-edge" in out
    assert "ghost" in out


def test_workspace_snapshot_has_allowlisted_thresholds_only():
    snapshot = build_workspace_snapshot()
    dumped = snapshot.model_dump(mode="json")
    assert set(dumped["thresholds"]) == {
        "high",
        "low",
        "retry_max",
        "judge_band_high",
        "arbiter_retry_max",
        "judge_max_passes",
        "conflict_threshold",
        "by_class",
    }
    # Class names come from the taxonomy, never from a hardcoded inventory.
    by_class = dumped["thresholds"]["by_class"]
    assert by_class
    for class_key, row in by_class.items():
        assert isinstance(class_key, str) and class_key
        assert set(row) <= {"high", "low", "judge_band_high"}


def test_abbreviated_json_flag_is_rejected_instead_of_leaking_text(run_cli):
    """Only the literal --json token requests machine mode; --js is refused."""
    proc = run_cli("inspect", "--js")
    assert proc.returncode == EXIT_INVALID
    assert proc.stdout == ""
    assert "invalid-arguments" in proc.stderr


def test_pipeline_version_follows_the_imported_pipelines_provider(monkeypatch):
    import importlib.metadata as metadata

    monkeypatch.setattr(metadata, "packages_distributions", lambda: {"pipeline": ["other-dist"]})
    monkeypatch.setattr(
        metadata,
        "version",
        lambda name: {"other-dist": "9.9.9", "mailroom": "0.9.3"}[name],
    )
    assert snapshot_module._pipeline_version() == "9.9.9"

    monkeypatch.setattr(metadata, "packages_distributions", lambda: {})
    assert snapshot_module._pipeline_version() is None


def test_pipeline_version_requires_an_unambiguous_provider(monkeypatch):
    import importlib.metadata as metadata

    monkeypatch.setattr(
        metadata,
        "packages_distributions",
        lambda: {"pipeline": ["mailroom", "unrelated-mailroom"]},
    )
    assert snapshot_module._pipeline_version() is None

    monkeypatch.setattr(
        metadata,
        "packages_distributions",
        lambda: {"pipeline": ["mailroom", "mailroom"]},
    )
    assert snapshot_module._pipeline_version() == metadata.version("mailroom")
