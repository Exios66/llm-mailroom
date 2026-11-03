#!/usr/bin/env python3
"""Build ``src/tests/fixtures/audit/legacy_audit.db`` (roadmap M2).

The fixture is a database as the release BEFORE the unified audit row left it:
a per-event ``audit_log`` (many rows per document, ``seq`` counted per
document, non-unique ``doc_id`` index) written by ``write_audit_entry``, the
writer that was current when the migration was written. ``test_audit_migrate``
copies it into a temp dir and migrates the copy.

Ids and timestamps are fixed, so every run produces the same logical rows
(the SQLite file itself is not byte-stable; ``test_fixture_is_reproducible``
compares rows).

    PYTHONPATH=src python src/scripts/build_legacy_audit_fixture.py
    PYTHONPATH=src python src/scripts/build_legacy_audit_fixture.py --out /tmp/x.db

Run it from a tree whose ``write_audit_entry`` still writes the per-event
``audit_log``. The script keeps ``ensure_schema`` from migrating the file it is
building, then drops the audit tables a pre-migration database does not have.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

DEFAULT_OUT = SRC_DIR / "tests" / "fixtures" / "audit" / "legacy_audit.db"

# audit_log exactly as the previous release created it (per-document seq, a
# plain doc_id index). Created up front so ``create_all`` leaves it alone.
LEGACY_AUDIT_LOG_DDL = (
    """CREATE TABLE audit_log (
        entry_id VARCHAR(128) NOT NULL,
        doc_id VARCHAR(128) NOT NULL,
        matter_id VARCHAR(128) NOT NULL,
        event VARCHAR(128) NOT NULL,
        actor VARCHAR(128) NOT NULL,
        detail JSON NOT NULL,
        prev_hash VARCHAR(256) NOT NULL,
        entry_hash VARCHAR(256) NOT NULL,
        seq INTEGER NOT NULL,
        timestamp DATETIME NOT NULL,
        PRIMARY KEY (entry_id),
        FOREIGN KEY(doc_id) REFERENCES documents (doc_id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_audit_log_matter_id ON audit_log (matter_id)",
    "CREATE INDEX ix_audit_log_doc_id ON audit_log (doc_id)",
)

# Tables that only exist after the unified-audit-row release.
POST_MIGRATION_TABLES = (
    "audit_log_legacy",
    "audit_attempts",
    "audit_addenda",
    "audit_meta",
    "audit_trace_io",
)

_T0 = datetime(2026, 10, 1, 9, 0, 0, 123456, tzinfo=timezone.utc)

# (doc_id, matter_id, [(event, actor, detail), ...])
DOCUMENTS = (
    ("fixture-doc-a", "matter-1", [
        ("ingested", "ingest", {"original_filename": "a.pdf", "bytes": 1024}),
        ("classified", "sorter", {"doc_type": "contract", "confidence": 0.93}),
        ("extracted", "contracts_specialist", {"fields": {"parties": ["Acme", "Beta"], "term_months": 24}, "score": 0.5}),
        ("archived", "archivist", {"note": "café — ünïcode", "nested": {"a": [1, 2.5, None, True]}}),
    ]),
    ("fixture-doc-b", "matter-1", [
        ("ingested", "ingest", {"original_filename": "b.txt"}),
        ("classified", "sorter", {"doc_type": "correspondence", "confidence": 0.71}),
        ("routed_to_review", "pipeline", {"reason": "low_confidence"}),
        ("review_approved", "reviewer", {"reviewer": "dana"}),
        ("archived", "archivist", {"archive_path": "archive/matter-1/correspondence/b.txt"}),
    ]),
    ("fixture-doc-c", "matter-2", [
        ("ingested", "ingest", {"original_filename": "c.pdf"}),
        ("failed", "pipeline", {"failure_class": "extraction_failed", "error_message": "parse error"}),
    ]),
)


async def _write_all() -> int:
    from schemas.audit import AuditLogEntry, compute_audit_hash
    from storage.audit_log import get_latest_audit_hash, write_audit_entry

    tick = 0
    written = 0
    # Interleave the documents so the global write order differs from the
    # per-document order, as it does in a live database.
    longest = max(len(events) for _, _, events in DOCUMENTS)
    for index in range(longest):
        for doc_id, matter_id, events in DOCUMENTS:
            if index >= len(events):
                continue
            event, actor, detail = events[index]
            tick += 1
            prev_hash = await get_latest_audit_hash(doc_id)
            entry = AuditLogEntry(
                entry_id=f"{doc_id}-{index + 1:02d}",
                doc_id=doc_id,
                matter_id=matter_id,
                event=event,
                actor=actor,
                detail=detail,
                prev_hash=prev_hash,
                timestamp=_T0 + timedelta(seconds=tick),
            )
            entry.entry_hash = compute_audit_hash(
                entry.prev_hash, doc_id, entry.entry_id, event, detail,
                matter_id=matter_id, actor=actor, timestamp=entry.timestamp,
            )
            await write_audit_entry(entry)
            written += 1
    return written


def build(out: Path) -> int:
    out = Path(out)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(tmp) / 'mailroom.db'}"
        from storage import db

        db._schema_checked_url = None
        db._ensure_models_imported()

        # documents first (the FK target), then the old-shape audit_log.
        from sqlalchemy import create_engine

        sync_engine = create_engine(f"sqlite:///{Path(tmp) / 'mailroom.db'}")
        try:
            db.Base.metadata.tables["documents"].create(sync_engine, checkfirst=True)
            with sync_engine.begin() as conn:
                for ddl in LEGACY_AUDIT_LOG_DDL:
                    conn.exec_driver_sql(ddl)
        finally:
            sync_engine.dispose()

        # Keep this build from migrating the file it is producing.
        try:
            import storage.audit_migrate as audit_migrate
        except ImportError:
            audit_migrate = None
        original = getattr(audit_migrate, "migrate_at_startup", None)
        if audit_migrate is not None and original is not None:
            async def _skip(*_a, **_k):
                return audit_migrate.MigrationResult(migrated=False, legacy_rows=0, backup_path=None)

            audit_migrate.migrate_at_startup = _skip
        try:
            written = asyncio.run(_write_all())
        finally:
            if audit_migrate is not None and original is not None:
                audit_migrate.migrate_at_startup = original

        src = Path(tmp) / "mailroom.db"
        conn = sqlite3.connect(src)
        try:
            for table in POST_MIGRATION_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("VACUUM")
        finally:
            conn.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        dest = sqlite3.connect(out)
        try:
            sqlite3.connect(src).backup(dest)
        finally:
            dest.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    written = build(args.out)
    print(f"wrote {written} legacy audit rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
