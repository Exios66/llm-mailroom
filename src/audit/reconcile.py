"""``audit-reconcile``: write the rows that waited in the trace sinks (spec §5.6 step 3).

Reads every sink's pending records, drops copies whose hashes do not match,
deduplicates by ``doc_id`` and ``detail_sha256`` and writes each document's
record as a normal row with ``detail.recovered_from``. A complete copy is
always preferred; with only a decisions-only copy the row is written with
``recovered_from.complete = false`` (``audit_gap_partial``). A document that
already has a row is skipped, except a partial row whose complete copy turns
up later: that becomes one ``recovered_detail`` addendum (``audit_gap_closed``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    recovered: int = 0
    partial: int = 0
    gap_closed: int = 0
    skipped_existing: int = 0
    rejected_hash: int = 0
    dry_run: bool = False
    docs: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the counters, dry-run flag and per-document actions as a dict."""
        return {k: getattr(self, k) for k in ("recovered", "partial", "gap_closed", "skipped_existing",
                                               "rejected_hash", "dry_run", "docs")}


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _attempts(record: dict[str, Any]):
    from audit.models import AttemptRow

    return [
        AttemptRow(
            attempt_id=s["attempt_id"], stage=s["stage"], attempt_no=s["attempt_no"], trigger=s["trigger"],
            outcome=s.get("outcome"), started_at=_ts(s.get("started_at")), ended_at=_ts(s.get("ended_at")),
            detail=s.get("detail") or {},
        )
        for s in record.get("sub_rows") or []
    ]


def hash_ok(record: dict[str, Any]) -> bool:
    """Check the captured detail and sub-row hashes; reject malformed records."""
    from audit.digests import detail_sha256
    from audit.writer import _build_sub_rows

    try:
        detail = record["detail"]
        if detail_sha256(detail) != record["detail_sha256"]:
            return False
        _, listed = _build_sub_rows(
            _attempts(record), parent_id=record["entry_id"], doc_id=record["doc_id"],
            matter_id=record["matter_id"], actor=record["actor"],
        )
        return listed == detail.get("attempts") and [x["entry_hash"] for x in listed] == list(
            record.get("sub_row_hashes") or [])
    except Exception:
        return False


def _gather() -> list[dict[str, Any]]:
    from audit import sinks

    out = []
    for sink in sinks.active_sinks():
        try:
            for rec in sink.fetch_pending() or []:
                out.append({**rec, "_sink": sink.name})
        except Exception:
            logger.warning("audit_reconcile_sink_read_failed", sink=sink.name, exc_info=True)
    return out


def _drop(records: list[dict[str, Any]]) -> None:
    from audit import sinks

    by_name = {s.name: s for s in sinks.active_sinks()}
    for rec in records:
        sink = by_name.get(rec.get("_sink"))
        if sink is not None and hasattr(sink, "drop_pending") and rec.get("ref"):
            try:
                sink.drop_pending(rec["ref"])
            except Exception:
                logger.warning("audit_reconcile_drop_failed", sink=sink.name, ref=rec.get("ref"), exc_info=True)


def _recovered_from(rec: dict[str, Any]) -> dict[str, Any]:
    out = {"sink": rec["_sink"], "ref": rec.get("ref"), "captured_at": rec.get("captured_at"),
           "detail_sha256": rec["detail_sha256"]}
    if not rec.get("complete", True):
        out.update({"complete": False, "full_detail_sha256": rec.get("full_detail_sha256")})
    return out


def _row_of(rec: dict[str, Any]):
    from audit.models import TerminalRow

    return TerminalRow(doc_id=rec["doc_id"], matter_id=rec["matter_id"], event=rec["event"], actor=rec["actor"],
                       detail={k: v for k, v in rec["detail"].items() if k != "attempts"},
                       attempts=_attempts(rec))


async def reconcile(*, dry_run: bool = False) -> ReconcileReport:
    """Reconcile valid pending sink records and report the action for each document.

    Prefer a complete copy; a partial row is supplemented by a matching complete
    copy in a later run. ``dry_run`` counts proposed actions without writing rows,
    addenda or manifests or dropping sink copies. Read and write errors from the
    audit database propagate; sink fetch and drop failures are logged.
    """
    from audit.models import Addendum
    from audit.row import mark_manifest
    from audit.writer import get_row, write_addendum, write_terminal_row

    report = ReconcileReport(dry_run=dry_run)
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in _gather():
        if not hash_ok(rec):
            report.rejected_hash += 1
            logger.error("audit_reconcile_hash_mismatch", doc_id=rec.get("doc_id"), sink=rec.get("_sink"),
                         ref=rec.get("ref"))
            continue
        by_doc[rec["doc_id"]].append(rec)

    for doc_id, copies in sorted(by_doc.items()):
        unique: dict[str, dict[str, Any]] = {}
        for rec in copies:
            unique.setdefault(rec["detail_sha256"], rec)
        complete = [r for r in unique.values() if r.get("complete", True)]
        row = await get_row(doc_id)
        if row is not None:
            rf = (row.get("detail") or {}).get("recovered_from") or {}
            closed = any(a["kind"] == "recovered_detail" for a in row.get("addenda") or [])
            if rf.get("complete") is False and not closed and complete:
                match = [r for r in complete if r["detail_sha256"] == rf.get("full_detail_sha256")]
                report.rejected_hash += len(complete) - len(match)
                for r in complete:
                    if r not in match:
                        logger.error("audit_reconcile_hash_mismatch", doc_id=doc_id, sink=r["_sink"],
                                     ref=r.get("ref"), reason="not_the_partial_rows_full_record")
                if match:
                    rec = match[0]
                    report.gap_closed += 1
                    report.docs.append({"doc_id": doc_id, "action": "recovered_detail"})
                    if not dry_run:
                        await write_addendum(Addendum(
                            doc_id=doc_id, kind="recovered_detail", actor="audit-reconcile",
                            # The sub-rows ride in the detail: their attempt ids are the partial row's.
                            detail={"detail": rec["detail"], "sub_rows": rec["sub_rows"],
                                    "recovered_from": _recovered_from(rec)},
                        ))
                        mark_manifest(doc_id, "written")
                        logger.info("audit_gap_closed", doc_id=doc_id, sink=rec["_sink"])
                        _drop(copies)
                    continue
            report.skipped_existing += 1
            report.docs.append({"doc_id": doc_id, "action": "skipped_existing"})
            if not dry_run and (rf.get("complete") is not False or closed):
                _drop(copies)  # the row (and any recovered detail) is in place: the copies are spent
            continue

        rec = complete[0] if complete else next(iter(unique.values()))
        is_partial = not rec.get("complete", True)
        report.docs.append({"doc_id": doc_id, "action": "partial" if is_partial else "recovered"})
        if is_partial:
            report.partial += 1
        else:
            report.recovered += 1
        if dry_run:
            continue
        await write_terminal_row(_row_of(rec), entry_id=rec["entry_id"], recovered_from=_recovered_from(rec))
        mark_manifest(doc_id, "written")
        if is_partial:
            logger.error("audit_gap_partial", doc_id=doc_id, sink=rec["_sink"],
                         full_detail_sha256=rec.get("full_detail_sha256"))
            # Keep the copies: a complete one may still turn up in another sink.
        else:
            logger.info("audit_row_recovered", doc_id=doc_id, sink=rec["_sink"])
            _drop(copies)
    return report


__all__ = ["ReconcileReport", "hash_ok", "reconcile"]
