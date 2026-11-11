"""Seed a synthetic audit store for screenshots, demos and tests.

This helper writes **only** the directory passed with ``--out``. It refuses to
touch the configured mailroom base directory, the current directory's ``data/``
store, the ``data/`` store of the repository checkout containing this package,
or an existing ``mailroom.db`` unless ``--force`` is given, so it can never seed
production state by accident.

    python -m mailroom_diagnostics.seed_audit_demo --out /tmp/audit-demo --schema event-chain
    python -m mailroom_diagnostics.seed_audit_demo --out /tmp/audit-demo --schema unified

Every row is synthetic. ``document_text`` and ``note`` fields exist on purpose:
they model the raw detail a real audit row can carry, and the diagnostics API
must never return their values.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SYNTHETIC_TEXT = "SYNTHETIC-DEMO-DOCUMENT-TEXT-NOT-FOR-DISPLAY"
SYNTHETIC_NOTE = "SYNTHETIC-OPERATOR-NOTE-NOT-FOR-DISPLAY"

_DOCS = ("demo-contract-001", "demo-correspondence-002", "demo-claim-003")


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _entry_hash(
    *,
    prev_hash: str,
    doc_id: str,
    entry_id: str,
    event: str,
    detail: dict[str, Any],
    matter_id: str,
    actor: str,
    timestamp: datetime,
) -> str:
    from schemas.audit import compute_audit_hash

    return compute_audit_hash(
        prev_hash,
        doc_id,
        entry_id,
        event,
        detail,
        matter_id=matter_id,
        actor=actor,
        timestamp=timestamp,
    )


def _attempt_hash(sub: dict[str, Any], *, matter_id: str, actor: str) -> str | None:
    """Hash a sub-row with the foundation's own primitive when it is installed."""
    try:
        from audit.writer import attempt_hash
    except Exception:
        return None
    return attempt_hash(
        {
            "attempt_id": sub["attempt_id"],
            "parent_id": sub["parent_id"],
            "doc_id": sub["doc_id"],
            "stage": sub["stage"],
            "attempt_no": sub["attempt_no"],
            "trigger": sub["trigger"],
            "outcome": sub["outcome"],
            "started_at": sub["started_at"],
            "ended_at": sub["ended_at"],
            "detail": sub["detail"],
        },
        matter_id=matter_id,
        actor=actor,
    )


def _detail(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    # Values are synthetic but deliberately present: the API projection must
    # expose only their key names.
    detail = {
        "document_text": SYNTHETIC_TEXT,
        "note": SYNTHETIC_NOTE,
        "original_filename": "synthetic-demo.pdf",
    }
    detail.update(extra or {})
    return detail


def seed_event_chain(db_path: Path, *, force: bool = False) -> list[str]:
    """Write a per-event hash chain (today's schema) for three demo documents."""
    _prepare(db_path, force=force)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE audit_log ("
            "entry_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, matter_id TEXT NOT NULL,"
            "event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT,"
            "prev_hash TEXT, entry_hash TEXT, seq INTEGER, timestamp TEXT)"
        )
        start = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
        for index, doc_id in enumerate(_DOCS):
            seq = 0
            prev = ""
            events = ["ingested", "classified", "extracted"]
            if index == 2:
                events += ["routed_to_review", "review_approved"]
            events += ["failed" if index == 1 else "archived"]
            for step, event in enumerate(events):
                seq += 1
                timestamp = start + timedelta(minutes=index * 10 + step)
                entry_id = f"{doc_id}-entry-{seq}"
                detail = _detail({"stage": event})
                digest = _entry_hash(
                    prev_hash=prev,
                    doc_id=doc_id,
                    entry_id=entry_id,
                    event=event,
                    detail=detail,
                    matter_id=f"DEMO-MATTER-{index + 1}",
                    actor="demo-seeder",
                    timestamp=timestamp,
                )
                conn.execute(
                    "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        doc_id,
                        f"DEMO-MATTER-{index + 1}",
                        event,
                        "demo-seeder",
                        json.dumps(detail),
                        prev,
                        digest,
                        seq,
                        _iso(timestamp),
                    ),
                )
                prev = digest
        conn.commit()
    finally:
        conn.close()
    return list(_DOCS)


