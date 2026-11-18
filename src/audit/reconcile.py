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

A copy whose write fails for a reason that will not change (an integrity error that retrying cannot
cure, such as a duplicate primary key, or a malformed record) is **quarantined**: moved, not deleted,
to a separate table of the same spool, reported as ``quarantined`` and no longer read as
pending, so later runs do not fail on it forever. Any other failure (a locked database, a
conflict that outlasted its retries, an unrecognised error) leaves the copy in place for the next run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from audit.models import AuditMigrationPending

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
    #: Documents whose copies were moved to quarantine by this run (also counted in ``failed``).
    quarantined: int = 0
    #: Copies the sinks hold in quarantine from this and earlier runs (informational: not a failure).
    quarantine_held: int = 0
    #: Spooled records that are not a JSON object: unreadable, so never recovered (also counted in ``failed``).
    malformed: int = 0
    #: Sinks whose pending records could not be read at all, and how many: the report is then
    #: incomplete, so the CLI exits nonzero rather than reporting a clean, all-zero run.
    sink_read_failed: int = 0
    sink_read_failed_names: list[str] = field(default_factory=list)
    #: Spool files that do not exist (nothing was ever parked there, or the volume is not mounted).
    spool_missing: list[str] = field(default_factory=list)
    dry_run: bool = False
    docs: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the counters, dry-run flag and per-document actions as a dict."""
        return {k: getattr(self, k) for k in ("recovered", "partial", "gap_closed", "resumed",
                                               "skipped_existing", "rejected_hash", "failed", "quarantined",
                                               "quarantine_held", "malformed", "sink_read_failed", "sink_read_failed_names",
                                               "spool_missing", "dry_run", "docs")}


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


def _gather(report: ReconcileReport | None = None) -> list[dict[str, Any]]:
    """Every sink's pending records; a sink that cannot be read is recorded on ``report``.

    A sink that raises from ``fetch_pending`` is an unknown, not an empty sink: it is counted in
    ``report.sink_read_failed`` (and named) so a run that read nothing cannot pass for a clean one.
    A sink whose store does not exist (``missing_store()``) is listed in ``report.spool_missing``
    but is not a failure: a spool is created by the first parked row.
    """
    from audit import sinks

    out = []
    for sink in sinks.active_sinks():
        name = getattr(sink, "name", "?")
        try:
            for rec in sink.fetch_pending() or []:
                out.append({**rec, "_sink": name})
            missing = getattr(sink, "missing_store", None)
            gone = missing() if callable(missing) else None
            if gone and report is not None:
                report.spool_missing.append(str(gone))
            bad = getattr(sink, "malformed_pending", None)
            if callable(bad) and report is not None:
                n = int(bad() or 0)
                report.malformed += n
                report.failed += n
            held = getattr(sink, "quarantined_count", None)
            if callable(held) and report is not None:
                report.quarantine_held += int(held() or 0)
        except Exception:
            logger.warning("audit_reconcile_sink_read_failed", sink=name, exc_info=True)
            if report is not None:
                report.sink_read_failed += 1
                report.sink_read_failed_names.append(name)
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


def _is_deterministic(exc: BaseException) -> bool:
    """True for a failure that will repeat identically on every run, so its copies are quarantined.

    That is an integrity error the writer does not retry (a duplicate primary key: ``_is_retryable``
    is false) and a ``ValueError`` (a malformed record: an unknown event or stage, a bad timestamp).
    Contention, a dropped connection, a conflict that outlasted its retries and anything unrecognised
    (possibly a bug a later deploy fixes) stay pending and are retried by the next run.
    """
    from sqlalchemy.exc import IntegrityError

    from audit.writer import _is_retryable

    if isinstance(exc, IntegrityError):
        return not _is_retryable(exc)
    return isinstance(exc, ValueError)


def _quarantine(records: list[dict[str, Any]], error: str) -> int:
    """Move each record to its sink's quarantine (kept, not deleted); return how many moved."""
    from audit import sinks

    by_name = {s.name: s for s in sinks.active_sinks()}
    moved = 0
    for rec in records:
        sink = by_name.get(rec.get("_sink"))
        if sink is not None and hasattr(sink, "quarantine_pending") and rec.get("ref"):
            try:
                if sink.quarantine_pending(rec["ref"], error):
                    moved += 1
            except Exception:
                logger.warning("audit_reconcile_quarantine_failed", sink=sink.name, ref=rec.get("ref"),
                               exc_info=True)
    return moved


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


