"""Roadmap M4 (bounded): verification of the unified audit storage.

The checks are storage-level: global row chain, attempt sub-row membership and
hashes, per-document addendum chains, the legacy per-document chain and stored
trace-I/O hashes. Pending rows/sinks are reported as uncollected, never as
proof. Every test runs against a real SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, text

from storage import db

FIXTURE = Path(__file__).parent / "fixtures" / "audit" / "legacy_audit.db"


@pytest.fixture
async def audit_db(tmp_path, monkeypatch) -> Path:
    """A fresh database explicitly migrated to the unified shape."""
    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    from storage import audit_migrate

    result = await audit_migrate.migrate_audit_log()
    assert result.reason == "no_legacy_rows"
    return path


def _q(path: Path, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def _attempt(n: int, stage: str = "extraction", tag: str = "", **detail):
    from audit.models import AttemptRow

    return AttemptRow(
        attempt_id=f"att-{stage}-{n}-{tag}",
        stage=stage,
        attempt_no=n,
        trigger="initial",
        outcome="ok",
        started_at=datetime(2026, 11, 3, 9, 0, n, tzinfo=timezone.utc),
        ended_at=datetime(2026, 11, 3, 9, 0, n + 1, tzinfo=timezone.utc),
        detail=detail or {"extracted_data": {"n": n}},
    )


def _row(doc_id: str, attempts=None, detail: dict | None = None):
    from audit.models import TerminalRow

    return TerminalRow(
        doc_id=doc_id,
        matter_id="M-1",
        event="archived",
        actor="archivist",
        detail=detail or {"row_schema": 1, "stage": "archive"},
        attempts=list(attempts or []),
    )


async def _seed_row_and_addendum(doc_id: str = "doc-v"):
    from audit.models import Addendum
    from audit.writer import write_addendum, write_terminal_row

    row = await write_terminal_row(
        _row(doc_id, attempts=[_attempt(1), _attempt(2, trigger="retry")])
    )
    addendum = await write_addendum(
        Addendum(
            doc_id=doc_id,
            kind="review_decision",
            actor="reviewer",
            detail={"decision": "approved"},
            attempts=[_attempt(1, stage="judge", tag="add")],
        )
    )
    return row, addendum


async def _insert_io(visit_id: str, doc_id: str, prompt: str, completion: str):
    from audit.verify import io_sha256
    from storage.audit_log import AuditTraceIORecord
    from storage.db import async_session

    async with async_session() as session:
        session.add(
            AuditTraceIORecord(
                visit_id=visit_id,
                doc_id=doc_id,
                prompt=prompt,
                completion=completion,
                sha256=io_sha256(prompt, completion),
            )
        )
        await session.commit()


async def test_clean_db_verifies(audit_db):
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    await _insert_io("visit-1", "doc-v", "prompt", "completion")

    report = await verify_audit_db()

    assert report.ok is True
    assert report.migrated is True
    assert (report.main.checked, report.main.failures) == (1, [])
    assert report.subrows.checked == 3 and report.subrows.ok
    assert report.addenda.checked == 1 and report.addenda.ok
    assert report.legacy.checked == 0 and report.legacy.ok
    assert report.trace_io.checked == 1 and report.trace_io.ok
    assert report.pending.collected is False
    assert "not collected" in (report.pending.note or "")


async def test_tampered_row_detail_fails_main(audit_db):
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ? WHERE doc_id = 'doc-v'",
            (json.dumps({"row_schema": 1, "stage": "tampered"}),),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.main.ok is False
    assert {f["kind"] for f in report.main.failures} == {"hash_mismatch"}
    # The addendum chain is independent of the row detail.
    assert report.addenda.ok is True


async def test_deleted_subrow_fails_subrows(audit_db):
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "DELETE FROM audit_attempts WHERE attempt_id = 'att-extraction-1-'"
        )

    report = await verify_audit_db()

    assert report.ok is False
    kinds = {f["kind"] for f in report.subrows.failures}
    assert kinds == {"missing_subrow"}
    assert report.main.ok is True  # the row hash still covers the listed hash


async def test_extra_subrow_fails_subrows(audit_db):
    from audit.writer import attempt_hash
    from audit.verify import verify_audit_db

    row, _ = await _seed_row_and_addendum()
    sub = {
        "attempt_id": "att-extra",
        "parent_id": row.entry_id,
        "doc_id": "doc-v",
        "stage": "extraction",
        "attempt_no": 9,
        "trigger": "initial",
        "outcome": "ok",
        "started_at": None,
        "ended_at": None,
        "detail": {},
    }
    digest = attempt_hash(sub, matter_id="M-1", actor="archivist")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "INSERT INTO audit_attempts (attempt_id, parent_id, doc_id, stage, attempt_no, "
            "trigger, outcome, started_at, ended_at, detail, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}', ?)",
            (
                sub["attempt_id"], sub["parent_id"], sub["doc_id"], sub["stage"],
                sub["attempt_no"], sub["trigger"], sub["outcome"], digest,
            ),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert {f["kind"] for f in report.subrows.failures} == {"not_listed_by_parent"}


async def test_edited_addendum_fails_addenda(audit_db):
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_addenda SET detail = ? WHERE doc_id = 'doc-v'",
            (json.dumps({"decision": "rejected"}),),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert {f["kind"] for f in report.addenda.failures} == {"hash_mismatch"}
    assert report.main.ok is True


@pytest.mark.parametrize(
    "field,statement",
    [
        ("stage", "UPDATE audit_attempts SET stage='judge' WHERE attempt_id='att-extraction-1-'"),
        ("attempt_no", "UPDATE audit_attempts SET attempt_no=99 WHERE attempt_id='att-extraction-1-'"),
        ("trigger", "UPDATE audit_attempts SET trigger='resume' WHERE attempt_id='att-extraction-1-'"),
        ("outcome", "UPDATE audit_attempts SET outcome='failed' WHERE attempt_id='att-extraction-1-'"),
        (
            "started_at",
            "UPDATE audit_attempts SET started_at='2026-11-03 10:00:00.000000' "
            "WHERE attempt_id='att-extraction-1-'",
        ),
        (
            "ended_at",
            "UPDATE audit_attempts SET ended_at='2026-11-03 10:00:00.000000' "
            "WHERE attempt_id='att-extraction-1-'",
        ),
        ("detail", "UPDATE audit_attempts SET detail='{\"tampered\": true}' WHERE attempt_id='att-extraction-1-'"),
        ("parent_id", "UPDATE audit_attempts SET parent_id='missing-parent' WHERE attempt_id='att-extraction-1-'"),
        ("doc_id", "UPDATE audit_attempts SET doc_id='other-doc' WHERE attempt_id='att-extraction-1-'"),
    ],
)
async def test_attempt_envelope_field_edits_fail_subrows(audit_db, field, statement):
    """Each envelope field is covered independently by the sub-row hash."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(statement)

    report = await verify_audit_db()

    assert report.subrows.ok is False
    kinds = {f["kind"] for f in report.subrows.failures}
    if field not in {"parent_id", "doc_id"}:
        assert "hash_mismatch" in kinds
    assert report.main.ok is True  # the row hash only covers the listed hashes


