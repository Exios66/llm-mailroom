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


async def test_reconcile_skips_a_row_a_concurrent_writer_commits_after_the_check(sinks, db_down, monkeypatch):
    """T6 (#229): two reconciles can both see no row; the loser must not append ``resumed``."""
    from audit import writer
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row, write_terminal_row

    await commit_or_park(_row())
    db_down[0] = None
    real_get_row = get_row

    async def racing_get_row(doc_id):
        seen = await real_get_row(doc_id)  # reconcile's read: no row yet ...
        if seen is None:
            await write_terminal_row(_row(doc_id))  # ... then another writer commits it
        return seen

    monkeypatch.setattr(writer, "get_row", racing_get_row)
    report = await reconcile()
    row = await real_get_row("d-1")
    assert row["addenda"] == []
    assert (report.recovered, report.partial, report.skipped_existing) == (0, 0, 1)
    assert report.docs == [{"doc_id": "d-1", "action": "skipped_existing"}]


async def test_two_concurrent_reconciles_write_one_row_and_no_addendum(sinks, db_down):
    import asyncio

    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    await commit_or_park(_row())
    db_down[0] = None
    first, second = await asyncio.gather(reconcile(), reconcile())
    assert first.recovered + second.recovered == 1
    assert (await get_row("d-1"))["addenda"] == []


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


async def test_complete_copy_only_in_write_only_sinks_is_not_reported_pending(sinks, db_down, logs):
    """T9 (#229) and T19: ``audit-reconcile`` cannot read a write-only sink, so ``pending`` would never clear.

    The copy still exists, so the row is ``pending_partial`` with the write-only refs listed, not a ``gap``.
    """
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    class WriteOnly(FakeSink):
        recoverable = False

        def fetch_pending(self):
            return []

    external = WriteOnly("phoenix")
    sinks[:] = [external]
    out = await commit_or_park(_row())
    assert len(external.pending) == 1
    assert out.status == "pending_partial"
    assert [r["sink"] for r in out.refs] == ["phoenix"]  # the copy is still named for an operator
    assert out.refs[0]["complete"] is False and out.refs[0]["recoverable"] is False
    assert "audit_pending_partial" in logs and "audit_pending" not in logs and "audit_gap" not in logs
    db_down[0] = None
    assert (await reconcile()).recovered == 0 and await get_row("d-1") is None


async def test_readable_decisions_only_copy_beats_write_only_complete_copy(sinks, db_down):
    from audit.writer import commit_or_park

    class WriteOnly(FakeSink):
        recoverable = False

        def fetch_pending(self):
            return []

    sinks[:] = [WriteOnly("phoenix"), FakeSink("tiny", max_payload_bytes=12_000)]
    out = await commit_or_park(_row(big=True))
    assert out.status == "pending_partial"
    assert {r["sink"]: r["recoverable"] for r in out.refs} == {"phoenix": False, "tiny": True}


async def test_complete_copy_in_a_readable_sink_stays_pending(sinks, db_down):
    from audit.writer import commit_or_park

    class WriteOnly(FakeSink):
        recoverable = False

        def fetch_pending(self):
            return []

    sinks[:] = [WriteOnly("phoenix"), sinks[0]]
    assert (await commit_or_park(_row())).status == "pending"


async def test_migration_pending_takes_fallback(sinks, db_down, monkeypatch):
    from audit.models import AuditMigrationPending
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    import time

    from audit import writer
    # A migration that has not run is deterministic: one attempt, no backoff (#239 review).
    monkeypatch.setattr(writer, "RETRY_DELAYS_S", (5.0, 5.0))
    db_down[0] = AuditMigrationPending("audit migration has not run")
    started = time.monotonic()
    out = await commit_or_park(_row())
    assert out.status == "pending" and db_down[1]["n"] == 1
    assert time.monotonic() - started < 2.0
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


