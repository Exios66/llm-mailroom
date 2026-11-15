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