async def test_cross_parent_attempt_reference_fails_subrows(audit_db):
    """R1: a second parent cannot list another parent's attempt by hash.

    The old missing-subrow loop only checked global existence, so row B could
    list row A's attempt (correct hash) and verification still passed.
    """
    from audit.verify import verify_audit_db
    from audit.writer import get_row, hash_entry, write_terminal_row

    await write_terminal_row(_row("doc-a", attempts=[_attempt(1, tag="a1")]))
    await write_terminal_row(_row("doc-b"))
    row_a = await get_row("doc-a")
    row_b = await get_row("doc-b")
    sub = row_a["sub_rows"][0]
    detail_b = dict(row_b["detail"])
    detail_b["attempts"] = [
        {
            "attempt_id": sub["attempt_id"],
            "stage": sub["stage"],
            "attempt_no": sub["attempt_no"],
            "entry_hash": sub["entry_hash"],
        }
    ]
    # Recompute B's own hash so the MAIN chain stays valid; only membership is broken.
    digest = hash_entry(
        prev_hash=row_b["prev_hash"],
        doc_id="doc-b",
        entry_id=row_b["entry_id"],
        event=row_b["event"],
        detail=detail_b,
        matter_id=row_b["matter_id"],
        actor=row_b["actor"],
        timestamp=row_b["timestamp"],
    )
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ?, entry_hash = ? WHERE doc_id = 'doc-b'",
            (json.dumps(detail_b), digest),
        )

    report = await verify_audit_db()

    assert report.main.ok is True  # B's row hash was recomputed correctly
    assert report.subrows.ok is False
    assert {f["kind"] for f in report.subrows.failures} & {
        "attempt_referenced_twice",
        "reference_parent_mismatch",
    }


