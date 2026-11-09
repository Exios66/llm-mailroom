"""Provenance hashes: detect seams that moved without pretending to explain them."""

from __future__ import annotations

import hashlib
from pathlib import Path

from mailroom_diagnostics.models import Thresholds
from mailroom_diagnostics.provenance import (
    HASH_INPUTS,
    effective_policy_hash,
    hash_inputs,
    sha256_file,
    workspace_hashes,
)
from mailroom_diagnostics.snapshot import build_workspace_snapshot, load_artifact

REPO_ROOT = Path(__file__).resolve().parents[3]


def _thresholds(**overrides) -> Thresholds:
    data = {"high": 0.8, "low": 0.5, "retry_max": 2, "judge_band_high": 0.9}
    data.update(overrides)
    return Thresholds(**data)


def test_sha256_file_matches_hashlib(tmp_path: Path):
    target = tmp_path / "sample.bin"
    target.write_bytes(b"mailroom")
    assert sha256_file(target) == hashlib.sha256(b"mailroom").hexdigest()


def test_hash_inputs_is_deterministic_and_reports_missing(tmp_path: Path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    groups = {"one": ("a.py",), "two": ("a.py", "missing.py")}

    hashes, missing = hash_inputs(tmp_path, groups)
    assert missing == ["missing.py"]
    assert set(hashes) == {"one", "two"}
    assert hash_inputs(tmp_path, groups) == (hashes, missing)

    (tmp_path / "a.py").write_text("value = 2\n", encoding="utf-8")
    changed, _ = hash_inputs(tmp_path, groups)
    assert changed["one"] != hashes["one"]
    assert changed["two"] != hashes["two"]


def test_renaming_an_input_changes_the_group_hash(tmp_path: Path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("value = 1\n", encoding="utf-8")
    first, _ = hash_inputs(tmp_path, {"g": ("a.py",)})
    second, _ = hash_inputs(tmp_path, {"g": ("b.py",)})
    assert first["g"] != second["g"]


def test_effective_policy_hash_is_canonical_and_sensitive():
    base = effective_policy_hash(_thresholds())
    assert base.startswith("sha256:")
    assert base == effective_policy_hash(_thresholds())
    assert base != effective_policy_hash(_thresholds(high=0.81))


def test_workspace_hashes_include_sources_and_policy(tmp_path: Path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    thresholds = _thresholds()
    hashes, missing = workspace_hashes(tmp_path, thresholds)
    assert "effective_policy" in hashes
    assert hashes["effective_policy"] == effective_policy_hash(thresholds)
    assert missing


def test_snapshot_schema_version_announces_provenance_enrichment():
    snapshot = build_workspace_snapshot()
    assert snapshot.schema_version == "1.1.0"
    assert {"config_source", "hashes", "missing_inputs"} <= set(
        snapshot.provenance.model_dump()
    )


def test_workspace_snapshot_reports_source_hashes():
    snapshot = build_workspace_snapshot()
    provenance = snapshot.provenance
    assert provenance.config_source == "workspace-files"
    assert provenance.hashes["topology"].startswith("sha256:")
    assert provenance.hashes["routing"].startswith("sha256:")
    expected, _ = hash_inputs(
        REPO_ROOT,
        {
            "taxonomy": ("src/config/taxonomy.yaml",),
            "contract": ("contract/mailroom-contract.json",),
        },
    )
    assert provenance.hashes["taxonomy"] == expected["taxonomy"]
    assert provenance.hashes["contract"] == expected["contract"]
    # The group digest binds the path too; it is not a bare file hash.
    assert provenance.hashes["taxonomy"] != (
        "sha256:" + sha256_file(REPO_ROOT / "src/config/taxonomy.yaml")
    )
    assert provenance.hashes["effective_policy"].startswith("sha256:")
    assert provenance.missing_inputs == []
    # The graph hash and the source hashes are different questions.
    assert snapshot.graph_hash != provenance.hashes["topology"]


def test_artifact_provenance_does_not_invent_workspace_hashes(contract_path):
    provenance = load_artifact(contract_path).provenance
    assert provenance.config_source == "artifact"
    assert provenance.hashes == {}
    assert provenance.missing_inputs == []


def test_hash_inputs_only_names_real_seams():
    """Protect the allowlist from silently absorbing unrelated code."""
    assert set(HASH_INPUTS) == {
        "topology",
        "routing",
        "taxonomy",
        "contract",
        "implementation",
    }
    assert all(path.startswith(("src/", "config/", "contract/")) for paths in HASH_INPUTS.values() for path in paths)
