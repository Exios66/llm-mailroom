"""Roadmap M12: unified audit rows in the Parquet warehouse (spec §7)."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import date, datetime, timezone

import pytest

from storage import db

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
DAY1, DAY2 = date(2026, 11, 3), date(2026, 11, 4)


@pytest.fixture
def wh(monkeypatch, temp_base_dir, tmp_path):
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "1")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mailroom.db'}")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    # M12 warehouse audit files export unified rows, which need the migration.
    import asyncio

    from storage import audit_migrate

    asyncio.run(audit_migrate.migrate_audit_log())
    if "storage.warehouse" in sys.modules:
        importlib.reload(sys.modules["storage.warehouse"])
    from storage import warehouse

    return warehouse


def _att(doc, stage, n, trigger="initial"):
    from audit.models import AttemptRow

    return AttemptRow(attempt_id=f"{doc}-{stage}-{n}", stage=stage, attempt_no=n, trigger=trigger, outcome="ok",
                      started_at=T0, ended_at=T0, detail={"nodes": [stage]})


def seed(doc, *, doc_type="contract", attempts=None, scoring=None, model=None):
    """A catalog document plus its unified row (synchronous)."""
    from audit.row import build_row
    from audit.writer import write_terminal_row
    from storage.catalog import write_document_record

    state = {"doc_id": doc, "matter_id": "M", "doc_type": doc_type, "scoring": scoring,
             "audit_trace": [{"node": "extract", "model": model}] if model else []}
    row = build_row(state, event="archived", actor="archivist")
    row.attempts = list(attempts if attempts is not None else
                        [_att(doc, "classification", 1), _att(doc, "extraction", 1)])

    async def go():
        await write_document_record({"doc_id": doc, "matter_id": "M", "original_filename": f"{doc}.txt",
                                     "stage": "archived", "doc_type": doc_type})
        await write_terminal_row(row)

    asyncio.run(go())


def add_addendum(doc, kind="rescore"):
    from audit.models import Addendum
    from audit.writer import write_addendum

    asyncio.run(write_addendum(Addendum(doc_id=doc, kind=kind, actor="audit-rescore", detail={"note": kind})))


def legacy_event(doc, event="intake_received"):
    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry

    asyncio.run(write_audit_entry(build_audit_entry(doc_id=doc, matter_id="M", event=event, actor="intake",
                                                    detail={}, prev_hash="")))


def test_parquet_round_trip_rows_attempts_addenda(wh):
    from audit.metrics import METRIC_KEYS
    from audit.writer import get_row

    seed("d-1")
    add_addendum("d-1")
    result = asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY1))
    assert result["exported_audit_rows"] == 1 and result["exported_audit_attempts"] == 2
    assert result["exported_audit_addenda"] == 1

    full = asyncio.run(get_row("d-1"))
    rows = wh._read_parquet_rows(wh.daily_audit_rows_path(DAY1))
    assert len(rows) == 1
    r = rows[0]
    assert (r["entry_id"], r["doc_id"], r["entry_hash"]) == (full["entry_id"], "d-1", full["entry_hash"])
    assert json.loads(r["detail_json"]) == full["detail"]
    assert set(METRIC_KEYS) <= set(r)
    assert r["rescored"] is True and r["doc_type"] == "contract"

    atts = wh._read_parquet_rows(wh.daily_audit_attempts_path(DAY1))
    assert sorted(a["attempt_id"] for a in atts) == ["d-1-classification-1", "d-1-extraction-1"]
    assert {a["parent_id"] for a in atts} == {full["entry_id"]}
    assert json.loads(atts[0]["detail_json"]) == {"nodes": [atts[0]["stage"]]}

    adds = wh._read_parquet_rows(wh.daily_audit_addenda_path(DAY1))
    assert [(a["addendum_id"], a["doc_id"], a["kind"]) for a in adds] == [
        (full["addenda"][0]["addendum_id"], "d-1", "rescore")]

    # A second export the same day merges by key: one more addendum, no duplicates.
    add_addendum("d-1", "review_decision")
    asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY1))
    assert len(wh._read_parquet_rows(wh.daily_audit_rows_path(DAY1))) == 1
    assert len(wh._read_parquet_rows(wh.daily_audit_attempts_path(DAY1))) == 2
    adds = wh._read_parquet_rows(wh.daily_audit_addenda_path(DAY1))
    assert sorted(a["kind"] for a in adds) == ["rescore", "review_decision"]
    # The per-event documents file is untouched by the unified export.
    assert [d["doc_id"] for d in wh._read_parquet_rows(wh.daily_documents_path(DAY1))] == ["d-1"]


def test_legacy_exported_once(wh):
    legacy_event("d-1")
    legacy_event("d-1", "classified")
    seed("d-1")
    first = asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY1))
    assert first["exported_audit_legacy"] == 2
    path = wh.audit_legacy_path()
    legacy = wh._read_parquet_rows(path)
    assert sorted(e["event"] for e in legacy) == ["classified", "intake_received"]
    stamp = wh.load_warehouse_manifest()["legacy_export"]
    assert stamp["rows"] == 2 and stamp["file"] == path.name

    mtime = path.stat().st_mtime_ns
    legacy_event("d-2")
    seed("d-2")
    second = asyncio.run(wh.export_to_warehouse(doc_ids=["d-2"], stamp=DAY2))
    assert second["exported_audit_legacy"] == 0
    assert path.stat().st_mtime_ns == mtime and len(wh._read_parquet_rows(path)) == 2
    assert wh.load_warehouse_manifest()["legacy_export"] == stamp
    # Later per-event entries still reach the daily audit file, as before.
    assert [e["doc_id"] for e in wh._read_parquet_rows(wh.daily_audit_path(DAY2))] == ["d-2"]


def test_manifest_maps_doc_to_hash_and_files(wh):
    from audit.readers import doc_view
    from audit.writer import get_row

    seed("d-1")
    seed("d-2", attempts=[])
    asyncio.run(wh.export_to_warehouse(doc_ids=["d-1", "d-2"], stamp=DAY1))
    docs = wh.load_warehouse_manifest()["documents"]
    h1 = asyncio.run(get_row("d-1"))["entry_hash"]
    assert docs["d-1"] == {
        "entry_hash": h1, "export_date": "2026-11-03",
        "files": ["audit_rows_2026-11-03.parquet", "audit_attempts_2026-11-03.parquet"],
    }
    assert docs["d-2"]["files"] == ["audit_rows_2026-11-03.parquet"]

    add_addendum("d-1")
    asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY2))
    docs = wh.load_warehouse_manifest()["documents"]
    assert docs["d-1"]["export_date"] == "2026-11-04"
    assert docs["d-1"]["files"] == ["audit_rows_2026-11-04.parquet", "audit_attempts_2026-11-04.parquet",
                                    "audit_addenda_2026-11-04.parquet"]
    assert docs["d-2"]["export_date"] == "2026-11-03"  # untouched by a batch it was not in
    assert asyncio.run(doc_view("d-1"))["warehouse"] == {
        "export_date": "2026-11-04", "files": docs["d-1"]["files"]}


def _null_legacy_seq(where: str) -> None:
    """Make some migrated legacy rows look like pre-``seq`` rows (NULL, as ``ALTER TABLE ADD COLUMN`` leaves them)."""
    from sqlalchemy import text

    async def go():
        async with db.async_session() as session:
            await session.execute(text(f"UPDATE audit_log_legacy SET seq = NULL WHERE {where}"))
            await session.commit()

    asyncio.run(go())


def test_legacy_history_with_null_seq_mixed_with_integers_still_sorts(wh):
    """Review threads T3/T27: a NULL ``seq`` next to an integer ``seq`` must not abort the export."""
    legacy_event("d-1", "intake_received")
    legacy_event("d-1", "classified")
    legacy_event("d-1", "extracted")
    seed("d-1")
    _null_legacy_seq("seq = 1")  # one pre-seq row, the others keep integers

    history = asyncio.run(wh.fetch_legacy_history())
    assert [e["event"] for e in history] == ["intake_received", "classified", "extracted"]
    assert [e["seq"] for e in history] == [None, 2, 3]  # stored values are exported untouched

    result = asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY1))
    assert result["exported_audit_legacy"] == 3
    assert "legacy_export" in wh.load_warehouse_manifest()


def test_legacy_exported_even_when_no_terminal_document_is_selected(wh):
    """Review thread T5: an empty selection (here a future watermark) must not skip the one-time legacy export."""
    legacy_event("d-1")
    legacy_event("d-1", "classified")
    seed("d-1")

    empty = asyncio.run(wh.export_to_warehouse(since=datetime(2099, 1, 1, tzinfo=timezone.utc), stamp=DAY1))
    assert empty["exported_documents"] == 0
    assert empty["exported_audit_legacy"] == 2
    assert sorted(e["event"] for e in wh._read_parquet_rows(wh.audit_legacy_path())) == ["classified", "intake_received"]
    stamp = wh.load_warehouse_manifest()["legacy_export"]
    assert stamp["rows"] == 2

    # Still exactly once: the next export, empty or not, leaves the file and the stamp alone.
    mtime = wh.audit_legacy_path().stat().st_mtime_ns
    later = asyncio.run(wh.export_to_warehouse(doc_ids=["d-1"], stamp=DAY1))
    assert later["exported_audit_legacy"] == 0
    assert wh.audit_legacy_path().stat().st_mtime_ns == mtime
    assert wh.load_warehouse_manifest()["legacy_export"] == stamp


def test_empty_selection_with_no_legacy_rows_writes_no_file(wh):
    """An empty export over an empty legacy history stays a no-op for the files (it only stamps the manifest)."""
    result = asyncio.run(wh.export_to_warehouse(doc_ids=["nobody"], stamp=DAY1))
    assert result["exported_documents"] == 0
    assert result["exported_audit_legacy"] == 0
    assert not wh.audit_legacy_path().exists()
    assert wh.load_warehouse_manifest()["legacy_export"]["rows"] == 0


def test_incremental_export_reselects_a_document_whose_addendum_is_newer_than_the_watermark(wh):
    """Review thread T4: an addendum never touches the catalog row, so the watermark alone would skip it."""
    seed("d-1")
    seed("d-2")
    first = asyncio.run(wh.export_to_warehouse(stamp=DAY1))  # watermark = newest catalog update
    assert first["exported_documents"] == 2 and first["exported_audit_addenda"] == 0

    add_addendum("d-1", "review_decision")  # no catalog write: documents.updated_at is unchanged
    second = asyncio.run(wh.export_to_warehouse(stamp=DAY1))
    assert second["exported_documents"] == 1  # only d-1, not d-2
    assert second["exported_audit_addenda"] == 1
    adds = wh._read_parquet_rows(wh.daily_audit_addenda_path(DAY1))
    assert [(a["doc_id"], a["kind"]) for a in adds] == [("d-1", "review_decision")]

    # Re-selecting is idempotent: the same addendum merges by key, never duplicates.
    asyncio.run(wh.export_to_warehouse(stamp=DAY1))
    assert len(wh._read_parquet_rows(wh.daily_audit_addenda_path(DAY1))) == 1


def _catalog(doc, stage):
    from storage.catalog import write_document_record

    asyncio.run(write_document_record({"doc_id": doc, "matter_id": "M", "original_filename": f"{doc}.txt",
                                       "stage": stage, "doc_type": "contract"}))


def _row(doc, event):
    from audit.row import build_row
    from audit.writer import write_terminal_row

    row = build_row({"doc_id": doc, "matter_id": "M", "doc_type": "contract"}, event=event, actor="pipeline")
    asyncio.run(write_terminal_row(row))


def test_incremental_export_picks_up_a_rejection_row_written_after_the_routine_export(wh):
    """Regression (PR #239 review): a rejected review exports when the catalog flips to ``failed``, before the
    ``failed`` audit row exists, and a later document moves the watermark past that row. The next incremental
    export must still carry the row."""
    _catalog("rejected", "failed")
    routine = asyncio.run(wh.export_to_warehouse(doc_ids=["rejected"], stamp=DAY1))  # the hook in the reject path
    assert routine["exported_documents"] == 1 and routine["exported_audit_rows"] == 0
    assert "rejected" not in (wh.load_warehouse_manifest().get("documents") or {})

    _row("rejected", "failed")  # the audit_failed node, after the hook
    seed("later")  # its catalog update is newer than the failed row
    asyncio.run(wh.export_to_warehouse(doc_ids=["later"], stamp=DAY1))  # advances the watermark past the row

    incremental = asyncio.run(wh.export_to_warehouse(stamp=DAY1))
    assert incremental["exported_audit_rows"] == 1
    rows = wh._read_parquet_rows(wh.daily_audit_rows_path(DAY1))
    assert sorted((r["doc_id"], r["event"]) for r in rows) == [("later", "archived"), ("rejected", "failed")]
    assert wh.load_warehouse_manifest()["documents"]["rejected"]["files"] == ["audit_rows_2026-11-03.parquet"]

    # Nothing is selected the next time: the row is exported and its hash is in the manifest.
    assert asyncio.run(wh.export_to_warehouse(stamp=DAY1))["exported_documents"] == 0


def test_addendum_only_change_is_exported_once_not_on_every_incremental_run(wh):
    """Regression (PR #239 review): the watermark did not cover the addendum timestamp, so the document
    was selected (and rewritten) on every incremental run."""
    seed("d-1")
    asyncio.run(wh.export_to_warehouse(stamp=DAY1))
    add_addendum("d-1", "review_decision")
    assert asyncio.run(wh.export_to_warehouse(stamp=DAY1))["exported_documents"] == 1
    assert asyncio.run(wh.export_to_warehouse(stamp=DAY1))["exported_documents"] == 0
    add_addendum("d-1", "rescore")
    again = asyncio.run(wh.export_to_warehouse(stamp=DAY1))
    assert again["exported_documents"] == 1 and again["exported_audit_addenda"] == 2


def test_legacy_alone_export_does_not_block_the_event_loop_thread(wh, monkeypatch):
    """Review (PR #298): the lock and manifest I/O of the no-document legacy export run off the event loop."""
    import threading

    from storage import warehouse

    seen: dict[str, int] = {}

    async def fake_history():
        return []

    def fake_publish(legacy):
        seen["publish"] = threading.get_ident()
        return 0

    monkeypatch.setattr(warehouse, "fetch_legacy_history", fake_history)
    monkeypatch.setattr(warehouse, "_publish_legacy_alone", fake_publish)

    async def run():
        seen["loop"] = threading.get_ident()
        return await warehouse._export_legacy_alone({})

    assert asyncio.run(run()) == 0
    assert seen["publish"] != seen["loop"]