async def test_duplicate_attempt_reference_fails_subrows(audit_db):
    from audit.verify import verify_audit_db
    from audit.writer import get_row, hash_entry, write_terminal_row

    await write_terminal_row(_row("doc-a", attempts=[_attempt(1, tag="dup")]))
    row = await get_row("doc-a")
    sub = row["sub_rows"][0]
    ref = {
        "attempt_id": sub["attempt_id"],
        "stage": sub["stage"],
        "attempt_no": sub["attempt_no"],
        "entry_hash": sub["entry_hash"],
    }
    detail = dict(row["detail"])
    detail["attempts"] = [ref, dict(ref)]
    digest = hash_entry(
        prev_hash=row["prev_hash"],
        doc_id="doc-a",
        entry_id=row["entry_id"],
        event=row["event"],
        detail=detail,
        matter_id=row["matter_id"],
        actor=row["actor"],
        timestamp=row["timestamp"],
    )
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ?, entry_hash = ? WHERE doc_id = 'doc-a'",
            (json.dumps(detail), digest),
        )

    report = await verify_audit_db()

    assert report.main.ok is True
    assert report.subrows.ok is False
    assert {f["kind"] for f in report.subrows.failures} == {"duplicate_attempt_ref"}


async def test_non_object_detail_reports_failure(audit_db):
    """R2: detail=['oops'] must be a structured failure, not an AttributeError."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ? WHERE doc_id = 'doc-v'",
            (json.dumps(["oops"]),),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.main.ok is False
    assert "detail_not_object" in {f["kind"] for f in report.main.failures}


async def test_non_string_attempt_ref_id_reports_failure(audit_db):
    """R2: an unhashable attempt_id=[1] must fail, not raise TypeError."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    detail = {"attempts": [{"attempt_id": [1], "entry_hash": "x"}]}
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ? WHERE doc_id = 'doc-v'",
            (json.dumps(detail),),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.subrows.ok is False
    assert "attempt_ref_malformed" in {f["kind"] for f in report.subrows.failures}


async def test_invalid_attempt_timestamp_reports_failure(audit_db):
    """R2: an undecodable timestamp is a failed subrows section, not a crash."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_attempts SET started_at = 'not-a-date' "
            "WHERE attempt_id = 'att-extraction-1-'"
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.subrows.ok is False
    assert report.subrows.failures


async def test_invalid_json_decode_reports_failure(audit_db):
    """R2: raw invalid JSON must be a reported failure, never green/skip."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = 'not-json' WHERE doc_id = 'doc-v'"
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.main.ok is False
    assert any(f["kind"] == "verification_error" for f in report.main.failures)


@pytest.mark.parametrize("raw_detail", ["null", "[]", "0"])
async def test_non_object_attempt_detail_fails_subrows(audit_db, raw_detail):
    """R3: null/list/scalar attempt detail must fail even at the original hash.

    The writer envelope intentionally hashes falsy detail as {} for
    compatibility; verification must require an actual object instead of
    inheriting that coercion. The stored hash here was computed over {} so the
    old verifier would report a clean run.
    """
    from audit.verify import verify_audit_db
    from audit.writer import write_terminal_row

    attempt = _attempt(1, tag="empty")
    attempt.detail = {}
    await write_terminal_row(_row("doc-empty", attempts=[attempt]))
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_attempts SET detail = ? WHERE attempt_id = 'att-extraction-1-empty'",
            (raw_detail,),
        )

    report = await verify_audit_db()

    assert report.ok is False
    assert report.subrows.ok is False
    assert "detail_not_object" in {f["kind"] for f in report.subrows.failures}


@pytest.mark.parametrize("migrated", [False, True])
async def test_null_event_detail_is_not_verified(tmp_path, monkeypatch, migrated):
    """JSON null must not stand in for the empty object originally hashed."""
    from audit.verify import verify_audit_db
    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry
    from storage.audit_migrate import migrate_audit_log

    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    await write_audit_entry(build_audit_entry("doc-null", "matter", "ingested", "test", {}))
    if migrated:
        await migrate_audit_log()
    assert (await verify_audit_db()).ok
    table = "audit_log_legacy" if migrated else "audit_log"
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE {table} SET detail='null' WHERE doc_id='doc-null'")
    report = await verify_audit_db()
    assert not report.ok
    assert any(f["kind"] == "detail_not_object" for f in report.legacy.failures)


