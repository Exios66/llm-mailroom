"""``audit-reconcile``: write the rows that waited in the trace sinks (spec §5.6 step 3).

Reads every sink's pending records, drops copies whose hashes do not match,
deduplicates by ``doc_id`` and ``detail_sha256`` and writes each document's
record as a normal row with ``detail.recovered_from``. A complete copy is
always preferred; with only a decisions-only copy the row is written with
``recovered_from.complete = false`` (``audit_gap_partial``). A document that
already has a row is not written again, with two exceptions: a partial row whose
complete copy turns up later gets one ``recovered_detail`` addendum
(``audit_gap_closed``), and a copy that is a later terminal write of the document
(its content is not in the row or its addenda) becomes a ``resumed`` addendum, as the
live writer records a repeat write (``audit_repeat_write_recovered``). A copy whose
digest or content the document already holds is spent and only dropped; the manifest
is set once the document's records are confirmed.
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
    resumed: int = 0
    skipped_existing: int = 0
    rejected_hash: int = 0
    failed: int = 0
    dry_run: bool = False
    docs: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the counters, dry-run flag and per-document actions as a dict."""
        return {k: getattr(self, k) for k in ("recovered", "partial", "gap_closed", "resumed",
                                               "skipped_existing", "rejected_hash", "failed", "dry_run", "docs")}


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


def _content_sha(detail: Any) -> str | None:
    """Digest of what a run recorded, without the sub-row references and the repeat-write marker.

    A live write and a parked copy of the same run differ in ``attempts`` (the sub-row hashes
    cover each row's own id) and a ``resumed`` addendum adds ``event``; the rest is the run.
    """
    from audit.digests import detail_sha256

    try:
        return detail_sha256({k: v for k, v in (detail or {}).items() if k not in ("attempts", "event")})
    except Exception:
        return None


def _present_shas(row: dict[str, Any]) -> set[str]:
    """Digests of every record the document's row and addenda already hold."""
    out: set[str] = set()

    def note_recovered(detail: Any) -> None:
        rf = (detail or {}).get("recovered_from") if isinstance(detail, dict) else None
        if isinstance(rf, dict):
            out.update(v for v in (rf.get("detail_sha256"), rf.get("full_detail_sha256")) if v)

    detail = row.get("detail") or {}
    note_recovered(detail)
    out.add(_content_sha(detail))
    for add in row.get("addenda") or []:
        ad = add.get("detail") or {}
        note_recovered(ad)
        if add.get("kind") == "resumed":
            out.add(_content_sha(ad))
        elif add.get("kind") == "recovered_detail":
            out.add(_content_sha(ad.get("detail")))
    out.discard(None)
    return out


def _fresh(unique: list[dict[str, Any]], present: set[str]) -> list[dict[str, Any]]:
    """The copies the document does not hold yet: later terminal writes that parked, earliest first.

    A complete copy is spent when its digest, or its content, is already present. A decisions-only
    copy is spent when the complete copy it reduces is present or is being written.
    """
    complete_shas = {c["detail_sha256"] for c in unique if c.get("complete", True)}
    out = []
    for c in unique:
        if c.get("complete", True):
            spent = c["detail_sha256"] in present or _content_sha(c["detail"]) in present
        else:
            full = c.get("full_detail_sha256")
            spent = c["detail_sha256"] in present or full in present or full in complete_shas
        if not spent:
            out.append(c)
    return sorted(out, key=lambda c: str(c.get("captured_at") or ""))


async def _write_repeat(doc_id: str, rec: dict[str, Any]) -> None:
    """Write a parked repeat terminal write the way the live writer does: a ``resumed`` addendum.

    Same shape as ``writer._as_resumed`` (the requested event rides in ``detail.event``, the attempt
    ids are new), plus ``recovered_from``. Its ``detail_sha256`` is what keeps a copy that turns up
    again from being written twice.
    """
    from audit.models import Addendum
    from audit.writer import write_addendum

    await write_addendum(Addendum(
        doc_id=doc_id, kind="resumed", actor=rec["actor"],
        detail={**rec["detail"], "event": rec["event"], "recovered_from": _recovered_from(rec)},
        attempts=_attempts(rec), reidentify_attempts=True,
    ))
    logger.info("audit_repeat_write_recovered", doc_id=doc_id, sink=rec["_sink"], ref=rec.get("ref"),
                complete=rec.get("complete", True))


