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


def _client(monkeypatch, tmp_path, *, migrate):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mailroom.db'}")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    if migrate:
        # M11 readers: the unified row exists only after the explicit migration.
        from storage import audit_migrate

        asyncio.run(audit_migrate.migrate_audit_log())
    if "api.main" in sys.modules:
        importlib.reload(sys.modules["api.main"])
    from api.main import app

    return TestClient(app)


@pytest.fixture
def client(monkeypatch, temp_base_dir, tmp_path):
    with _client(monkeypatch, tmp_path, migrate=True) as c:
        yield c


@pytest.fixture
def unmigrated_client(monkeypatch, temp_base_dir, tmp_path):
    """The default state of an existing deployment: per-event rows only, no migration."""
    with _client(monkeypatch, tmp_path, migrate=False) as c:
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
    assert set(body) == {"doc_id", "chain_length", "chain_valid", "entries", "unified", "pending", "warehouse"}
    # The existing keys keep their original meaning: the per-event chain (none written for d-1).
    assert body["doc_id"] == "d-1" and body["chain_length"] == 0 and body["chain_valid"] is True
    assert body["entries"] == []
    unified = body["unified"]
    assert set(unified) == {"migrated", "chain_length", "chain_valid", "entries"}
    assert unified["migrated"] is True and unified["chain_length"] == 3 and unified["chain_valid"] is True
    entries = unified["entries"]
    assert [e["kind"] for e in entries] == ["row", "attempt", "attempt", "addendum", "attempt", "addendum"]
    assert all("parent_id" in e for e in entries)
    row_id = entries[0]["entry_id"]
    assert entries[0]["parent_id"] is None
    assert entries[1]["parent_id"] == entries[2]["parent_id"] == row_id
    assert entries[3]["parent_id"] == row_id and entries[3]["addendum_kind"] == "review_decision"
    assert entries[4]["parent_id"] == entries[3]["addendum_id"]
    assert entries[5]["addendum_kind"] == "rescore"
    assert body["pending"] is None and body["warehouse"] is None
    # The /v1 alias serves the same view.
    assert client.get("/v1/audit/d-1", headers=AUTH).json()["unified"]["entries"] == entries


def test_audit_doc_unknown_and_tampered(client):
    body = client.get("/audit/nope", headers=AUTH).json()
    assert (body["chain_length"], body["chain_valid"], body["entries"]) == (0, True, [])
    assert (body["unified"]["chain_length"], body["unified"]["chain_valid"], body["unified"]["entries"]) == (0, True, [])

    _write("d-2")
    import sqlite3

    path = db._resolve_url().split("///", 1)[1]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE doc_id = 'd-2'")
    assert client.get("/audit/d-2", headers=AUTH).json()["unified"]["chain_valid"] is False


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
    assert "unified" not in r.json()  # opt-in: the default call costs what it did before the unified row
    r = client.get("/audit?unified=true", headers=AUTH)
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