def test_cli_json_stdout_is_only_the_report_when_reconcile_logs(sinks, db_down, tmp_path, monkeypatch):
    """T26 (#229): a recovered row logs ``audit_row_recovered``; ``--json`` stdout must still parse."""
    import asyncio
    import json
    import os
    import subprocess
    import sys

    from audit.writer import commit_or_park

    asyncio.run(commit_or_park(_row()))
    db_down[0] = None
    src = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(src), "LOG_FORMAT": "json", "LOG_LEVEL": "INFO"}
    env.pop("LOG_FILE", None)
    done = subprocess.run([sys.executable, str(src / "scripts" / "audit_reconcile.py"), "--json"],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["recovered"] == 1  # stdout is the JSON report and nothing else
    logged = [json.loads(line) for line in done.stderr.splitlines() if line.startswith("{")]
    assert "audit_row_recovered" in [e.get("event") for e in logged]  # the logs went to stderr, level kept


async def test_row_held_only_by_write_only_sinks_is_not_reported_pending(sinks, db_down, logs):
    """Review thread T19: ``audit-reconcile`` cannot read a write-only copy back, so ``pending`` would be a promise nobody keeps."""
    from audit.writer import commit_or_park

    class WriteOnly(FakeSink):
        recoverable = False

        def fetch_pending(self):
            return []

    class SqlDown(FakeSink):
        def capture_pending(self, record):
            raise OSError("spool disk full")

    sinks[:] = [SqlDown("sql"), WriteOnly("langfuse")]
    out = await commit_or_park(_row())
    assert out.status != "pending"
    assert out.status == "pending_partial" and [r["sink"] for r in out.refs] == ["langfuse"]
    assert "audit_pending" not in logs


# ---------------------------------------------------------------- a parked repeat terminal write (review fix R2)


def _second_run(doc_id="d-1"):
    """A later terminal write of the same document: its own trace, so its detail differs from ``_row``."""
    row = _row(doc_id)
    row.detail["trace"][0].update({"visit_id": "v-second", "prompt": "second run only"})
    return row


async def _row_then_parked_repeat(db_down):
    """d-1 has a row (manifest written); a second terminal write fails 3x and parks."""
    from audit.row import mark_manifest
    from audit.writer import commit_or_park

    _save_manifest()
    db_down[0] = None
    first = await commit_or_park(_row())
    assert first.status == "written"
    mark_manifest("d-1", first.status, first.refs)
    assert _manifest().audit_written is True
    db_down[0] = RuntimeError("database is locked")
    second = await commit_or_park(_second_run())
    assert second.status == "pending"
    mark_manifest("d-1", second.status, second.refs)
    assert _manifest().audit_written == "pending"
    db_down[0] = None


async def test_reconcile_writes_a_parked_repeat_terminal_write_as_a_resumed_addendum(sinks, db_down, logs):
    """The live writer turns a repeat terminal write into a ``resumed`` addendum; so must reconcile.

    Regression: the parked copy was dropped unwritten and the manifest stayed ``pending`` forever.
    """
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import get_row

    await _row_then_parked_repeat(db_down)
    before = await get_row("d-1")
    report = await reconcile()
    assert (report.recovered, report.resumed, report.skipped_existing) == (0, 1, 0)
    assert report.docs == [{"doc_id": "d-1", "action": "resumed"}]
    after = await get_row("d-1")
    assert after["entry_hash"] == before["entry_hash"]  # the row is immutable
    [add] = after["addenda"]
    assert add["kind"] == "resumed" and add["actor"] == "archivist" and add["detail"]["event"] == "archived"
    assert add["detail"]["trace"][0]["prompt"] == "second run only"  # the data of the second run survived
    assert add["detail"]["recovered_from"]["sink"] == "sql" and add["detail"]["recovered_from"]["detail_sha256"]
    assert len(add["sub_rows"]) == 1
    assert sinks[0].fetch_pending() == []  # spent
    assert _manifest().audit_written is True and _manifest().audit_pending_refs == []
    assert (await verify_audit_db()).ok
    assert "audit_repeat_write_recovered" in logs


async def test_reconcile_does_not_write_a_repeat_copy_twice(sinks, db_down):
    """The same parked copy turning up again (a drop that failed, a second sink) adds no second addendum."""
    from audit.reconcile import reconcile
    from audit.writer import get_row

    await _row_then_parked_repeat(db_down)
    [spooled] = sinks[0].fetch_pending()
    spooled = {k: v for k, v in spooled.items() if k not in ("ref", "_sink")}
    await reconcile()
    # The copy is back (the drop did not take, or another sink held it too) and the manifest is stale.
    sinks[0].capture_pending(spooled)
    from audit.row import mark_manifest

    mark_manifest("d-1", "pending", ())
    report = await reconcile()
    assert (report.resumed, report.skipped_existing) == (0, 1)
    assert len((await get_row("d-1"))["addenda"]) == 1
    assert sinks[0].fetch_pending() == []
    assert _manifest().audit_written is True


async def test_reconcile_sets_the_manifest_when_the_row_was_written_but_the_mark_never_ran(sinks, db_down):
    """The crash window: the recovered row is in the DB, the copy is still parked, the manifest says pending."""
    from audit.reconcile import reconcile
    from audit.row import mark_manifest
    from audit.writer import commit_or_park, get_row

    _save_manifest()
    out = await commit_or_park(_row())
    mark_manifest("d-1", out.status, out.refs)
    [spooled] = sinks[0].fetch_pending()
    db_down[0] = None
    await reconcile()
    row = await get_row("d-1")
    assert row["detail"]["recovered_from"]["detail_sha256"] == spooled["detail_sha256"]
    # Rewind to the crash: copy parked again, manifest back to pending.
    sinks[0].capture_pending({k: v for k, v in spooled.items() if k not in ("ref", "_sink")})
    mark_manifest("d-1", "pending", out.refs)
    report = await reconcile()
    assert (report.recovered, report.resumed, report.skipped_existing) == (0, 0, 1)
    assert len((await get_row("d-1"))["addenda"]) == 0
    assert _manifest().audit_written is True and _manifest().audit_pending_refs == []
    assert sinks[0].fetch_pending() == []


async def test_reconcile_dry_run_counts_a_repeat_write_without_writing_it(sinks, db_down):
    from audit.reconcile import reconcile
    from audit.writer import get_row

    await _row_then_parked_repeat(db_down)
    report = await reconcile(dry_run=True)
    assert report.dry_run and (report.resumed, report.recovered, report.skipped_existing) == (1, 0, 0)
    assert (await get_row("d-1"))["addenda"] == []
    assert len(sinks[0].fetch_pending()) == 1 and _manifest().audit_written == "pending"


async def test_two_parked_writes_with_no_row_become_a_row_and_a_resumed_addendum(sinks, db_down):
    """Both terminal writes of a document parked: the earlier is the row, the later is not dropped."""
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    _save_manifest()
    await commit_or_park(_row())
    await commit_or_park(_second_run())
    assert len(sinks[0].fetch_pending()) == 2
    db_down[0] = None
    report = await reconcile()
    assert (report.recovered, report.resumed) == (1, 1)
    row = await get_row("d-1")
    assert row["detail"]["trace"][0]["visit_id"] == "v1"
    [add] = row["addenda"]
    assert add["kind"] == "resumed" and add["detail"]["trace"][0]["visit_id"] == "v-second"
    assert sinks[0].fetch_pending() == [] and _manifest().audit_written is True
    assert (await verify_audit_db()).ok


# ---------------------------------------------------------------- a partial recovery keeps the gap on the manifest (review fix R3)


async def test_partial_recovery_keeps_the_manifest_pending_partial_until_the_complete_copy_closes_it(
        sinks, db_down):
    """A decisions-only row is not "fully written": the manifest keeps showing the gap and the kept refs."""
    from audit.reconcile import reconcile
    from audit.row import mark_manifest
    from audit.writer import commit_or_park

    small = FakeSink("small", max_payload_bytes=8_000)
    late = FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    out = await commit_or_park(_row(big=True))
    mark_manifest("d-1", out.status, out.refs)
    held = late.pending.copy()
    late.pending.clear()  # the complete copy is out of reach on the first run
    db_down[0] = None

    await reconcile()
    m = _manifest()
    assert m.audit_written == "pending_partial"
    assert [(r["sink"], r["complete"]) for r in m.audit_pending_refs] == [("small", False)]
    assert m.audit_pending_refs[0]["ref"] in small.pending  # the kept copy is named

    # A second run with nothing new leaves the gap showing; so does a stale manifest (the crash window).
    mark_manifest("d-1", "written")
    await reconcile()
    assert _manifest().audit_written == "pending_partial" and _manifest().audit_pending_refs

    late.pending.update(held)
    report = await reconcile()
    assert report.gap_closed == 1
    assert _manifest().audit_written is True and _manifest().audit_pending_refs == []


# ---------------------------------------------------------------- one failing document does not stop the run (review fix R5)


async def test_a_poison_document_does_not_block_the_next_one(sinks, db_down, monkeypatch, logs):
    from audit import writer
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    _save_manifest("a-doc")
    _save_manifest("z-doc")
    await commit_or_park(_row("a-doc"))
    await commit_or_park(_row("z-doc"))
    db_down[0] = None
    real = writer.write_terminal_row

    async def poison(row, **kw):
        if row.doc_id == "a-doc":
            raise RuntimeError("non-retryable db error")
        return await real(row, **kw)

    monkeypatch.setattr(writer, "write_terminal_row", poison)
    report = await reconcile()  # must not raise
    assert report.failed == 1 and report.recovered == 1
    assert await get_row("z-doc") is not None and await get_row("a-doc") is None
    assert [d for d in report.docs if d["doc_id"] == "a-doc"] == [
        {"doc_id": "a-doc", "action": "failed", "error": "RuntimeError: non-retryable db error"}]
    assert "audit_reconcile_doc_failed" in logs
    # The failed document keeps its copy for the next run; the recovered one is spent.
    assert [r["doc_id"] for r in sinks[0].fetch_pending()] == ["a-doc"]
    assert _manifest("z-doc").audit_written is True and _manifest("a-doc").audit_written is None

    monkeypatch.setattr(writer, "write_terminal_row", real)  # the poison is gone: the next run recovers it
    again = await reconcile()
    assert (again.recovered, again.failed) == (1, 0) and await get_row("a-doc") is not None


async def test_a_pending_migration_still_stops_reconcile(sinks, db_down, monkeypatch):
    """Nothing can be written before the migration: one error, not one per document."""
    from audit import writer
    from audit.models import AuditMigrationPending
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park

    await commit_or_park(_row("a-doc"))
    await commit_or_park(_row("z-doc"))

    async def pending(doc_id):
        raise AuditMigrationPending("audit_meta has no migration record")

    monkeypatch.setattr(writer, "get_row", pending)
    with pytest.raises(AuditMigrationPending):
        await reconcile()
    assert len(sinks[0].fetch_pending()) == 2


# ---------------------------------------------------------------- a failure inside the park step is a gap, not a lost row (review fix R6)


@pytest.mark.parametrize("breaks", ["pending_record", "decisions_only_record", "detail_sha256"])
async def test_a_failing_park_step_is_a_gap_outcome(sinks, db_down, monkeypatch, logs, breaks):
    """Building or capturing the pending record can itself raise (e.g. mixed-type keys in a detail).

    The exception used to escape ``commit_or_park``: ``write_row`` logged ``audit_row_write_failed``
    and returned ``None``, with no ``gap`` outcome, no ``audit_gap`` event and ``audit_written`` unset.
    """
    from audit import digests, writer
    from audit.row import write_row

    small = FakeSink("small", max_payload_bytes=100)  # the partial() builder runs for this sink
    sinks[:] = [small]
    _save_manifest()
    if breaks == "detail_sha256":
        monkeypatch.setattr(digests, "detail_sha256", lambda d: (_ for _ in ()).throw(TypeError("boom")))
    else:
        monkeypatch.setattr(writer, breaks, lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    out = await writer.commit_or_park(_row())
    assert out.status == "gap" and out.result is None and out.refs == ()
    assert "audit_gap" in logs and "audit_pending" not in logs

    # Through the terminal path: the manifest says the row is missing, not "no terminal node wrote it".
    assert write_row({"doc_id": "d-1", "matter_id": "M", "doc_type": "contract"}, event="archived",
                     actor="archivist") is None
    assert _manifest().audit_written is False


# ---------------------------------------------------------------- the verifier binds recovered records to their digests (review fix R4)


async def _closed_partial(sinks, db_down):
    """d-1 recovered from a decisions-only copy, then closed by its complete copy (a ``recovered_detail`` addendum)."""
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park

    small = FakeSink("small", max_payload_bytes=8_000)
    late = FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    await commit_or_park(_row(big=True))
    held = late.pending.copy()
    late.pending.clear()
    db_down[0] = None
    await reconcile()
    late.pending.update(held)
    assert (await reconcile()).gap_closed == 1


async def _forge(model, mutate, *, doc_id="d-1"):
    """Apply ``mutate(detail)`` to the document's (only) addendum or row and rehash it, so only the binding can catch it."""
    import copy

    from sqlalchemy import select

    from audit.writer import hash_entry
    from storage.audit_log import AuditAddendumRecord, AuditLogRecord
    from storage.db import async_session

    async with async_session() as s:
        row = (await s.execute(select(AuditLogRecord).where(AuditLogRecord.doc_id == doc_id))).scalars().one()
        rec = row if model == "row" else (
            await s.execute(select(AuditAddendumRecord).where(AuditAddendumRecord.doc_id == doc_id))
        ).scalars().one()
        detail = copy.deepcopy(rec.detail)
        mutate(detail)
        rec.detail = detail
        rec.entry_hash = hash_entry(
            prev_hash=rec.prev_hash, doc_id=rec.doc_id,
            entry_id=rec.entry_id if model == "row" else rec.addendum_id,
            event=rec.event if model == "row" else rec.kind, detail=detail, matter_id=row.matter_id,
            actor=rec.actor, timestamp=rec.timestamp,
        )
        await s.commit()


def _failure_kinds(report):
    return {f["kind"] for section in (report.main, report.addenda) for f in section.failures}


async def test_a_forged_and_rehashed_recovered_detail_addendum_fails_verification(sinks, db_down):
    from audit.verify import verify_audit_db

    await _closed_partial(sinks, db_down)
    assert (await verify_audit_db()).ok  # the honest chain verifies

    await _forge("addendum", lambda d: d["detail"].update(doc_type="FORGED"))
    report = await verify_audit_db()
    assert not report.ok and "recovered_detail_mismatch" in _failure_kinds(report)


async def test_a_recovered_detail_addendum_must_be_the_rows_full_record(sinks, db_down):
    """Forging the carried detail AND its digest still fails: the row's ``full_detail_sha256`` names the record."""
    from audit.digests import detail_sha256
    from audit.verify import verify_audit_db

    await _closed_partial(sinks, db_down)

    def swap(d):
        d["detail"]["doc_type"] = "FORGED"
        d["recovered_from"]["detail_sha256"] = detail_sha256(d["detail"])

    await _forge("addendum", swap)
    report = await verify_audit_db()
    assert not report.ok and "recovered_detail_mismatch" in _failure_kinds(report)
    assert not report.main.failures  # the row itself is untouched


@pytest.mark.parametrize("value", ["", None, "drop"])
async def test_a_recovered_detail_addendum_without_a_digest_fails(sinks, db_down, value):
    from audit.verify import verify_audit_db

    await _closed_partial(sinks, db_down)

    def strip(d):
        if value == "drop":
            d["recovered_from"].pop("detail_sha256")
        else:
            d["recovered_from"]["detail_sha256"] = value

    await _forge("addendum", strip)
    report = await verify_audit_db()
    assert not report.ok and "recovered_detail_unbound" in _failure_kinds(report)


@pytest.mark.parametrize("value", ["", None, "drop"])
async def test_a_recovered_row_without_a_digest_fails(sinks, db_down, value):
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park

    await commit_or_park(_row())
    db_down[0] = None
    await reconcile()
    assert (await verify_audit_db()).ok

    def strip(d):
        if value == "drop":
            d["recovered_from"].pop("detail_sha256")
        else:
            d["recovered_from"]["detail_sha256"] = value

    await _forge("row", strip)
    report = await verify_audit_db()
    assert not report.ok and "recovered_detail_unbound" in _failure_kinds(report)


async def test_a_resumed_addendum_recovered_from_a_sink_must_carry_its_digest(sinks, db_down):
    from audit.verify import verify_audit_db

    await _row_then_parked_repeat(db_down)
    from audit.reconcile import reconcile

    await reconcile()
    assert (await verify_audit_db()).ok
    await _forge("addendum", lambda d: d["recovered_from"].update(detail_sha256=""))
    report = await verify_audit_db()
    assert not report.ok and "recovered_detail_unbound" in _failure_kinds(report)


# ---------------------------------------------------------------- reconcile keeps every distinct parked write (audit PR #239 review)


def _repeat_of(doc_id="d-1", *, event="archived", outcome="ok"):
    """A later terminal write whose ``detail`` is byte-for-byte the first write's.

    Only the part ``_content_sha`` used to leave out differs: the terminal event, or the attempts' contents.
    """
    row = _row(doc_id)
    row.event = event
    row.attempts[0].outcome = outcome
    return row


async def _row_then_park(db_down, later):
    """d-1 has a live row from ``_row()``; ``later`` fails 3x and parks. Return the manifest-marked outcome."""
    from audit.row import mark_manifest
    from audit.writer import commit_or_park

    _save_manifest()
    db_down[0] = None
    first = await commit_or_park(_row())
    assert first.status == "written"
    mark_manifest("d-1", first.status, first.refs)
    db_down[0] = RuntimeError("database is locked")
    second = await commit_or_park(later)
    assert second.status == "pending"
    mark_manifest("d-1", second.status, second.refs)
    db_down[0] = None


async def test_reconcile_keeps_a_parked_write_whose_attempts_differ_from_the_row(sinks, db_down):
    """Blocking: ``_content_sha`` ignored the attempts, so a write with other attempts was a "duplicate" and its copy deleted."""
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import get_row

    await _row_then_park(db_down, _repeat_of(outcome="failed"))
    report = await reconcile()
    assert (report.resumed, report.skipped_existing) == (1, 0)
    [add] = (await get_row("d-1"))["addenda"]
    assert add["kind"] == "resumed" and [s["outcome"] for s in add["sub_rows"]] == ["failed"]
    assert sinks[0].fetch_pending() == [] and _manifest().audit_written is True
    assert (await verify_audit_db()).ok


async def test_reconcile_keeps_a_parked_write_whose_terminal_event_differs_from_the_row(sinks, db_down):
    """Blocking: ``_content_sha`` ignored the event, so ``triage_archived`` after ``archived`` (same detail) was dropped."""
    from audit.reconcile import reconcile
    from audit.writer import get_row

    await _row_then_park(db_down, _repeat_of(event="triage_archived"))
    report = await reconcile()
    assert (report.resumed, report.skipped_existing) == (1, 0)
    [add] = (await get_row("d-1"))["addenda"]
    assert add["detail"]["event"] == "triage_archived"
    assert sinks[0].fetch_pending() == []


async def test_reconcile_still_drops_a_second_copy_of_the_same_run(sinks, db_down):
    """The content match is for the SAME run parked again under a new entry id (a new digest): spent, not written twice."""
    from audit.reconcile import reconcile
    from audit.writer import get_row

    await _row_then_park(db_down, _repeat_of())  # same event, same detail, same attempts as the live row
    report = await reconcile()
    assert (report.resumed, report.skipped_existing) == (0, 1)
    assert (await get_row("d-1"))["addenda"] == []
    assert sinks[0].fetch_pending() == []
def _second_run_big(doc_id="d-1"):
    row = _row(doc_id, big=True)
    row.detail["trace"][0].update({"visit_id": "v-second", "prompt": "second run only " + "q" * 20_000})
    row.detail["extraction"] = {"extracted_data": {"blob": "second " + "w" * 20_000}}
    return row


async def _row_then_parked_big_repeat(sinks, db_down):
    """d-1 has a live row; its second write parks as a decisions-only copy in ``small``, complete in ``late``.

    Return the complete copy, taken out of ``late`` so a test chooses when it turns up.
    """
    from audit.row import mark_manifest
    from audit.writer import commit_or_park

    small = FakeSink("small", max_payload_bytes=8_000)
    late = FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    db_down[0] = None
    first = await commit_or_park(_row())
    mark_manifest("d-1", first.status, first.refs)
    db_down[0] = RuntimeError("database is locked")
    second = await commit_or_park(_second_run_big())
    assert second.status == "pending"
    mark_manifest("d-1", second.status, second.refs)
    db_down[0] = None
    held = dict(late.pending)
    late.pending.clear()
    assert len(small.pending) == 1 and len(held) == 1
    return small, late, held


async def test_a_decisions_only_repeat_write_keeps_its_gap_and_its_spool_copy(sinks, db_down):
    """Blocking: the partial copy was recorded as a ``resumed`` addendum, then the manifest said ``written`` and the copy was deleted."""
    from audit.reconcile import reconcile
    from audit.writer import get_row

    small, late, held = await _row_then_parked_big_repeat(sinks, db_down)
    await reconcile()
    [add] = (await get_row("d-1"))["addenda"]
    assert add["kind"] == "resumed" and add["detail"]["recovered_from"]["complete"] is False
    m = _manifest()
    assert m.audit_written == "pending_partial"  # the gap is still visible
    assert [(r["sink"], r["complete"]) for r in m.audit_pending_refs] == [("small", False)]
    assert len(small.pending) == 1  # kept: its complete copy may still turn up

    await reconcile()  # nothing new: the gap and the copy stay
    assert _manifest().audit_written == "pending_partial" and len(small.pending) == 1
    assert len((await get_row("d-1"))["addenda"]) == 1


async def test_a_complete_copy_arriving_after_a_decisions_only_repeat_write_is_recorded(sinks, db_down):
    """Blocking: the partial addendum's ``full_detail_sha256`` counted as present, so the complete copy was discarded."""
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db
    from audit.writer import get_row

    small, late, held = await _row_then_parked_big_repeat(sinks, db_down)
    await reconcile()
    late.pending.update(held)  # the complete copy of the second write turns up
    report = await reconcile()
    assert report.resumed == 1
    adds = (await get_row("d-1"))["addenda"]
    assert [a["kind"] for a in adds] == ["resumed", "resumed"]
    full = adds[1]["detail"]
    assert full["extraction"] == {"extracted_data": {"blob": "second " + "w" * 20_000}}
    assert full["recovered_from"].get("complete", True) is not False
    assert small.pending == {} and late.pending == {}  # both copies of that write are spent
    m = _manifest()
    assert m.audit_written is True and m.audit_pending_refs == []
    assert (await verify_audit_db()).ok


async def test_closing_a_partial_gap_does_not_drop_a_later_write_from_the_same_batch(sinks, db_down):
    """Major: a ``recovered_detail`` close deleted every copy of the document, including a different later write."""
    from audit.reconcile import reconcile
    from audit.row import mark_manifest
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    small = FakeSink("small", max_payload_bytes=8_000)
    late = FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    out = await commit_or_park(_row(big=True))
    mark_manifest("d-1", out.status, out.refs)
    held = dict(late.pending)
    late.pending.clear()
    db_down[0] = None
    assert (await reconcile()).partial == 1  # a decisions-only row; the gap is open

    db_down[0] = RuntimeError("database is locked")
    later = await commit_or_park(_second_run_big())  # a different, later terminal write of the same document
    assert later.status == "pending"
    db_down[0] = None
    late.pending.update({f"held-{k}": v for k, v in held.items()})  # the first write's complete copy turns up in the same batch

    report = await reconcile()
    assert (report.gap_closed, report.resumed, report.rejected_hash) == (1, 1, 0)
    adds = (await get_row("d-1"))["addenda"]
    assert [a["kind"] for a in adds] == ["recovered_detail", "resumed"]
    assert adds[1]["detail"]["trace"][0]["visit_id"] == "v-second"  # the later write was recorded, not dropped
    assert small.pending == {} and late.pending == {}
    assert _manifest().audit_written is True and _manifest().audit_pending_refs == []
    assert (await verify_audit_db()).ok


@pytest.mark.parametrize("partial", [False, True])
async def test_concurrent_recovery_appends_each_parked_write_once(sinks, db_down, monkeypatch, partial):
    import asyncio

    import audit.writer as writer
    from audit.reconcile import reconcile
    from audit.verify import verify_audit_db

    if partial:
        small, late = FakeSink("small", max_payload_bytes=8_000), FakeSink("late")
        sinks[:] = [small, late]
        await writer.commit_or_park(_row(big=True))
        held = dict(late.pending)
        late.pending.clear()
        db_down[0] = None
        assert (await reconcile()).partial == 1
        late.pending.update(held)
    else:
        await _row_then_park(db_down, _repeat_of(outcome="failed"))

    original_get = writer.get_row
    both_read = asyncio.Event()
    reads = 0

    async def same_snapshot(doc_id):
        nonlocal reads
        row = await original_get(doc_id)
        reads += 1
        if reads <= 2:
            if reads == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return row

    monkeypatch.setattr(writer, "get_row", same_snapshot)
    reports = await asyncio.gather(reconcile(), reconcile())
    assert all(r.failed == 0 for r in reports)
    row = await original_get("d-1")
    assert len(row["addenda"]) == 1
    assert row["addenda"][0]["kind"] == ("recovered_detail" if partial else "resumed")
    assert (await verify_audit_db()).ok
    assert all(not sink.fetch_pending() for sink in sinks)


@pytest.mark.parametrize("as_json", [False, True])
def test_reconcile_cli_reports_document_write_failure(monkeypatch, capsys, as_json):
    import importlib.util
    import json

    from audit.reconcile import ReconcileReport

    async def failed_reconcile(**kwargs):
        return ReconcileReport(failed=1, docs=[{"doc_id": "d-1", "action": "failed", "error": "write failed"}])

    monkeypatch.setattr("audit.reconcile.reconcile", failed_reconcile)
    spec = importlib.util.spec_from_file_location(
        "audit_reconcile_failure_cli", Path(__file__).resolve().parents[1] / "scripts" / "audit_reconcile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    capsys.readouterr()
    assert mod.main(["--json"] if as_json else []) == 1
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["failed"] == 1
    else:
        assert "1 failed" in output


@pytest.mark.parametrize("original_spool_visible", [False, True])
async def test_later_complete_write_is_recovered_while_original_gap_stays_open(
    sinks, db_down, original_spool_visible
):
    from audit.reconcile import reconcile
    from audit.row import mark_manifest
    from audit.verify import verify_audit_db
    from audit.writer import commit_or_park, get_row

    small, late = FakeSink("small", max_payload_bytes=8_000), FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    out = await commit_or_park(_row(big=True))
    mark_manifest("d-1", out.status, out.refs)
    original_full = dict(late.pending)
    late.pending.clear()
    db_down[0] = None
    assert (await reconcile()).partial == 1
    original_partial = dict(small.pending)
    if not original_spool_visible:
        small.pending.clear()  # the original sink copy cannot be read on this sweep
    db_down[0] = RuntimeError("database is locked")
    await commit_or_park(_second_run_big())
    db_down[0] = None

    dry_report = await reconcile(dry_run=True)
    assert (dry_report.resumed, dry_report.rejected_hash, dry_report.gap_closed) == (1, 0, 0)
    assert (await get_row("d-1"))["addenda"] == []
    assert _manifest().audit_written == "pending_partial"
    report = await reconcile()
    assert (report.resumed, report.rejected_hash, report.gap_closed) == (1, 0, 0)
    [add] = (await get_row("d-1"))["addenda"]
    assert add["kind"] == "resumed" and add["detail"]["trace"][0]["visit_id"] == "v-second"
    assert _manifest().audit_written == "pending_partial"
    assert any(r["ref"] in original_partial for r in _manifest().audit_pending_refs)
    assert (await verify_audit_db()).ok
    await reconcile()  # the later write is spent, while the original gap stays visible
    assert _manifest().audit_written == "pending_partial"
    assert len((await get_row("d-1"))["addenda"]) == 1

    small.pending.update(original_partial)
    late.pending.update(original_full)
    assert (await reconcile()).gap_closed == 1
    assert _manifest().audit_written is True
    assert [a["kind"] for a in (await get_row("d-1"))["addenda"]] == ["resumed", "recovered_detail"]
    assert all(not sink.fetch_pending() for sink in sinks)
    assert (await verify_audit_db()).ok


async def test_later_live_write_preserves_the_original_partial_gap(sinks, db_down, monkeypatch):
    import audit.row as row_mod
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    small, late = FakeSink("small", max_payload_bytes=8_000), FakeSink("late")
    sinks[:] = [small, late]
    _save_manifest()
    out = await commit_or_park(_row(big=True))
    row_mod.mark_manifest("d-1", out.status, out.refs)
    held = dict(late.pending)
    late.pending.clear()
    db_down[0] = None
    assert (await reconcile()).partial == 1
    before_refs = _manifest().audit_pending_refs
    later_row = _second_run_big()
    monkeypatch.setattr(row_mod, "build_row", lambda *args, **kwargs: later_row)

    assert row_mod.write_row({"doc_id": "d-1"}, event="archived", actor="archivist") is not None
    assert len((await get_row("d-1"))["addenda"]) == 1
    assert _manifest().audit_written == "pending_partial"
    assert _manifest().audit_pending_refs == before_refs
    assert small.fetch_pending()

    late.pending.update(held)
    assert (await reconcile()).gap_closed == 1
    assert _manifest().audit_written is True and _manifest().audit_pending_refs == []


# ---------------------------------------------------------------- #239 review: write-path hardening


def _load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "audit_reconcile_cli", Path(__file__).resolve().parents[1] / "scripts" / "audit_reconcile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _UnreadableSink(FakeSink):
    recoverable = True

    def fetch_pending(self):
        raise OSError("disk I/O error")


def _duplicate_attempt_row():
    """A row whose two attempts share one ``attempt_id``: a deterministic primary-key clash."""
    from audit.models import AttemptRow

    row = _row()
    row.attempts = [
        AttemptRow(attempt_id="dup", stage="classification", attempt_no=1, trigger="initial", started_at=T0,
                   ended_at=T0),
        AttemptRow(attempt_id="dup", stage="extraction", attempt_no=1, trigger="initial", started_at=T0,
                   ended_at=T0),
    ]
    return row


async def test_a_duplicate_primary_key_is_not_retried(sinks, monkeypatch):
    """#239 review: a duplicate sub-row key matched "unique", so it was retried 5 times and then failed anyway."""
    from sqlalchemy.exc import IntegrityError

    from audit import writer

    builds = []
    real = writer._build_sub_rows
    monkeypatch.setattr(writer, "_build_sub_rows", lambda *a, **k: builds.append(1) or real(*a, **k))
    with pytest.raises(IntegrityError):
        await writer.write_terminal_row(_duplicate_attempt_row())
    assert len(builds) == 1  # one attempt, not MAX_ATTEMPTS


@pytest.mark.parametrize("message, retry", [
    ("UNIQUE constraint failed: audit_log.seq", True),
    ("UNIQUE constraint failed: audit_log.doc_id", True),
    ("UNIQUE constraint failed: audit_addenda.doc_id, audit_addenda.seq", True),
    ('duplicate key value violates unique constraint "ux_audit_log_seq"', True),
    ('duplicate key value violates unique constraint "ix_audit_addenda_doc_seq"', True),
    ("UNIQUE constraint failed: audit_attempts.attempt_id", False),
    ("UNIQUE constraint failed: audit_log.entry_id", False),
    ('duplicate key value violates unique constraint "audit_attempts_pkey"', False),
    ("NOT NULL constraint failed: audit_log.event", False),
])
def test_only_chain_position_races_are_retryable(message, retry):
    from sqlalchemy.exc import IntegrityError

    from audit.writer import _is_retryable

    assert _is_retryable(IntegrityError("INSERT", {}, Exception(message))) is retry


async def test_a_deterministic_integrity_error_is_parked_without_retries(sinks, monkeypatch):
    from audit import writer
    from audit.writer import commit_or_park

    monkeypatch.setattr(writer, "RETRY_DELAYS_S", (5.0, 5.0))
    calls = []
    real = writer.write_terminal_row
    monkeypatch.setattr(writer, "write_terminal_row", lambda *a, **k: calls.append(1) or real(*a, **k))
    import time

    started = time.monotonic()
    out = await commit_or_park(_duplicate_attempt_row())
    assert out.status == "pending" and len(calls) == 1 and time.monotonic() - started < 2.0


async def test_reconcile_quarantines_a_record_that_can_never_be_written(sinks, db_down, logs):
    """#239 review: a deterministic failure used to fail every reconcile run forever."""
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row

    await commit_or_park(_row("a-doc"))
    await commit_or_park(_duplicate_attempt_row())  # d-1: parked after the first failed attempt
    db_down[0] = None
    first = await reconcile()
    assert (first.recovered, first.failed, first.quarantined) == (1, 1, 1)
    assert first.quarantine_held == 1
    assert [d["action"] for d in first.docs if d["doc_id"] == "d-1"] == ["quarantined"]
    assert await get_row("a-doc") is not None and await get_row("d-1") is None
    assert "audit_reconcile_quarantined" in logs
    # Kept, not deleted: the record and its error are in the spool's quarantine table.
    import sqlite3

    from audit.sinks.sql import spool_path

    with sqlite3.connect(spool_path()) as conn:
        [(doc_id, error)] = conn.execute("SELECT doc_id, error FROM quarantined_rows").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM pending_rows").fetchone()[0] == 0
    assert doc_id == "d-1" and "IntegrityError" in error
    # The next run is clean: it does not fail on the quarantined copy again, and still reports it.
    again = await reconcile()
    assert (again.failed, again.quarantined, again.quarantine_held) == (0, 0, 1)


async def test_reconcile_keeps_a_transiently_failing_record_pending(sinks, db_down, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from audit import writer
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park

    await commit_or_park(_row())
    db_down[0] = None

    async def locked(row, **kw):
        raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

    monkeypatch.setattr(writer, "write_terminal_row", locked)
    report = await reconcile()
    assert (report.failed, report.quarantined) == (1, 0)
    assert len(sinks[0].fetch_pending()) == 1 and sinks[0].quarantined_count() == 0


async def test_reconcile_reports_a_sink_it_could_not_read(sinks, db_down):
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park

    await commit_or_park(_row())
    db_down[0] = None
    sinks.append(_UnreadableSink("flaky"))
    report = await reconcile()
    assert report.recovered == 1  # the readable sink is still reconciled
    assert (report.sink_read_failed, report.sink_read_failed_names) == (1, ["flaky"])
    assert report.as_dict()["sink_read_failed_names"] == ["flaky"]


def test_cli_exits_nonzero_when_a_sink_cannot_be_read(sinks, db_down, capsys):
    import json

    sinks[:] = [_UnreadableSink("flaky")]
    db_down[0] = None
    mod = _load_cli()
    capsys.readouterr()
    assert mod.main(["--json"]) == 1  # was 0 with an all-zero report
    printed = capsys.readouterr().out
    out = json.loads(printed[printed.index('{\n  "recovered"'):])  # log lines may precede the report in-process
    assert out["sink_read_failed"] == 1 and out["recovered"] == 0
    assert mod.main([]) == 1
    assert "could not read 1 sink" in capsys.readouterr().err


def test_a_missing_spool_is_reported_but_is_not_an_error(sinks, db_down, capsys, tmp_path, monkeypatch):
    import json

    missing = tmp_path / "never-created" / "spool.db"
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(missing))
    db_down[0] = None
    mod = _load_cli()
    capsys.readouterr()
    assert mod.main(["--json"]) == 0  # nothing was ever parked: a clean run
    out = json.loads(capsys.readouterr().out)
    assert out["spool_missing"] == [str(missing)] and out["sink_read_failed"] == 0
    assert not missing.exists()  # reading never creates it


def test_cli_json_error_when_the_audit_migration_has_not_run(sinks, db_down, capsys, monkeypatch):
    import json

    from audit import reconcile as rec
    from audit.models import AuditMigrationPending

    async def pending(*, dry_run=False):
        raise AuditMigrationPending("audit_meta has no migration record")

    monkeypatch.setattr(rec, "reconcile", pending)
    mod = _load_cli()
    capsys.readouterr()
    assert mod.main(["--json"]) == 1
    out = json.loads(capsys.readouterr().out)  # stdout is JSON, not an empty string and a traceback
    assert out["error"] == "audit_migration_pending" and "migration" in out["message"]
    assert mod.main([]) == 1
    assert "audit migration has not run" in capsys.readouterr().err


async def test_resumed_counts_only_copies_actually_written(sinks, db_down, monkeypatch):
    """#239 review: ``resumed`` was counted before the write, which dedupes a copy a racing run recorded."""
    from audit import reconcile as rec
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row, write_terminal_row

    db_down[0] = None
    await write_terminal_row(_row())  # d-1 already has its row
    second = _row()
    second.detail["trace"][0]["visit_id"] = "v-second"
    db_down[0] = RuntimeError("database is locked")
    await commit_or_park(second)  # a later terminal write that parked
    db_down[0] = None
    [parked] = sinks[0].fetch_pending()
    record = {**parked, "_sink": "sql"}

    # ``write_addendum`` returns the existing addendum for a copy it already recorded.
    assert await rec._write_repeat("d-1", record, known=set()) is True
    known = {a["addendum_id"] for a in (await get_row("d-1"))["addenda"]}
    assert await rec._write_repeat("d-1", record, known=known) is False


async def test_a_copy_a_racing_sweep_wrote_after_our_row_snapshot_is_skipped_not_resumed(sinks, db_down, monkeypatch):
    """Review (PR #298): the competing write lands after our snapshot, and our write keeps the original ``known``."""
    from audit import reconcile as rec
    from audit.reconcile import reconcile
    from audit.writer import commit_or_park, get_row, write_terminal_row

    db_down[0] = None
    await write_terminal_row(_row())  # d-1 already has its row, and no addendum yet
    second = _row()
    second.detail["trace"][0]["visit_id"] = "v-second"
    db_down[0] = RuntimeError("database is locked")
    await commit_or_park(second)  # a later terminal write that parked
    db_down[0] = None

    real = rec._write_repeat
    outcomes: list[bool] = []

    async def late(doc_id, rec_, known=None):
        outcomes.append(await real(doc_id, rec_, set()))  # the racing sweep: it really wrote the addendum
        outcomes.append(await real(doc_id, rec_, known))  # ours: the original snapshot's ``known``, unchanged
        return outcomes[-1]

    monkeypatch.setattr(rec, "_write_repeat", late)
    report = await reconcile()
    assert outcomes == [True, False]  # one resumed (the racing sweep's), one skipped_existing (ours)
    assert report.resumed == 0 and report.skipped_existing == 1
    assert len((await get_row("d-1"))["addenda"]) == 1


def test_decisions_only_trace_keeps_the_visits_audit_event():
    """Review (PR #298): a decisions-only copy must still say who decided (``decision.audit_event``)."""
    from audit.writer import _decisions_view

    event = {"event": "review_decision", "actor": "human_reviewer", "detail": {"disposition": "approve"}}
    [view] = _decisions_view([{"visit_id": "v1", "node": "human_review", "decisions": [],
                               "decision": {"audit_event": event, "extracted_data": {"big": "x" * 10}}}])
    assert view["decision"] == {"audit_event": event}  # the payload fields stay out


def test_reconcile_counts_malformed_spool_rows_as_failed(sinks, tmp_path):
    """Review (PR #298): a corrupt spool row was only logged; the run must report it and exit nonzero."""
    import sqlite3

    from audit import reconcile as rec
    from audit.sinks import capture_pending
    from audit.sinks.sql import spool_path

    capture_pending({"doc_id": "d-1", "event": "archived"})
    with sqlite3.connect(spool_path()) as conn:
        conn.execute("INSERT INTO pending_rows (ref, doc_id, record, created_at) VALUES "
                     "('pend-bad', 'd-bad', '{\"truncated\": ', '2000-01-01T00:00:00+00:00')")
    report = rec.ReconcileReport()
    records = rec._gather(report)
    assert [r["doc_id"] for r in records] == ["d-1"]  # the valid record is still processed
    assert report.malformed == 1 and report.failed == 1 and report.as_dict()["malformed"] == 1


@pytest.mark.parametrize("tamper", ["missing", "extra", "forged_detail"])
async def test_a_recovered_detail_addendums_attempts_are_checked_against_their_hashes(sinks, db_down, tamper):
    """Review (PR #298): the carried ``sub_rows`` must be exactly the attempts the detail lists, hash for hash."""
    import copy

    from audit.verify import verify_audit_db

    await _closed_partial(sinks, db_down)
    assert (await verify_audit_db()).ok

    def mutate(d):
        if tamper == "missing":
            d["sub_rows"] = []
        elif tamper == "extra":
            d["sub_rows"] = d["sub_rows"] + [{**copy.deepcopy(d["sub_rows"][0]), "attempt_id": "ghost"}]
        else:
            d["sub_rows"][0]["detail"] = {"forged": True}

    await _forge("addendum", mutate)
    report = await verify_audit_db()
    reasons = {f.get("reason") for f in report.addenda.failures}
    assert not report.ok and "recovered_detail_mismatch" in _failure_kinds(report)
    assert reasons & {"missing_attempt", "attempt_hash", "attempt_malformed", "extra_attempt"}