def _kept_refs(copies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The manifest's ``audit_pending_refs`` for copies reconcile read back (so they are recoverable)."""
    seen: set[tuple[Any, Any]] = set()
    out = []
    for c in copies:
        key = (c["_sink"], c.get("ref"))
        if key not in seen:
            seen.add(key)
            out.append({"sink": c["_sink"], "ref": c.get("ref"), "complete": bool(c.get("complete", True)),
                        "recoverable": True})
    return out


def _mark_partial(doc_id: str, copies: list[dict[str, Any]], *, row_gap: dict | None = None,
                  observed: Any = None) -> None:
    """Keep the gap on the manifest: ``pending_partial`` with the kept copies, until a complete one closes it.

    A compare-and-set against ``observed`` (the manifest as the sweep saw it before it read the row): a
    second sweep that closed the gap in between has marked ``written`` and dropped the spooled copies,
    and writing ``pending_partial`` over that would point the manifest at copies that no longer exist,
    with nothing left for a later sweep to repair. The mark is then skipped. Without ``observed`` the
    manifest is read here, which still makes the update atomic but not relative to the row read.
    """
    from audit.row import _refs_key, mark_manifest, observe_manifest

    refs = _kept_refs(copies)
    if row_gap and not any((r["sink"], r["ref"]) == (row_gap.get("sink"), row_gap.get("ref")) for r in refs):
        # The original partial row remains incomplete even if its spool is
        # temporarily unreadable. Preserve its provenance without claiming
        # this sweep read that copy back successfully.
        refs.append({"sink": row_gap.get("sink"), "ref": row_gap.get("ref"),
                     "complete": False, "recoverable": False})
    expect = observed if observed is not None else observe_manifest(doc_id)
    if expect is not None and expect.audit_written == "pending_partial" and expect.audit_pending_refs == _refs_key(refs):
        return
    mark_manifest(doc_id, "pending_partial", refs, expect=expect)


def _content_sha(detail: Any, event: Any, sub_rows: Any) -> str | None:
    """Digest of what a run recorded, without the row identities.

    A live write and a parked copy of the same run differ in ``detail.attempts`` (the sub-row hashes
    cover each row's own id) and a ``resumed`` addendum adds ``detail.event``, so those are not
    compared as written. What the run recorded is still compared: the terminal ``event`` and each
    attempt's contents (stage, number, trigger, outcome, times, detail) from ``sub_rows``, without the
    attempt, parent and hash ids and without the ``original_attempt_id`` a repeat write adds.
    """
    import json

    from audit.digests import detail_sha256

    def attempt(sub: dict[str, Any]) -> dict[str, Any]:
        return {
            **{k: sub.get(k) for k in ("stage", "attempt_no", "trigger", "outcome", "started_at", "ended_at")},
            "detail": {k: v for k, v in (sub.get("detail") or {}).items() if k != "original_attempt_id"},
        }

    try:
        attempts = sorted((attempt(s) for s in sub_rows or []),
                          key=lambda a: json.dumps(a, sort_keys=True, default=str))
        body = {k: v for k, v in (detail or {}).items() if k not in ("attempts", "event", "recovered_from")}
        return detail_sha256({"event": event, "detail": body, "attempts": attempts})
    except Exception:
        return None


def _present_shas(row: dict[str, Any]) -> set[str]:
    """Digests of every record the document's row and addenda already hold."""
    out: set[str] = set()

    def note_recovered(detail: Any) -> None:
        rf = (detail or {}).get("recovered_from") if isinstance(detail, dict) else None
        if isinstance(rf, dict):
            out.add(rf.get("detail_sha256"))
            # A decisions-only record names a full record it does not hold: that digest is not on file.
            if rf.get("complete") is not False:
                out.add(rf.get("full_detail_sha256"))

    detail = row.get("detail") or {}
    note_recovered(detail)
    out.add(_content_sha(detail, row.get("event"), row.get("sub_rows")))
    for add in row.get("addenda") or []:
        ad = add.get("detail") or {}
        note_recovered(ad)
        if add.get("kind") == "resumed":
            out.add(_content_sha(ad, ad.get("event"), add.get("sub_rows")))
        elif add.get("kind") == "recovered_detail":
            out.add(_content_sha(ad.get("detail"), row.get("event"), ad.get("sub_rows")))
    out.discard(None)
    out.discard("")
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
            spent = c["detail_sha256"] in present or _content_sha(c["detail"], c.get("event"), c.get("sub_rows")) in present
        else:
            full = c.get("full_detail_sha256")
            spent = c["detail_sha256"] in present or full in present or full in complete_shas
        if not spent:
            out.append(c)
    return sorted(out, key=lambda c: str(c.get("captured_at") or ""))


async def _write_repeat(doc_id: str, rec: dict[str, Any], known: set[str] | None = None) -> bool:
    """Write a parked repeat terminal write the way the live writer does: a ``resumed`` addendum.

    Same shape as ``writer._as_resumed`` (the requested event rides in ``detail.event``, the attempt
    ids are new), plus ``recovered_from``. Its ``detail_sha256`` is what keeps a copy that turns up
    again from being written twice. Return whether a new addendum was written: ``write_addendum``
    returns the existing one (``result.existing``) when a racing reconcile already recorded this copy;
    ``known`` holds the addendum ids seen before the write.
    """
    from audit.models import Addendum
    from audit.writer import write_addendum

    result = await write_addendum(Addendum(
        doc_id=doc_id, kind="resumed", actor=rec["actor"],
        detail={**rec["detail"], "event": rec["event"], "recovered_from": _recovered_from(rec)},
        attempts=_attempts(rec), reidentify_attempts=True,
    ))
    created = not result.existing and result.entry_id not in (known or set())
    if created:
        logger.info("audit_repeat_write_recovered", doc_id=doc_id, sink=rec["_sink"], ref=rec.get("ref"),
                    complete=rec.get("complete", True))
    return created


async def _reconcile_repeats(report: ReconcileReport, doc_id: str, row: dict[str, Any],
                             copies: list[dict[str, Any]], unique: list[dict[str, Any]], *, dry_run: bool,
                             observed: Any = None) -> None:
    """A document with a (complete) row: its copies are spent, or later terminal writes to record."""
    from audit.row import mark_manifest

    present = _present_shas(row)
    fresh = _fresh(unique, present)
    # A decisions-only copy whose full record is neither on file nor in this batch: the gap stays open,
    # so its copy is kept and the manifest keeps showing ``pending_partial`` until the complete copy comes.
    complete_shas = {c["detail_sha256"] for c in unique if c.get("complete", True)}
    open_partials = [c for c in copies if not c.get("complete", True)
                     and c.get("full_detail_sha256") not in present and c.get("full_detail_sha256") not in complete_shas]
    if dry_run:
        # Proposed actions: nothing is written, so the fresh copies are counted as they would be.
        if not fresh:
            report.skipped_existing += 1
            report.docs.append({"doc_id": doc_id, "action": "skipped_existing"})
        for _ in fresh:
            report.resumed += 1
            report.docs.append({"doc_id": doc_id, "action": "resumed"})
        return
    # Count real writes only: a racing reconcile may have recorded a copy since ``row`` was read.
    known = {a.get("addendum_id") for a in row.get("addenda") or []}
    written = 0
    for rec in fresh:
        if await _write_repeat(doc_id, rec, known):
            written += 1
            report.resumed += 1
            report.docs.append({"doc_id": doc_id, "action": "resumed"})
    if not written:
        report.skipped_existing += 1
        report.docs.append({"doc_id": doc_id, "action": "skipped_existing"})
    rf = (row.get("detail") or {}).get("recovered_from") or {}
    row_gap = rf if rf.get("complete") is False and not any(
        a["kind"] == "recovered_detail" for a in row.get("addenda") or []
    ) else None
    if open_partials or row_gap:
        _mark_partial(doc_id, open_partials, row_gap=row_gap, observed=observed)
        _drop([c for c in copies if not any(c is o for o in open_partials)])
        return
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
    from audit.row import mark_manifest, observe_manifest
    from audit.writer import get_row, write_addendum, write_terminal_row

    # The manifest as it stands before the row is read: every decision below rests on that read, so a
    # later "the gap is still open" mark only applies if no other sweep moved the manifest meanwhile.
    observed = observe_manifest(doc_id)
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
                if match:
                    rec = match[0]
                    # Only the copies of this write are spent; any other copy is a later write of the
                    # document and goes through the closed row's path below.
                    mine = [c for c in copies if rec["detail_sha256"] in (c["detail_sha256"], c.get("full_detail_sha256"))]
                    rest = [c for c in copies if not any(c is m for m in mine)]
                    report.gap_closed += 1
                    report.docs.append({"doc_id": doc_id, "action": "recovered_detail"})
                    if dry_run:
                        if rest:
                            unique_rest = [c for c in unique if not any(c is m for m in mine)]
                            for _ in _fresh(unique_rest, _present_shas(row) | {rec["detail_sha256"]}):
                                report.resumed += 1
                                report.docs.append({"doc_id": doc_id, "action": "resumed"})
                        return True
                    await write_addendum(Addendum(
                        doc_id=doc_id, kind="recovered_detail", actor="audit-reconcile",
                        # The sub-rows ride in the detail: their attempt ids are the partial row's.
                        detail={"detail": rec["detail"], "sub_rows": rec["sub_rows"],
                                "recovered_from": _recovered_from(rec)},
                    ))
                    mark_manifest(doc_id, "written")
                    logger.info("audit_gap_closed", doc_id=doc_id, sink=rec["_sink"])
                    _drop(mine)
                    if rest:
                        return await _reconcile_doc(report, doc_id, rest, dry_run=False)
                    return True
            # A different complete copy can be a later terminal write, not a
            # corrupt version of the original. hash_ok already validated it.
            # Recover those repeats while retaining the original row's gap.
            await _reconcile_repeats(report, doc_id, row, copies, unique, dry_run=dry_run, observed=observed)
            return True
        await _reconcile_repeats(report, doc_id, row, copies, unique, dry_run=dry_run, observed=observed)
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
        if is_partial:
            _mark_partial(doc_id, copies, observed=observed)  # the row is only a decisions-only record: the gap stays visible
        else:
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
        fresh = _fresh(unique_rest, {rec["detail_sha256"], _content_sha(rec["detail"], rec.get("event"), rec.get("sub_rows"))} - {None})
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
    copy in a later run; a copy that is a later terminal write of a document with
    a row becomes a ``resumed`` addendum. ``dry_run`` counts proposed actions without
    writing rows, addenda or manifests or dropping sink copies.

    Each document is isolated: an error while reading or writing one (a constraint
    violation, a bad record, a lock that outlasted the retries) is logged as
    ``audit_reconcile_doc_failed``, counted in ``failed`` and listed in ``docs``; its
    copies stay in the sinks for the next run and the other documents are still
    reconciled. ``AuditMigrationPending`` is the one error that propagates: it means
    the audit tables are not migrated, so no document can be written and one error is
    more honest than one failure per document. Sink fetch and drop failures are logged.
    Runs may overlap: a document whose row appears between the check and the write is
    reported as ``skipped_existing`` (never as a ``resumed`` addendum).
    """
    report = ReconcileReport(dry_run=dry_run)
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in _gather(report):
        if not hash_ok(rec):
            report.rejected_hash += 1
            logger.error("audit_reconcile_hash_mismatch", doc_id=rec.get("doc_id"), sink=rec.get("_sink"),
                         ref=rec.get("ref"))
            continue
        by_doc[rec["doc_id"]].append(rec)

    for doc_id, copies in sorted(by_doc.items()):
        try:
            # A lost race leaves a row behind, so the second pass takes the existing-row path.
            if not await _reconcile_doc(report, doc_id, copies, dry_run=dry_run):
                await _reconcile_doc(report, doc_id, copies, dry_run=dry_run)
        except AuditMigrationPending:
            raise  # no document can be written before the migration: stop, do not log one failure per document
        except Exception as exc:  # noqa: BLE001 - one bad document must not strand the ones after it
            error = f"{type(exc).__name__}: {exc}"
            report.failed += 1
            entry = {"doc_id": doc_id, "action": "failed", "error": error}
            logger.error("audit_reconcile_doc_failed", doc_id=doc_id, error=error, exc_info=True)
            moved = _quarantine(copies, error) if _is_deterministic(exc) and not dry_run else 0
            if moved:
                # It fails the same way on every run: keep the copies aside so the next run does not.
                report.quarantined += 1
                report.quarantine_held += moved
                entry["action"] = "quarantined"
                logger.error("audit_reconcile_quarantined", doc_id=doc_id, copies=moved, error=error)
            report.docs.append(entry)
    return report


__all__ = ["ReconcileReport", "hash_ok", "reconcile"]