async def _reconcile_repeats(report: ReconcileReport, doc_id: str, row: dict[str, Any],
                             copies: list[dict[str, Any]], unique: list[dict[str, Any]], *, dry_run: bool) -> None:
    """A document with a (complete) row: its copies are spent, or later terminal writes to record."""
    from audit.row import mark_manifest

    fresh = _fresh(unique, _present_shas(row))
    if not fresh:
        report.skipped_existing += 1
        report.docs.append({"doc_id": doc_id, "action": "skipped_existing"})
    for _ in fresh:
        report.resumed += 1
        report.docs.append({"doc_id": doc_id, "action": "resumed"})
    if dry_run:
        return
    for rec in fresh:
        await _write_repeat(doc_id, rec)
    # The row (and every repeat write) is in place: the copies are spent. A manifest still
    # ``pending`` (the crash window between the write and the mark) is closed here too.
    mark_manifest(doc_id, "written")
    _drop(copies)


async def _reconcile_doc(report: ReconcileReport, doc_id: str, copies: list[dict[str, Any]], *,
                         dry_run: bool) -> bool:
    """Reconcile one document. Return ``False`` when another writer committed its row first.

    The existence check and the write are separate transactions, so a concurrent
    reconcile (or the live writer) can win in between. The write therefore asks
    for ``if_absent``; on ``AuditRowExists`` nothing has been counted or dropped
    and the caller runs the document again, now through the existing-row path.

    Several different copies of one document are several terminal writes: the
    earliest becomes the row and each later one a ``resumed`` addendum, as the live
    writer would have recorded them.
    """
    from audit.models import Addendum, AuditRowExists
    from audit.row import mark_manifest
    from audit.writer import get_row, write_addendum, write_terminal_row

    unique_by_sha: dict[str, dict[str, Any]] = {}
    for rec in copies:
        unique_by_sha.setdefault(rec["detail_sha256"], rec)
    unique = sorted(unique_by_sha.values(), key=lambda c: str(c.get("captured_at") or ""))
    complete = [r for r in unique if r.get("complete", True)]
    row = await get_row(doc_id)
    if row is not None:
        rf = (row.get("detail") or {}).get("recovered_from") or {}
        closed = any(a["kind"] == "recovered_detail" for a in row.get("addenda") or [])
        if rf.get("complete") is False and not closed:
            if complete:
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
                    return True
            # The partial row stays open: keep every copy, a complete one may still turn up.
            report.skipped_existing += 1
            report.docs.append({"doc_id": doc_id, "action": "skipped_existing"})
            return True
        await _reconcile_repeats(report, doc_id, row, copies, unique, dry_run=dry_run)
        return True

    rec = complete[0] if complete else unique[0]
    is_partial = not rec.get("complete", True)
    if not dry_run:
        try:
            await write_terminal_row(_row_of(rec), entry_id=rec["entry_id"], recovered_from=_recovered_from(rec),
                                     if_absent=True)
        except AuditRowExists:
            logger.info("audit_reconcile_lost_race", doc_id=doc_id)
            return False
        mark_manifest(doc_id, "written")
    report.docs.append({"doc_id": doc_id, "action": "partial" if is_partial else "recovered"})
    if is_partial:
        report.partial += 1
    else:
        report.recovered += 1
    if is_partial:
        if not dry_run:
            logger.error("audit_gap_partial", doc_id=doc_id, sink=rec["_sink"],
                         full_detail_sha256=rec.get("full_detail_sha256"))
        # Keep the copies: a complete one may still turn up in another sink.
        return True
    # The copies of this very write are spent; any other copy is a later terminal write of the document.
    mine = [c for c in copies if rec["detail_sha256"] in (c["detail_sha256"], c.get("full_detail_sha256"))]
    rest = [c for c in copies if not any(c is m for m in mine)]
    if dry_run:
        unique_rest = [c for c in unique if not any(c is m for m in mine)]
        fresh = _fresh(unique_rest, {rec["detail_sha256"], _content_sha(rec["detail"])} - {None})
        for _ in fresh:
            report.resumed += 1
            report.docs.append({"doc_id": doc_id, "action": "resumed"})
        return True
    logger.info("audit_row_recovered", doc_id=doc_id, sink=rec["_sink"])
    _drop(mine)
    if rest:
        return await _reconcile_doc(report, doc_id, rest, dry_run=dry_run)
    return True


async def reconcile(*, dry_run: bool = False) -> ReconcileReport:
    """Reconcile valid pending sink records and report the action for each document.

    Prefer a complete copy; a partial row is supplemented by a matching complete
    copy in a later run. ``dry_run`` counts proposed actions without writing rows,
    addenda or manifests or dropping sink copies. Read and write errors from the
    audit database propagate; sink fetch and drop failures are logged. Runs may
    overlap: a document whose row appears between the check and the write is
    reported as ``skipped_existing`` (never as a ``resumed`` addendum).
    """
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
        # A lost race leaves a row behind, so the second pass takes the existing-row path.
        if not await _reconcile_doc(report, doc_id, copies, dry_run=dry_run):
            await _reconcile_doc(report, doc_id, copies, dry_run=dry_run)
    return report


__all__ = ["ReconcileReport", "hash_ok", "reconcile"]
