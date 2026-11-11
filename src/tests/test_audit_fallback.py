"""Roadmap M10: the write-failure fallback and ``audit-reconcile`` (spec §5.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from storage import db

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


class FakeSink:
    """An extra sink that keeps pending records in memory, with an optional payload limit."""

    def __init__(self, name="fake", max_payload_bytes=None):
        self.name = name
        self.max_payload_bytes = max_payload_bytes
        self.pending: dict[str, dict] = {}

    def record_io(self, visit_id, doc_id, prompt, completion):
        return None

    def capture_pending(self, record):
        ref = f"{self.name}-{len(self.pending) + 1}"
        self.pending[ref] = dict(record)
        return ref

    def fetch_pending(self):
        return [{"ref": ref, **rec} for ref, rec in self.pending.items()]

    def drop_pending(self, ref):
        self.pending.pop(ref, None)

    def fetch_io(self, ref):
        return None


@pytest.fixture
def audit_db(tmp_path, monkeypatch, temp_base_dir):
    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    monkeypatch.setattr("audit.writer.RETRY_DELAYS_S", (0, 0))
    assert db.ensure_schema()
    # The unified-audit migration is an operator step (never automatic); the
    # M10 fallback/reconcile tests run against a migrated database.
    import asyncio

    from storage import audit_migrate

    asyncio.run(audit_migrate.migrate_audit_log())
    return path


@pytest.fixture
def sinks(monkeypatch, audit_db):
    """``sql`` plus whatever fakes a test appends; ``active_sinks`` reads this list."""
    from audit.sinks.sql import SqlSink

    current = [SqlSink()]
    monkeypatch.setattr("audit.sinks.active_sinks", lambda: list(current))
    return current


@pytest.fixture
def db_down(monkeypatch):
    """While ``down[0]`` is truthy, every commit raises it (an exception instance)."""
    from audit import writer

    real = writer.write_terminal_row
    down: list = [RuntimeError("database is locked")]
    calls = {"n": 0}

    async def flaky(row, **kw):
        if down[0] is not None:
            calls["n"] += 1
            raise down[0]
        return await real(row, **kw)

    monkeypatch.setattr(writer, "write_terminal_row", flaky)
    down.append(calls)
    return down


def _row(doc_id="d-1", big=False):
    from audit.models import AttemptRow
    from audit.row import build_row

    row = build_row({"doc_id": doc_id, "matter_id": "M", "doc_type": "contract"}, event="archived",
                    actor="archivist")
    row.detail["trace"] = [{"visit_id": "v1", "node": "classify", "attempt_id": "a1", "status": "ok",
                            "outcome": "ok", "decisions": [{"kind": "class", "value": "contract"}],
                            "prompt": "x" * (20_000 if big else 10)}]
    if big:
        row.detail["extraction"] = {"extracted_data": {"blob": "y" * 20_000}}
    row.attempts = [AttemptRow(attempt_id=f"{doc_id}-a1", stage="classification", attempt_no=1, trigger="initial",
                               outcome="ok", started_at=T0, ended_at=T0,
                               detail={"nodes": ["classify"], "decisions": ["contract"], "raw": "z" * 50})]
    return row


def _save_manifest(doc_id="d-1"):
    from pipeline.bins import save_manifest
    from schemas.manifest import DocumentManifest

    save_manifest(DocumentManifest(doc_id=doc_id, matter_id="M", original_filename=f"{doc_id}.pdf"))


def _manifest(doc_id="d-1"):
    from pipeline.bins import load_manifest

    return load_manifest(doc_id)


@pytest.fixture
def logs(monkeypatch):
    """Every structlog event name logged by the audit modules."""
    seen: list[str] = []

    class Rec:
        def __getattr__(self, level):
            def log(event, *a, **kw):
                seen.append(event)
            return log

    for mod in ("audit.writer", "audit.reconcile", "audit.sinks.sql"):
        monkeypatch.setattr(f"{mod}.logger", Rec())
    return seen


async def test_three_failures_then_pending_capture(sinks, db_down, logs):
    from audit.writer import COMMIT_ATTEMPTS, commit_or_park

    out = await commit_or_park(_row())
    assert db_down[1]["n"] == COMMIT_ATTEMPTS == 3
    assert out.status == "pending" and out.result is None
    assert [r["sink"] for r in out.refs] == ["sql"] and out.refs[0]["complete"] is True
    [rec] = sinks[0].fetch_pending()
    for key in ("entry_id", "doc_id", "matter_id", "event", "actor", "detail", "sub_rows", "sub_row_hashes",
                "detail_sha256", "captured_at"):
        assert key in rec, key
    assert logs.count("audit_row_commit_failed") == 3 and "audit_pending" in logs


def test_manifest_marked_pending_and_run_succeeds(sinks, db_down):
    from audit.row import write_row

    _save_manifest()
    state = {"doc_id": "d-1", "matter_id": "M", "doc_type": "contract"}
    assert write_row(state, event="archived", actor="archivist") is None  # no exception: the run succeeds
    m = _manifest()
    assert m.audit_written == "pending"
    assert m.audit_pending_refs and m.audit_pending_refs[0]["sink"] == "sql"


async def test_oversize_payload_sends_decisions_only_plus_sha(sinks, db_down):
    from audit.writer import commit_or_park

    small = FakeSink("small", max_payload_bytes=8_000)
    sinks.append(small)
    out = await commit_or_park(_row(big=True))
    assert out.status == "pending"  # sql holds the complete copy
    assert {r["sink"]: r["complete"] for r in out.refs} == {"sql": True, "small": False}
    [full] = sinks[0].fetch_pending()
    [part] = small.fetch_pending()
    assert part["complete"] is False and part["full_detail_sha256"] == full["detail_sha256"]
    assert part["detail"]["extraction"] is None
    assert part["detail"]["trace"][0]["decisions"] == [{"kind": "class", "value": "contract"}]
    assert "prompt" not in part["detail"]["trace"][0]
    assert part["sub_rows"][0]["detail"] == {"nodes": ["classify"], "decisions": ["contract"]}


async def test_reconcile_writes_recovered_row_that_verifies(sinks, db_down, logs):
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    _save_manifest()
    await commit_or_park(_row())
    db_down[0] = None
    report = await reconcile()
    assert (report.recovered, report.skipped_existing, report.rejected_hash) == (1, 0, 0)
    row = await get_row("d-1")
    rf = row["detail"]["recovered_from"]
    assert rf["sink"] == "sql" and rf["ref"].startswith("pend-") and rf["captured_at"] and rf["detail_sha256"]
    assert "complete" not in rf
    assert len(row["sub_rows"]) == 1
    assert (await verify_audit_db()).ok
    assert sinks[0].fetch_pending() == []  # spent
    assert _manifest().audit_written is True
    assert "audit_row_recovered" in logs


async def test_reconcile_dry_run_writes_nothing(sinks, db_down):
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    await commit_or_park(_row())
    db_down[0] = None
    report = await reconcile(dry_run=True)
    assert report.recovered == 1 and report.dry_run
    assert await get_row("d-1") is None and len(sinks[0].fetch_pending()) == 1


async def test_reconcile_dedupes_across_sinks(sinks, db_down):
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    other = FakeSink("other")
    sinks.append(other)
    await commit_or_park(_row())
    assert len(other.pending) == 1 and len(sinks[0].fetch_pending()) == 1
    db_down[0] = None
    report = await reconcile()
    assert (report.recovered, report.skipped_existing) == (1, 0)
    assert len((await get_row("d-1"))["sub_rows"]) == 1
    assert other.pending == {} and sinks[0].fetch_pending() == []


async def test_reconcile_rejects_hash_mismatch(sinks, db_down, logs):
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    fake = FakeSink("fake")
    sinks[:] = [fake]
    await commit_or_park(_row())
    [ref] = fake.pending
    fake.pending[ref]["detail"]["doc_type"] = "tampered"
    db_down[0] = None
    report = await reconcile()
    assert (report.recovered, report.rejected_hash) == (0, 1)
    assert await get_row("d-1") is None
    assert "audit_reconcile_hash_mismatch" in logs

    # An edited sub-row is refused the same way.
    fake.pending.clear()
    db_down[0] = RuntimeError("down")
    await commit_or_park(_row("d-2"))
    [ref] = fake.pending
    fake.pending[ref]["sub_rows"][0]["detail"]["raw"] = "edited"
    db_down[0] = None
    assert (await reconcile()).rejected_hash == 1


async def test_reconcile_skips_doc_with_row(sinks, db_down):
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row, write_terminal_row

    await commit_or_park(_row())
    db_down[0] = None
    await write_terminal_row(_row())  # the row got written some other way meanwhile
    before = await get_row("d-1")
    report = await reconcile()
    assert (report.recovered, report.skipped_existing) == (0, 1)
    after = await get_row("d-1")
    assert after["entry_hash"] == before["entry_hash"] and after["addenda"] == before["addenda"]
    assert sinks[0].fetch_pending() == []


async def test_decisions_only_copy_reconciles_as_partial(sinks, db_down, logs):
    from audit.reconcile import reconcile
    from audit.row import mark_manifest
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    small = FakeSink("small", max_payload_bytes=8_000)
    sinks[:] = [small]
    _save_manifest()
    out = await commit_or_park(_row(big=True))
    mark_manifest("d-1", out.status, out.refs)
    assert out.status == "pending_partial" and _manifest().audit_written == "pending_partial"
    assert "audit_pending_partial" in logs
    db_down[0] = None
    report = await reconcile()
    assert (report.recovered, report.partial) == (0, 1)
    rf = (await get_row("d-1"))["detail"]["recovered_from"]
    assert rf["complete"] is False and rf["full_detail_sha256"]
    assert "audit_gap_partial" in logs
    assert (await verify_audit_db()).ok
    assert len(small.pending) == 1  # kept: a complete copy may still turn up


async def test_complete_copy_after_partial_row_becomes_addendum(sinks, db_down, logs):
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    small = FakeSink("small", max_payload_bytes=8_000)
    late = FakeSink("late")
    sinks[:] = [small, late]
    await commit_or_park(_row(big=True))
    held = late.pending.copy()
    late.pending.clear()  # the complete copy is out of reach on the first run
    db_down[0] = None

    assert (await reconcile()).partial == 1
    row = await get_row("d-1")
    rf = row["detail"]["recovered_from"]

    # A complete copy with a different hash is refused.
    from audit.writer import pending_record

    stranger = pending_record(_row(big=True))
    late.pending["late-x"] = stranger
    report = await reconcile()
    assert (report.gap_closed, report.rejected_hash) == (0, 1)

    late.pending.clear()
    late.pending.update(held)
    [copy] = held.values()
    assert copy["detail_sha256"] == rf["full_detail_sha256"]
    report = await reconcile()
    assert report.gap_closed == 1 and "audit_gap_closed" in logs
    after = await get_row("d-1")
    assert after["entry_hash"] == row["entry_hash"]  # the row itself is untouched
    [add] = after["addenda"]
    assert add["kind"] == "recovered_detail" and add["seq"] == 1
    assert add["prev_hash"] == row["entry_hash"]
    assert add["detail"]["detail"]["extraction"] == {"extracted_data": {"blob": "y" * 20_000}}
    assert (await verify_audit_db()).ok

    third = await reconcile()
    assert (third.gap_closed, third.recovered, third.partial) == (0, 0, 0)
    assert len((await get_row("d-1"))["addenda"]) == 1


async def test_no_sink_accepts_logs_audit_gap(sinks, db_down, logs):
    from audit.writer import commit_or_park

    class Refuses(FakeSink):
        def capture_pending(self, record):
            raise OSError("disk full")

    sinks[:] = [Refuses()]
    out = await commit_or_park(_row())
    assert out.status == "gap" and out.refs == ()
    assert "audit_gap" in logs


def test_ephemeral_spool_warning(monkeypatch, tmp_path, logs):
    from audit.sinks.sql import warn_if_ephemeral

    data = tmp_path / "data"
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(data))
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(data / "audit_spool.db"))
    assert warn_if_ephemeral() is False and logs == []
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "elsewhere" / "audit_spool.db"))
    assert warn_if_ephemeral() is True and logs == ["audit_spool_ephemeral"]


async def test_migration_pending_takes_fallback(sinks, db_down):
    from audit.models import AuditMigrationPending
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    db_down[0] = AuditMigrationPending("audit migration has not run")
    out = await commit_or_park(_row())
    assert out.status == "pending" and db_down[1]["n"] == 3
    db_down[0] = None  # the migration ran
    assert (await reconcile()).recovered == 1
    assert (await get_row("d-1"))["detail"]["recovered_from"]["sink"] == "sql"


def test_cli_dry_run_json(sinks, db_down, capsys):
    import asyncio
    import importlib.util
    import json

    from audit.writer import commit_or_park

    asyncio.run(commit_or_park(_row()))
    db_down[0] = None
    spec = importlib.util.spec_from_file_location(
        "audit_reconcile_cli", Path(__file__).resolve().parents[1] / "scripts" / "audit_reconcile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    capsys.readouterr()  # drop the setup's log lines
    assert mod.main(["--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recovered"] == 1 and out["dry_run"] is True