async def test_tampered_legacy_fails_legacy_only(tmp_path, monkeypatch):
    """Realistic pre-existing rows: the committed legacy fixture, migrated."""
    from audit.verify import verify_audit_db
    from storage import audit_migrate

    path = tmp_path / "mailroom.db"
    shutil.copy(FIXTURE, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    await audit_migrate.migrate_audit_log()

    clean = await verify_audit_db()
    assert clean.ok is True
    assert clean.legacy.checked == 3 and clean.legacy.ok  # 3 documents, 11 rows
    assert clean.main.checked == 0  # nothing terminal written yet

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE audit_log_legacy SET actor = 'mallory' WHERE entry_id = 'fixture-doc-b-03'"
        )

    report = await verify_audit_db()
    assert report.ok is False
    assert report.main.ok is True
    assert [f["doc_id"] for f in report.legacy.failures] == ["fixture-doc-b"]


async def test_io_sha_mismatch_fails_trace_io(audit_db):
    from audit.verify import verify_audit_db
    from storage.audit_log import AuditTraceIORecord
    from storage.db import async_session

    async with async_session() as session:
        session.add(
            AuditTraceIORecord(
                visit_id="bad-io", doc_id="doc-v", prompt="p", completion="c", sha256="0" * 64
            )
        )
        await session.commit()

    report = await verify_audit_db()

    assert report.ok is False
    assert report.trace_io.ok is False
    assert {f["kind"] for f in report.trace_io.failures} == {"io_sha_mismatch"}


async def test_recovered_row_detail_sha_checked(audit_db):
    from audit.verify import detail_sha256, verify_audit_db
    from audit.writer import write_terminal_row

    base = {"row_schema": 1, "stage": "archive", "attempts": []}
    detail = {**base, "recovered_from": {"sink": "sql", "detail_sha256": detail_sha256(base)}}
    await write_terminal_row(_row("doc-recovered", detail=detail))

    clean = await verify_audit_db()
    assert clean.ok is True

    tampered = {**detail, "stage": "failed"}
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_log SET detail = ? WHERE doc_id = 'doc-recovered'",
            (json.dumps(tampered),),
        )
    report = await verify_audit_db()
    assert report.ok is False
    assert "recovered_detail_mismatch" in {f["kind"] for f in report.main.failures}


async def test_pending_is_reported_as_uncollected(audit_db):
    from audit.verify import verify_audit_db

    report = await verify_audit_db(check_sinks=True)

    assert report.ok is True  # nothing collected is broken
    assert report.pending.collected is False
    assert report.pending.checked == 0
    assert "M9/M10" in (report.pending.note or "")


async def test_pre_migration_db_verifies_the_live_legacy_chain(tmp_path, monkeypatch):
    """Before the opt-in migration, audit_log IS the per-event chain."""
    from schemas.audit import build_audit_entry
    from storage.audit_log import get_latest_audit_hash, write_audit_entry
    from audit.verify import verify_audit_db

    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    prev = ""
    for event in ("classified", "extracted"):
        await write_audit_entry(
            build_audit_entry("doc-live", "matter", event, "test", {}, prev_hash=prev)
        )
        prev = await get_latest_audit_hash("doc-live")

    report = await verify_audit_db()

    assert report.ok is True
    assert report.migrated is False
    assert report.main.collected is False
    assert report.subrows.collected is False
    assert report.addenda.collected is False
    assert report.legacy.checked == 1 and report.legacy.ok


async def test_verification_reads_one_read_only_snapshot(audit_db):
    """The BEGIN must precede the first read, and the snapshot must not write."""
    from audit.verify import verify_audit_db

    await _seed_row_and_addendum()
    statements: list[str] = []
    engine = db.get_engine().sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        report = await verify_audit_db()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert report.ok is True
    assert statements, "no SQL captured"
    assert statements[0].strip().upper() == "BEGIN DEFERRED"
    first_read = next(i for i, sql in enumerate(statements) if sql.lstrip().upper().startswith("SELECT"))
    assert first_read == 1
    assert all(
        sql.lstrip().upper().startswith("SELECT")
        for sql in statements[1:]
    ), statements
