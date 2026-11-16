"""#246 follow-up: dropped blank-text rows must not pass the live pull/smoke scripts silently.

``build_manifest_rows`` drops rows with no ``doc_text`` and counts them in
``integrity["missing_text"]``. The two live scripts used to check only
``mismatched``. No network: the live_manifest functions are replaced.
"""

from __future__ import annotations

import argparse

import pytest

from scripts import pull_live_manifest, smoke_live_documents

REVISION = {"tag": "v9.9", "sha": "a" * 40}


def _stats(missing: int, mismatched: int = 0) -> dict:
    return {
        "classes": {},
        "integrity": {"checked": 3, "verified": 3, "mismatched": mismatched, "missing_text": missing},
    }


def _patch_pull(monkeypatch, stats: dict) -> list:
    written: list = []
    monkeypatch.setattr(pull_live_manifest, "resolve_revision", lambda _r: REVISION)
    monkeypatch.setattr(pull_live_manifest, "load_frames", lambda _r: (None, None, {}))
    monkeypatch.setattr(pull_live_manifest, "build_manifest_rows", lambda *_a: ([{"id": "a"}], stats))

    def write(rows, out, provenance):
        written.append(out)
        return {"csv_sha256": "x" * 64}

    monkeypatch.setattr(pull_live_manifest, "write_manifest_csv", write)
    return written


def test_pull_refuses_missing_text_by_default(monkeypatch, tmp_path, capsys):
    written = _patch_pull(monkeypatch, _stats(missing=2))
    assert pull_live_manifest.main(["--live", "--out", str(tmp_path / "m.csv")]) == 1
    assert not written
    err = capsys.readouterr().err
    assert "refusing" in err and "2 corpus rows have no document text" in err
    assert "--allow-missing-text" in err


def test_pull_allows_missing_text_with_the_explicit_flag(monkeypatch, tmp_path, capsys):
    written = _patch_pull(monkeypatch, _stats(missing=2))
    assert pull_live_manifest.main(["--live", "--allow-missing-text", "--out", str(tmp_path / "m.csv")]) == 0
    assert written
    assert "warning: 2 corpus rows have no document text" in capsys.readouterr().err


def test_pull_without_missing_text_is_unchanged(monkeypatch, tmp_path):
    written = _patch_pull(monkeypatch, _stats(missing=0))
    assert pull_live_manifest.main(["--live", "--out", str(tmp_path / "m.csv")]) == 0
    assert written


def test_pull_still_refuses_hash_mismatches_first(monkeypatch, tmp_path, capsys):
    written = _patch_pull(monkeypatch, _stats(missing=2, mismatched=1))
    assert pull_live_manifest.main(["--live", "--allow-missing-text", "--out", str(tmp_path / "m.csv")]) == 1
    assert not written
    assert "content_sha256 mismatches" in capsys.readouterr().err


def _smoke_args(**over) -> argparse.Namespace:
    base = dict(live=True, revision=None, per_class=1, classes=["contract"], max_chars=250_000)
    base.update(over)
    return argparse.Namespace(**base)


def _patch_smoke(monkeypatch, stats: dict) -> None:
    monkeypatch.setattr(smoke_live_documents, "resolve_revision", lambda _r: REVISION)
    monkeypatch.setattr(smoke_live_documents, "load_frames", lambda _r: (None, None, {}))
    monkeypatch.setattr(smoke_live_documents, "build_manifest_rows", lambda *_a: ([{"id": "a"}], stats))
    monkeypatch.setattr(smoke_live_documents, "select_smoke_rows", lambda rows, **_k: rows)


def test_smoke_refuses_missing_text_by_default(monkeypatch):
    _patch_smoke(monkeypatch, _stats(missing=3))
    with pytest.raises(SystemExit) as excinfo:
        smoke_live_documents._load(_smoke_args())
    message = str(excinfo.value)
    assert "refusing" in message and "3 corpus rows have no document text" in message
    assert "--allow-missing-text" in message


def test_smoke_allows_missing_text_with_the_explicit_flag(monkeypatch, capsys):
    _patch_smoke(monkeypatch, _stats(missing=3))
    rows, revision, source = smoke_live_documents._load(_smoke_args(allow_missing_text=True))
    assert rows == [{"id": "a"}] and source == "live"
    assert "warning: 3 corpus rows have no document text" in capsys.readouterr().err


def test_smoke_without_missing_text_is_unchanged(monkeypatch):
    _patch_smoke(monkeypatch, _stats(missing=0))
    rows, _revision, source = smoke_live_documents._load(_smoke_args())
    assert rows == [{"id": "a"}] and source == "live"


def test_smoke_cli_exposes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(smoke_live_documents, "_run", lambda args: seen.update(vars(args)) or 0)
    assert smoke_live_documents.main(["--live", "--allow-missing-text"]) == 0
    assert seen["allow_missing_text"] is True
