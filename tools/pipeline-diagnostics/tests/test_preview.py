"""Fresh-process preview refresh: atomic publication and last-good recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mailroom_diagnostics.models import DiagnosticsError
from mailroom_diagnostics.preview import (
    read_state,
    refresh,
    seam_fingerprint,
    watch,
)
from mailroom_diagnostics.snapshot import load_artifact

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def valid_bytes(minimal_contract) -> bytes:
    return load_artifact(minimal_contract).model_dump_json().encode("utf-8")


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    (root / "src/config").mkdir(parents=True)
    (root / "src/config/taxonomy.yaml").write_text("confidence: {}\n", encoding="utf-8")
    return root


def test_refresh_publishes_atomically_and_clears_previous_error(tmp_path, workspace, valid_bytes):
    base = tmp_path / "preview"
    base.mkdir()
    (base / "error.json").write_text('{"code": "old", "message": "stale"}\n', encoding="utf-8")
    seen = []

    state = refresh(workspace, base, builder=lambda path: seen.append(path) or valid_bytes)

    assert seen == [workspace]
    assert state.error is None
    assert state.preview and state.last_good
    assert state.preview["provenance"]["kind"] == "artifact"
    assert not list(base.glob("*.tmp")) and not list(base.glob(".*.tmp"))


def test_failed_rebuild_keeps_the_last_valid_preview(tmp_path, workspace, valid_bytes):
    base = tmp_path / "preview"
    good = refresh(workspace, base, builder=lambda path: valid_bytes)
    assert good.preview is not None

    def boom(_path: Path) -> bytes:
        raise DiagnosticsError("preview-failed", "preview process exited 2")

    failed = refresh(workspace, base, builder=boom)

    assert failed.error == {
        "code": "preview-failed",
        "message": "preview process exited 2",
        "at": failed.error["at"],
    }
    assert failed.preview == good.preview
    assert failed.last_good == good.last_good


def test_invalid_output_is_recorded_without_replacing_the_preview(tmp_path, workspace, valid_bytes):
    base = tmp_path / "preview"
    good = refresh(workspace, base, builder=lambda path: valid_bytes)

    state = refresh(workspace, base, builder=lambda path: b"{not json")

    assert state.error["code"] == "preview-failed"
    assert state.preview == good.preview
    assert state.last_good == good.last_good


def test_staleness_tracks_the_full_seam_fingerprint(tmp_path, workspace, valid_bytes):
    base = tmp_path / "preview"
    state = refresh(workspace, base, builder=lambda path: valid_bytes)
    assert state.stale is False

    taxonomy = workspace / "src/config/taxonomy.yaml"
    taxonomy.write_text("confidence: {high: 0.9}\n", encoding="utf-8")
    assert read_state(base, workspace).stale is True

    assert refresh(workspace, base, builder=lambda path: valid_bytes).stale is False
    taxonomy.unlink()
    assert read_state(base, workspace).stale is True


def test_watch_debounces_a_source_change_into_one_rebuild(tmp_path, workspace, valid_bytes):
    base = tmp_path / "preview"
    fingerprints = iter([("a",), ("a",), ("a",), ("b",), ("b",), ("b",)])
    clock = iter([0.0, 1.0])
    refreshes = []

    watch(
        workspace,
        base,
        cycles=5,
        interval=0.01,
        debounce=0.3,
        clock=lambda: next(clock),
        sleep=lambda _seconds: None,
        fingerprint=lambda _workspace: next(fingerprints),
        refresh_fn=lambda path, directory: refreshes.append((path, directory)),
    )

    assert refreshes == [(workspace, base)]


def test_fingerprint_only_uses_declared_seams(tmp_path, workspace):
    before = seam_fingerprint(workspace)
    (workspace / "unrelated.md").write_text("docs\n", encoding="utf-8")
    assert seam_fingerprint(workspace) == before

    taxonomy = workspace / "src/config/taxonomy.yaml"
    taxonomy.write_text("confidence: {high: 0.9}\n", encoding="utf-8")
    os.utime(taxonomy, None)
    assert seam_fingerprint(workspace) != before

    taxonomy.unlink()
    assert seam_fingerprint(workspace) != before


def test_fresh_process_builds_a_valid_workspace_preview(tmp_path):
    state = refresh(REPO_ROOT, tmp_path / "preview")
    assert state.error is None, state.error
    provenance = state.preview["provenance"]
    assert provenance["kind"] == "workspace-preview"
    assert provenance["config_source"] == "workspace-files"
    assert provenance["git_sha"]
    assert set(provenance["hashes"]) >= {"topology", "routing", "taxonomy", "effective_policy"}
    assert state.stale is False


def test_cli_preview_prints_one_json_document(run_cli, tmp_path):
    proc = run_cli("preview", "--base", str(tmp_path / "preview"), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["preview"]["provenance"]["kind"] == "workspace-preview"
    assert payload["error"] is None
