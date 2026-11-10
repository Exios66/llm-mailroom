"""Roadmap M11: ``GET /audit/{doc_id}`` and ``GET /audit`` over the unified row (spec §7)."""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from storage import db

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
AUTH = {"Authorization": "Bearer test-token-123"}


@pytest.fixture
def client(monkeypatch, temp_base_dir, tmp_path):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mailroom.db'}")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    if "api.main" in sys.modules:
        importlib.reload(sys.modules["api.main"])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _att(doc, stage, n, trigger="initial"):
    from audit.models import AttemptRow

    return AttemptRow(attempt_id=f"{doc}-{stage}-{n}", stage=stage, attempt_no=n, trigger=trigger, outcome="ok",
                      started_at=T0, ended_at=T0, detail={"nodes": [stage]})


def _write(doc, *, event="archived", attempts=(), addenda=()):
    from audit.models import Addendum
    from audit.row import build_row
    from audit.writer import write_addendum, write_terminal_row

    row = build_row({"doc_id": doc, "matter_id": "M", "doc_type": "contract"}, event=event, actor="archivist")
    row.attempts = list(attempts)

    async def go():
        await write_terminal_row(row)
        for kind, atts in addenda:
            await write_addendum(Addendum(doc_id=doc, kind=kind, actor="human_reviewer", detail={"note": kind},
                                          attempts=list(atts)))

    asyncio.run(go())


def test_audit_doc_contract(client):
    _write("d-1", attempts=[_att("d-1", "classification", 1), _att("d-1", "extraction", 1)],
           addenda=[("review_decision", [_att("d-1", "extraction", 2, "human_correction")]), ("rescore", [])])
    r = client.get("/audit/d-1", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"doc_id", "chain_length", "chain_valid", "entries", "legacy", "pending", "warehouse"}
    assert body["doc_id"] == "d-1" and body["chain_length"] == 3 and body["chain_valid"] is True
    entries = body["entries"]
    assert [e["kind"] for e in entries] == ["row", "attempt", "attempt", "addendum", "attempt", "addendum"]
    assert all("parent_id" in e for e in entries)
    row_id = entries[0]["entry_id"]
    assert entries[0]["parent_id"] is None
    assert entries[1]["parent_id"] == entries[2]["parent_id"] == row_id
    assert entries[3]["parent_id"] == row_id and entries[3]["addendum_kind"] == "review_decision"
    assert entries[4]["parent_id"] == entries[3]["addendum_id"]
    assert entries[5]["addendum_kind"] == "rescore"
    assert set(body["legacy"]) == {"chain_length", "chain_valid", "entries"}
    assert body["pending"] is None and body["warehouse"] is None
    # The /v1 alias serves the same view.
    assert client.get("/v1/audit/d-1", headers=AUTH).json()["entries"] == entries


def test_audit_doc_unknown_and_tampered(client):
    body = client.get("/audit/nope", headers=AUTH).json()
    assert (body["chain_length"], body["chain_valid"], body["entries"]) == (0, True, [])

    _write("d-2")
    import sqlite3

    path = db._resolve_url().split("///", 1)[1]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE doc_id = 'd-2'")
    assert client.get("/audit/d-2", headers=AUTH).json()["chain_valid"] is False


def test_audit_doc_shows_pending(client):
    from pipeline.bins import save_manifest
    from schemas.manifest import DocumentManifest

    m = DocumentManifest(doc_id="d-p", matter_id="M", original_filename="p.pdf")
    m.audit_written = "pending"
    m.audit_pending_refs = [{"sink": "sql", "ref": "pend-1", "complete": True}]
    save_manifest(m)
    body = client.get("/audit/d-p", headers=AUTH).json()
    assert body["pending"] == {"audit_written": "pending", "refs": [{"sink": "sql", "ref": "pend-1",
                                                                       "complete": True}]}


def test_audit_summary_rates(client):
    _write("a", attempts=[_att("a", "classification", 1), _att("a", "classification", 2, "retry")])
    _write("b", attempts=[_att("b", "classification", 1, "arbiter_rescue"), _att("b", "extraction", 1)],
           addenda=[("rescore", [])])
    _write("c", event="failed")
    _write("e", addenda=[("review_decision", []), ("review_decision", [])])
    r = client.get("/audit", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "total_entries" in body and "by_event" in body  # the legacy summary keeps its keys
    u = body["unified"]
    assert u["rows"] == 4
    assert u["rows_by_event"] == {"archived": 3, "failed": 1}
    assert u["rows_by_outcome"] == {"archived": 3, "failed": 1}
    assert u["addenda_by_kind"] == {"rescore": 1, "review_decision": 2}
    assert u["attempts_by_stage"] == {"classification": 3, "extraction": 1}
    assert (u["retry_rate"], u["rescue_rate"]) == (0.25, 0.25)
    assert (u["pending"], u["recovered"]) == (0, 0)
    assert u["checks"]["ok"] is True
    assert set(u["checks"]) >= {"main", "subrows", "addenda", "legacy", "trace_io", "pending"}


def test_analyze_script_prints_unified(client, capsys):
    import importlib.util
    from pathlib import Path

    _write("a")
    spec = importlib.util.spec_from_file_location(
        "analyze_audit_db_cli", Path(__file__).resolve().parents[1] / "scripts" / "analyze_audit_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    capsys.readouterr()
    assert mod.main(["--json"]) in (0, None)
    import json

    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert report["unified"]["rows"] == 1
