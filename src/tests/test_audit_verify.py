"""Roadmap M4: verification of the unified audit DB (spec §6)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from storage import db

from tests.test_audit_writer import _attempt, _row  # noqa: F401  (shared builders)


@pytest.fixture
def audit_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))  # manifests (pending check) stay in tmp
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    return path


def _exec(path: Path, sql: str, *args) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(sql, args)


def _q(path: Path, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


async def _populate():
    from audit.models import Addendum
    from audit.writer import write_addendum, write_terminal_row

    await write_terminal_row(_row("doc-1", attempts=[_attempt(1), _attempt(2, trigger="retry")]))
    await write_terminal_row(_row("doc-2", attempts=[_attempt(1, stage="judge")]))
    await write_addendum(Addendum("doc-1", "review_decision", "reviewer", {"decision": "approve"}))
    await write_addendum(
        Addendum("doc-1", "resumed", "pipeline", {"event": "archived"}, attempts=[_attempt(3, tag="r")])
    )


def _failed(section) -> bool:
    return not section.ok and section.failures


async def test_clean_db_verifies(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    report = await verify_audit_db()
    assert report.ok, report.as_dict()
    assert report.main.checked == 2
    assert report.subrows.checked == 4
    assert report.addenda.checked == 2
    for name in ("main", "subrows", "addenda", "legacy", "trace_io", "pending"):
        section = getattr(report, name)
        assert section.ok and section.failures == []
    assert set(report.as_dict()) == {"ok", "main", "subrows", "addenda", "legacy", "trace_io", "pending"}


async def test_tampered_row_detail_fails_main(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    detail = json.loads(_q(audit_db, "SELECT detail FROM audit_log WHERE doc_id='doc-2'")[0][0])
    detail["pipeline_success"] = False
    _exec(audit_db, "UPDATE audit_log SET detail=? WHERE doc_id='doc-2'", json.dumps(detail))
    report = await verify_audit_db()
    assert not report.ok and _failed(report.main)
    assert report.main.failures[0]["doc_id"] == "doc-2"
    assert report.subrows.ok and report.addenda.ok


async def test_broken_prev_link_fails_main(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    _exec(audit_db, "UPDATE audit_log SET prev_hash='x' WHERE doc_id='doc-2'")
    report = await verify_audit_db()
    assert any(f["reason"] == "prev_hash_mismatch" for f in report.main.failures)


async def test_deleted_subrow_fails_subrows(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    _exec(audit_db, "DELETE FROM audit_attempts WHERE attempt_id='att-extraction-2-'")
    report = await verify_audit_db()
    assert not report.ok and report.main.ok
    assert [f["reason"] for f in report.subrows.failures] == ["missing_subrow"]


async def test_extra_subrow_fails_subrows(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    (parent,) = _q(audit_db, "SELECT entry_id FROM audit_log WHERE doc_id='doc-2'")[0:1]
    _exec(
        audit_db,
        "INSERT INTO audit_attempts (attempt_id, parent_id, doc_id, stage, attempt_no, trigger, outcome, "
        "detail, entry_hash) VALUES ('att-extra', ?, 'doc-2', 'judge', 9, 'retry', 'ok', '{}', 'h')",
        parent[0],
    )
    report = await verify_audit_db()
    assert [f["reason"] for f in report.subrows.failures] == ["unlisted_subrow"]


async def test_edited_subrow_fails_subrows(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    _exec(audit_db, "UPDATE audit_attempts SET outcome='error' WHERE attempt_id='att-judge-1-'")
    report = await verify_audit_db()
    assert [f["reason"] for f in report.subrows.failures] == ["subrow_hash_mismatch"]


async def test_edited_addendum_fails_addenda(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    _exec(audit_db, "UPDATE audit_addenda SET detail=? WHERE seq=1", json.dumps({"decision": "reject"}))
    report = await verify_audit_db()
    assert not report.ok and report.main.ok
    assert report.addenda.failures[0]["reason"] == "entry_hash_mismatch"
    # The second addendum still links to the stored hash of the first, so only one failure.
    assert len(report.addenda.failures) == 1


async def test_addendum_subrows_are_checked(audit_db):
    from audit.verify import verify_audit_db

    await _populate()
    _exec(audit_db, "DELETE FROM audit_attempts WHERE attempt_id='att-extraction-3-r'")
    report = await verify_audit_db()
    assert [f["reason"] for f in report.subrows.failures] == ["missing_subrow"]


async def test_tampered_legacy_fails_legacy_only(audit_db):
    from audit.verify import verify_audit_db
    from schemas.audit import build_audit_entry

    entry = build_audit_entry("doc-old", "M-1", "ingested", "intake", {"n": 1})
    _exec(
        audit_db,
        "INSERT INTO audit_log_legacy (entry_id, doc_id, matter_id, event, actor, detail, prev_hash, "
        "entry_hash, seq, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        entry.entry_id, entry.doc_id, entry.matter_id, entry.event, entry.actor,
        json.dumps({"n": 2}), entry.prev_hash, entry.entry_hash,
        entry.timestamp.replace(tzinfo=None).isoformat(sep=" "),
    )
    await _populate()
    report = await verify_audit_db()
    assert not report.ok and not report.legacy.ok
    assert report.legacy.failures[0]["doc_id"] == "doc-old"
    assert report.main.ok and report.subrows.ok and report.addenda.ok


async def test_io_sha_mismatch_fails_trace_io(audit_db):
    from audit.digests import io_sha256
    from audit.verify import verify_audit_db
    from audit.writer import write_terminal_row

    good = io_sha256("prompt", "completion")
    trace = [{"visit_id": "v-1", "node": "sorter", "io_sha256": good}]
    await write_terminal_row(_row("doc-t", trace=trace))
    _exec(audit_db, "INSERT INTO audit_trace_io (visit_id, doc_id, prompt, completion, sha256, created_at) "
          "VALUES ('v-1', 'doc-t', 'prompt', 'completion', ?, '2026-11-03 09:00:00')", good)
    assert (await verify_audit_db()).trace_io.ok
    _exec(audit_db, "UPDATE audit_trace_io SET completion='edited' WHERE visit_id='v-1'")
    report = await verify_audit_db()
    assert not report.ok and report.trace_io.failures[0]["reason"] == "io_content_mismatch"
    _exec(audit_db, "DELETE FROM audit_trace_io WHERE visit_id='v-1'")
    assert (await verify_audit_db()).trace_io.failures[0]["reason"] == "io_missing"


async def test_recovered_row_detail_sha_checked(audit_db):
    from audit.digests import detail_sha256
    from audit.verify import verify_audit_db
    from audit.writer import write_terminal_row

    row = _row("doc-r")
    sha = detail_sha256({**row.detail, "attempts": []})  # the pending record holds the detail as stored
    row.detail["recovered_from"] = {"sink": "sql", "ref": "p-1", "captured_at": "t", "detail_sha256": sha}
    await write_terminal_row(row)
    assert (await verify_audit_db()).ok
    bad = _row("doc-r2")
    bad.detail["recovered_from"] = {"sink": "sql", "ref": "p-2", "captured_at": "t", "detail_sha256": "0" * 64}
    await write_terminal_row(bad)
    report = await verify_audit_db()
    assert [(f["doc_id"], f["reason"]) for f in report.main.failures] == [("doc-r2", "recovered_detail_sha_mismatch")]


async def test_pending_manifest_without_row_reported(audit_db):
    from audit.verify import verify_audit_db
    from audit.writer import write_terminal_row
    from pipeline.bins import manifests_dir

    mdir = manifests_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "doc-p.json").write_text(json.dumps({"doc_id": "doc-p", "audit_written": "pending"}))
    (mdir / "doc-q.json").write_text(json.dumps({"doc_id": "doc-q", "audit_written": "pending_partial"}))
    (mdir / "doc-done.json").write_text(json.dumps({"doc_id": "doc-done", "audit_written": "pending"}))
    (mdir / "doc-ok.json").write_text(json.dumps({"doc_id": "doc-ok", "audit_written": True}))
    await write_terminal_row(_row("doc-done"))
    report = await verify_audit_db()
    assert not report.ok and report.main.ok
    assert sorted((f["doc_id"], f["audit_written"]) for f in report.pending.failures) == [
        ("doc-p", "pending"), ("doc-q", "pending_partial")
    ]
    assert report.pending.checked == 3


async def test_unmigrated_db_checks_the_live_event_table_as_legacy(audit_db):
    from audit.verify import verify_audit_db

    _exec(audit_db, "DELETE FROM audit_meta")
    report = await verify_audit_db()
    assert report.ok and report.main.checked == 0 and report.legacy.ok


def _load_script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_audit_chains.py"
    spec = importlib.util.spec_from_file_location("verify_audit_chains_m4", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exits_nonzero_on_failure(audit_db, monkeypatch, capsys):
    import asyncio

    asyncio.run(_populate())
    mod = _load_script()
    monkeypatch.setattr(sys, "argv", ["verify_audit_chains.py"])
    assert mod.main() == 0
    assert "main chain" in capsys.readouterr().out
    _exec(audit_db, "UPDATE audit_attempts SET outcome='error' WHERE attempt_id='att-judge-1-'")
    assert mod.main() == 1
    monkeypatch.setattr(sys, "argv", ["verify_audit_chains.py", "--json"])
    capsys.readouterr()
    assert mod.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["subrows"]["failures"][0]["reason"] == "subrow_hash_mismatch"


def test_legacy_verify_chain_alias():
    from schemas.audit import build_audit_entry, verify_chain, verify_legacy_chain

    entry = build_audit_entry("d", "M", "ingested", "intake", {})
    assert verify_legacy_chain([entry]) and verify_chain([entry])
    with pytest.raises(TypeError):
        verify_chain([{"entry_id": "row-1"}])