def seed_unified(db_path: Path, *, force: bool = False) -> list[str]:
    """Write the optional unified-row schema (one row + attempts + addenda).

    Attempt hashes are written only when the foundation's ``audit.writer`` is
    importable; without it the API reports attempt verification as unavailable
    instead of inventing a second hashing rule.
    """
    _prepare(db_path, force=force)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE audit_log (
                entry_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL UNIQUE,
                matter_id TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
                detail TEXT, prev_hash TEXT, entry_hash TEXT,
                seq INTEGER NOT NULL UNIQUE, timestamp TEXT);
            CREATE TABLE audit_attempts (
                attempt_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, doc_id TEXT NOT NULL,
                stage TEXT NOT NULL, attempt_no INTEGER NOT NULL, trigger TEXT NOT NULL,
                outcome TEXT, started_at TEXT, ended_at TEXT, detail TEXT, entry_hash TEXT);
            CREATE TABLE audit_addenda (
                addendum_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, kind TEXT NOT NULL,
                seq INTEGER NOT NULL, actor TEXT NOT NULL, timestamp TEXT, detail TEXT,
                prev_hash TEXT, entry_hash TEXT);
            CREATE TABLE audit_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE audit_log_legacy (
                entry_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, matter_id TEXT NOT NULL,
                event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT,
                prev_hash TEXT, entry_hash TEXT, seq INTEGER, timestamp TEXT);
            """
        )
        conn.execute(
            "INSERT INTO audit_meta VALUES (?,?)",
            ("migration", json.dumps({"migrated_at": "2026-11-09T08:00:00+00:00", "legacy_rows": 0})),
        )
        start = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        prev = ""
        for row_index, doc_id in enumerate(_DOCS):
            matter_id = f"DEMO-MATTER-{row_index + 1}"
            timestamp = start + timedelta(minutes=row_index * 15)
            entry_id = f"row-{doc_id}"
            attempts = [
                {
                    "attempt_id": f"{doc_id}-extract-1",
                    "parent_id": entry_id,
                    "doc_id": doc_id,
                    "stage": "extraction",
                    "attempt_no": 1,
                    "trigger": "initial",
                    "outcome": "ok" if row_index != 1 else "retry",
                    "started_at": _iso(timestamp - timedelta(seconds=30)),
                    "ended_at": _iso(timestamp - timedelta(seconds=10)),
                    "detail": {"document_text": SYNTHETIC_TEXT, "fields": ["parties"]},
                }
            ]
            listed = []
            for sub in attempts:
                digest = _attempt_hash(
                    sub,
                    matter_id=matter_id,
                    actor="demo-seeder",
                )
                sub["entry_hash"] = digest or ""
                listed.append(
                    {
                        "attempt_id": sub["attempt_id"],
                        "stage": sub["stage"],
                        "attempt_no": sub["attempt_no"],
                        "entry_hash": digest or "",
                    }
                )
            event = "failed" if row_index == 1 else "archived"
            detail = _detail({"attempts": listed, "stage": event})
            digest = _entry_hash(
                prev_hash=prev,
                doc_id=doc_id,
                entry_id=entry_id,
                event=event,
                detail=detail,
                matter_id=matter_id,
                actor="demo-seeder",
                timestamp=timestamp,
            )
            conn.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    doc_id,
                    matter_id,
                    event,
                    "demo-seeder",
                    json.dumps(detail),
                    prev,
                    digest,
                    row_index + 1,
                    _iso(timestamp),
                ),
            )
            for sub in attempts:
                conn.execute(
                    "INSERT INTO audit_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sub["attempt_id"],
                        sub["parent_id"],
                        sub["doc_id"],
                        sub["stage"],
                        sub["attempt_no"],
                        sub["trigger"],
                        sub["outcome"],
                        sub["started_at"],
                        sub["ended_at"],
                        json.dumps(sub["detail"]),
                        sub["entry_hash"],
                    ),
                )
            prev = digest

        # One addendum on the first document, chained from its row hash, plus a
        # migrated legacy section for the third document.
        first_row = conn.execute(
            "SELECT doc_id, matter_id, entry_id, entry_hash FROM audit_log WHERE doc_id = ?",
            (_DOCS[0],),
        ).fetchone()
        if first_row is not None:
            matter_id = first_row[1]
            timestamp = start + timedelta(hours=1)
            addendum_id = f"add-{_DOCS[0]}-1"
            detail = _detail({"reason": "rescore"})
            digest = _entry_hash(
                prev_hash=first_row[3],
                doc_id=first_row[0],
                entry_id=addendum_id,
                event="rescore",
                detail=detail,
                matter_id=matter_id,
                actor="demo-seeder",
                timestamp=timestamp,
            )
            conn.execute(
                "INSERT INTO audit_addenda VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    addendum_id,
                    first_row[0],
                    "rescore",
                    1,
                    "demo-seeder",
                    _iso(timestamp),
                    json.dumps(detail),
                    first_row[3],
                    digest,
                ),
            )

        legacy_doc = _DOCS[2]
        legacy_prev = ""
        for step, event in enumerate(("ingested", "classified", "archived")):
            timestamp = start - timedelta(hours=2) + timedelta(minutes=step)
            entry_id = f"legacy-{legacy_doc}-{step + 1}"
            detail = _detail({"stage": event})
            digest = _entry_hash(
                prev_hash=legacy_prev,
                doc_id=legacy_doc,
                entry_id=entry_id,
                event=event,
                detail=detail,
                matter_id="DEMO-MATTER-3",
                actor="demo-seeder",
                timestamp=timestamp,
            )
            conn.execute(
                "INSERT INTO audit_log_legacy VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    legacy_doc,
                    "DEMO-MATTER-3",
                    event,
                    "demo-seeder",
                    json.dumps(detail),
                    legacy_prev,
                    digest,
                    step + 1,
                    _iso(timestamp),
                ),
            )
            legacy_prev = digest
        conn.commit()
    finally:
        conn.close()
    return list(_DOCS)


def _checkout_data_target(start: Path | None = None) -> Path | None:
    """The ``data/`` store of the repository checkout containing this module.

    The working-directory check only covers a seed run started from the repo
    root. This walks up from the installed module (editable installs point at
    the checkout) and recognizes a mailroom checkout by its root
    ``pyproject.toml`` plus the pipeline taxonomy.
    """
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "config" / "taxonomy.yaml"
        ).is_file():
            return parent / "data" / "mailroom.db"
    return None


def _configured_targets() -> list[Path]:
    """Database files this process would treat as the live store.

    ``--force`` may replace a scratch database, never the one the configured
    base directory, ``DATABASE_URL``, the current checkout or the repository
    checkout containing this package points at.
    """
    targets: list[Path] = []
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).expanduser().resolve()
    targets.append(base / "mailroom.db")
    targets.append(Path.cwd().resolve() / "data" / "mailroom.db")
    checkout_target = _checkout_data_target()
    if checkout_target is not None:
        targets.append(checkout_target)
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("sqlite") and "///" in url:
        raw = url.split("///", 1)[1].split("?", 1)[0]
        if raw:
            targets.append(Path(raw).expanduser().resolve())
    return targets


def _prepare(db_path: Path, *, force: bool) -> None:
    """Create the parent directory and refuse production/existing targets."""
    target = db_path.expanduser().resolve()
    if target.name != "mailroom.db":
        raise SystemExit("refusing to seed: the file must be named mailroom.db")
    for protected in _configured_targets():
        if target == protected:
            raise SystemExit(
                "refusing to seed the configured audit store (MAILROOM_BASE_DIR or "
                "DATABASE_URL); pass a temporary --out directory"
            )
    if target.parent == Path(target.anchor):
        raise SystemExit("refusing to seed a filesystem root")
    if target.exists() and not force:
        raise SystemExit("refusing to overwrite an existing mailroom.db; pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()


def seed(db_path: Path, *, schema: str = "event-chain", force: bool = False) -> list[str]:
    if schema == "event-chain":
        return seed_event_chain(db_path, force=force)
    if schema == "unified":
        return seed_unified(db_path, force=force)
    raise SystemExit(f"unknown schema {schema!r}; expected event-chain or unified")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailroom-diagnostics-seed-audit")
    parser.add_argument("--out", type=Path, required=True, help="explicit temporary output directory")
    parser.add_argument("--schema", choices=("event-chain", "unified"), default="event-chain")
    parser.add_argument("--force", action="store_true", help="overwrite an existing mailroom.db")
    args = parser.parse_args(argv)
    docs = seed(args.out.expanduser() / "mailroom.db", schema=args.schema, force=args.force)
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "schema": args.schema,
                "documents": docs,
                "database": str((args.out.expanduser() / "mailroom.db").resolve()),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    raise SystemExit(main())