def test_analyze_no_verify_skips_unified_checks_and_exit_status(client, capsys, tmp_path):
    """T25 (#229): ``--no-verify`` skips the unified verifier too, and ``unified.checks.ok`` drives the exit status."""
    import importlib.util
    import json
    import sqlite3
    from pathlib import Path

    _write("a")
    with sqlite3.connect(tmp_path / "mailroom.db") as conn:
        conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE doc_id = 'a'")
    spec = importlib.util.spec_from_file_location(
        "analyze_audit_db_cli_nv", Path(__file__).resolve().parents[1] / "scripts" / "analyze_audit_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def run(*flags):
        capsys.readouterr()
        code = mod.main(["--json", *flags])
        out = capsys.readouterr().out
        return code, json.loads(out[out.index("{"):])

    code, report = run()
    assert code == 1 and report["unified"]["checks"]["ok"] is False
    code, report = run("--no-verify")
    assert code == 0 and report["unified"]["checks"] is None

    help_text = " ".join(mod.__doc__.split())
    assert "--no-verify" in help_text and "unified" in help_text and "Exit status" in help_text


@pytest.mark.parametrize("stored", [
    '[1, 2]',  # the whole detail is not an object
    '{"attempts": 5}',  # attempts is not a list
    '{"attempts": "x"}',
    '{"attempts": [3, {"attempt_id": ["unhashable"], "entry_hash": "h"}, {"attempt_id": "a", "entry_hash": 7}]}',
])
def test_audit_doc_with_malformed_parent_detail_is_invalid_not_a_500(client, stored):
    """Review thread T17: a tampered parent ``detail`` fails the check, as ``verify.py`` does, and never raises."""
    _write("d-m", attempts=[_att("d-m", "classification", 1)])
    import sqlite3

    path = db._resolve_url().split("///", 1)[1]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_log SET detail = ? WHERE doc_id = 'd-m'", (stored,))
    resp = client.get("/audit/d-m", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["unified"]["chain_valid"] is False
    assert asyncio.run(_doc_valid("d-m")) is False


async def _doc_valid(doc_id):
    from audit.readers import doc_valid

    return await doc_valid(doc_id)


@pytest.mark.parametrize("on_addendum", [False, True])
def test_document_chain_detects_deleted_attempt(client, on_addendum):
    from sqlalchemy import delete
    from storage.audit_log import AuditAttemptRecord
    from storage.db import async_session
    from audit.verify import verify_audit_db

    att = _att("d-missing", "extraction", 1)
    _write("d-missing", attempts=[] if on_addendum else [att],
           addenda=[("resumed", [att])] if on_addendum else [])
    assert client.get("/audit/d-missing", headers=AUTH).json()["unified"]["chain_valid"] is True

    async def remove():
        async with async_session() as session:
            await session.execute(delete(AuditAttemptRecord).where(AuditAttemptRecord.doc_id == "d-missing"))
            await session.commit()

    asyncio.run(remove())
    assert asyncio.run(verify_audit_db()).ok is False
    assert client.get("/audit/d-missing", headers=AUTH).json()["unified"]["chain_valid"] is False


def _legacy_entry(doc, event="archived"):
    """A per-event audit entry as the previous release wrote it (written before any migration)."""
    from schemas.audit import build_audit_entry
    from storage.audit_log import get_latest_audit_hash, write_audit_entry

    async def go():
        prev = await get_latest_audit_hash(doc)
        entry = build_audit_entry(doc, "M", event, "archivist", {"note": event}, prev_hash=prev)
        await write_audit_entry(entry)

    asyncio.run(go())


def test_audit_doc_keeps_per_event_keys_on_an_unmigrated_database(unmigrated_client):
    """Regression (PR #239 review): ``entries`` / ``chain_length`` / ``chain_valid`` stay the per-event
    chain on the default, unmigrated database; the unified view is only under the new ``unified`` key."""
    _legacy_entry("d-old")
    body = unmigrated_client.get("/audit/d-old", headers=AUTH).json()
    assert body["chain_length"] == 1 and body["chain_valid"] is True
    assert [e["event"] for e in body["entries"]] == ["archived"]
    assert body["entries"][0]["detail"] == {"note": "archived"}
    assert body["unified"] == {"migrated": False, "chain_length": 0, "chain_valid": True, "entries": []}


def test_audit_doc_per_event_history_survives_the_migration(unmigrated_client):
    from storage import audit_migrate

    _legacy_entry("d-old")
    _legacy_entry("d-old", event="review_completed")
    asyncio.run(audit_migrate.migrate_audit_log())
    _write("d-old")
    body = unmigrated_client.get("/audit/d-old", headers=AUTH).json()
    assert body["chain_length"] == 2 and body["chain_valid"] is True
    assert [e["event"] for e in body["entries"]] == ["archived", "review_completed"]
    assert body["unified"]["migrated"] is True and body["unified"]["chain_length"] == 1
    assert body["unified"]["entries"][0]["kind"] == "row"


def test_audit_summary_unified_is_opt_in(client):
    _write("a")
    default = client.get("/audit", headers=AUTH).json()
    assert "unified" not in default and "total_entries" in default
    assert client.get("/audit?unified=true&verify=false", headers=AUTH).json()["unified"]["checks"] is None


def _count_statements(fn):
    """Run ``fn`` and return how many SQL statements it issued."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    # Class-level: the engine is rebuilt for every ``asyncio.run`` loop, so none can be listened to by instance.
    event.listen(Engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    return len(seen)


def test_unified_summary_queries_do_not_grow_with_the_number_of_documents(client):
    """Regression (PR #239 review): no per-document ``get_row`` loop behind ``GET /audit?unified=true``."""
    from audit.readers import unified_summary

    def run():
        return asyncio.run(unified_summary(verify=False))

    for i in range(2):
        _write(f"few-{i}", attempts=[_att(f"few-{i}", "classification", 1)])
    few = _count_statements(run)
    for i in range(12):
        _write(f"many-{i}", attempts=[_att(f"many-{i}", "classification", 1)], addenda=[("rescore", [])])
    many = _count_statements(run)
    assert run()["rows"] == 14
    assert few > 0 and many == few, (few, many)


def test_all_rows_match_get_row_and_page(client):
    from audit.readers import all_rows
    from audit.writer import get_row

    for i in range(3):
        _write(f"p-{i}", attempts=[_att(f"p-{i}", "extraction", 1)], addenda=[("rescore", [_att(f"p-{i}", "extraction", 2)])])
    rows = asyncio.run(all_rows())
    assert [r["doc_id"] for r in rows] == ["p-0", "p-1", "p-2"]
    for row in rows:
        assert row == asyncio.run(get_row(row["doc_id"]))
    assert [r["doc_id"] for r in asyncio.run(all_rows(limit=1, offset=1))] == ["p-1"]
    assert asyncio.run(all_rows(limit=1, offset=1))[0] == asyncio.run(get_row("p-1"))


def _rewrite(doc_id, mutate):
    """Edit a document's stored rows, then re-hash them so only a rule other than the hash can object."""
    from sqlalchemy import select

    from audit import verify
    from storage.audit_log import AuditAddendumRecord, AuditAttemptRecord, AuditLogRecord
    from storage.db import async_session

    async def go():
        async with async_session() as session:
            row = (await session.execute(select(AuditLogRecord).where(AuditLogRecord.doc_id == doc_id))).scalar_one()
            adds = (await session.execute(
                select(AuditAddendumRecord).where(AuditAddendumRecord.doc_id == doc_id)
                .order_by(AuditAddendumRecord.seq))).scalars().all()
            subs = (await session.execute(
                select(AuditAttemptRecord).where(AuditAttemptRecord.doc_id == doc_id))).scalars().all()
            mutate(row, adds, subs)
            from audit.writer import attempt_hash

            for sub in subs:
                sub.entry_hash = attempt_hash(verify._attempt_sub(sub), matter_id=row.matter_id, actor=row.actor)
            row.entry_hash = verify._row_hash(row)
            prev = row.entry_hash
            for add in adds:
                add.prev_hash = prev if add.prev_hash == "RELINK" else add.prev_hash
                add.entry_hash = verify._addendum_hash(add, matter_id=row.matter_id)
                prev = add.entry_hash
            await session.commit()

    asyncio.run(go())


def _set_event(row, adds, subs):
    row.event = "bogus"


def _break_row_link(row, adds, subs):
    row.prev_hash = "f" * 64


def _unknown_stage(row, adds, subs):
    subs[0].stage = "not-a-stage"


def _unknown_trigger(row, adds, subs):
    subs[0].trigger = "not-a-trigger"


def _addendum_seq_gap(row, adds, subs):
    adds[0].seq = 3


def _unknown_addendum_kind(row, adds, subs):
    adds[0].kind = "not-a-kind"


def _unbound_recovered_detail(row, adds, subs):
    adds[0].kind = "recovered_detail"
    adds[0].detail = {"recovered_from": {"detail_sha256": "0" * 64}, "detail": {"x": 1}}


@pytest.mark.parametrize("mutate", [_set_event, _break_row_link, _unknown_stage, _unknown_trigger,
                                    _addendum_seq_gap, _unknown_addendum_kind, _unbound_recovered_detail])
def test_doc_validity_matches_the_full_verifier(client, mutate):
    """Regression (PR #239 review): every rule the full verifier applies to a document, ``chain_valid``
    applies too. Each edit is re-hashed, so only the verifier's non-hash rules can reject it."""
    from audit.verify import verify_audit_db

    _write("first", attempts=[_att("first", "extraction", 1)])
    _write("d-v", attempts=[_att("d-v", "extraction", 1)], addenda=[("rescore", [])])
    assert client.get("/audit/d-v", headers=AUTH).json()["unified"]["chain_valid"] is True
    _rewrite("d-v", mutate)
    assert asyncio.run(verify_audit_db()).ok is False
    assert client.get("/audit/d-v", headers=AUTH).json()["unified"]["chain_valid"] is False
    assert client.get("/audit/first", headers=AUTH).json()["unified"]["chain_valid"] is True


def test_doc_validity_is_scoped_to_the_document(client):
    """A broken later document does not fail the healthy ones before it; re-hashing a row breaks the next row's link."""
    _write("a")
    _write("b")
    _write("c")
    _rewrite("b", _set_event)
    assert client.get("/audit/a", headers=AUTH).json()["unified"]["chain_valid"] is True
    assert client.get("/audit/b", headers=AUTH).json()["unified"]["chain_valid"] is False
    # b's stored hash changed, so c no longer links to it: the document-scoped check sees that too.
    assert client.get("/audit/c", headers=AUTH).json()["unified"]["chain_valid"] is False


def _verify_cli(monkeypatch, capsys, *argv):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_audit_chains.py"
    spec = importlib.util.spec_from_file_location("verify_audit_chains_cli_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["verify_audit_chains.py", *argv])
    capsys.readouterr()
    code = mod.main()
    return code, capsys.readouterr().out


def test_verify_cli_doc_checks_the_unified_row_too(client, monkeypatch, capsys):
    """Regression (PR #239 review): ``--doc`` read only the per-event chain, so a document with a (broken or
    healthy) unified row and no per-event entries printed "no audit entries" and exited 0."""
    _write("d-cli", attempts=[_att("d-cli", "extraction", 1)], addenda=[("rescore", [])])
    code, out = _verify_cli(monkeypatch, capsys, "--doc", "d-cli")
    assert code == 0 and "no audit entries" not in out and "unified main chain: checked=1" in out
    _rewrite("d-cli", _set_event)  # re-hashed: only the verifier's event rule rejects it
    code, out = _verify_cli(monkeypatch, capsys, "--doc", "d-cli")
    assert code == 1 and "BROKEN" in out and "unknown_event" in out
    code, out = _verify_cli(monkeypatch, capsys, "--doc", "never-seen")
    assert code == 0 and "no audit entries for never-seen" in out


def test_verify_cli_warns_on_pending_and_can_fail_on_it(client, monkeypatch, capsys):
    from pipeline.bins import save_manifest
    from schemas.manifest import DocumentManifest

    _write("d-ok")
    m = DocumentManifest(doc_id="d-park", matter_id="M", original_filename="p.pdf")
    m.audit_written = "pending_partial"
    save_manifest(m)

    code, out = _verify_cli(monkeypatch, capsys)
    assert code == 0 and "WARNING: 1 manifest(s) are parked" in out  # the documented exit status is unchanged
    code, out = _verify_cli(monkeypatch, capsys, "--fail-on-pending")
    assert code == 1 and "WARNING" in out
    code, out = _verify_cli(monkeypatch, capsys, "--json")
    assert code == 0 and '"pending_manifests": 1' in out
    code, out = _verify_cli(monkeypatch, capsys, "--doc", "d-park")  # a parked doc has no stored row to find
    assert code == 0 and "d-park is parked as pending_partial" in out
    code, _ = _verify_cli(monkeypatch, capsys, "--doc", "d-park", "--fail-on-pending")
    assert code == 1


def test_audit_doc_orphaned_addendum_is_invalid_on_a_migrated_database(client):
    """Review (PR #298): with no unified row, ``chain_valid`` was hard-wired True; orphans must be caught."""
    import sqlite3

    _write("d-orphan", addenda=[("rescore", [])])
    path = db._resolve_url().split("///", 1)[1]
    with sqlite3.connect(path) as conn:  # raw connection: foreign keys off, so the row alone can be removed
        conn.execute("DELETE FROM audit_log WHERE doc_id = 'd-orphan'")
    unified = client.get("/audit/d-orphan", headers=AUTH).json()["unified"]
    assert unified["entries"] == [] and unified["chain_valid"] is False
    assert client.get("/audit/never-seen", headers=AUTH).json()["unified"]["chain_valid"] is True


def test_unreadable_manifest_is_unavailable_and_fails_on_pending(client, monkeypatch, capsys):
    """Review (PR #298): an unreadable manifest was reported as "nothing pending"."""
    from pipeline.bins import manifests_dir

    (manifests_dir() / "d-bad.json").write_text("{not json")
    assert client.get("/audit/d-bad", headers=AUTH).json()["pending"]["unavailable"] is True
    _write("d-ok")
    code, out = _verify_cli(monkeypatch, capsys)
    assert code == 0 and "could not be read" in out
    code, out = _verify_cli(monkeypatch, capsys, "--fail-on-pending")
    assert code == 1 and "could not be read" in out
    code, _ = _verify_cli(monkeypatch, capsys, "--doc", "d-bad", "--fail-on-pending")
    assert code == 1


@pytest.mark.parametrize("seq", [7, 0])
def test_doc_validity_needs_genesis_seq_one_or_an_existing_predecessor(client, seq):
    """Review (PR #298): a scoped check must not accept a row whose predecessor is missing or whose genesis seq is not 1."""
    from audit.verify import verify_audit_db

    _write("a")
    _write("b")

    def detach(row, adds, subs):
        row.seq, row.prev_hash = seq, ""

    _rewrite("b", detach)
    assert asyncio.run(verify_audit_db()).ok is False
    assert client.get("/audit/b", headers=AUTH).json()["unified"]["chain_valid"] is False
