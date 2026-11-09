#!/usr/bin/env python3
"""Export human review resolutions as calibration labels.

Reads every manifest under the manifests bin that carries a ``human_label``
(written when a person resolves a parked document) and emits one ``RunRecord``
JSONL row per document (``run.mode="real"``, ``gold.label_source="human"``),
so ``calibrate_thresholds.py`` and ``hillclimb_eval.py`` can fit thresholds
against what reviewers actually decided. The snapshot keeps only the LAST value of
each stage, so a sorter attempt 1 that a retry or a winning reviewer verdict replaced
is exported as unobserved (see ``sorter_attempt_one``), never as attempt 1. A summary says how much review effort
was wasted (the model was right and the person only confirmed it).

Usage::

    PYTHONPATH=src python src/scripts/export_review_labels.py --out labels.jsonl
    PYTHONPATH=src python src/scripts/export_review_labels.py --since 2026-01-01 --summary-only

Read-only on the pipeline: nothing is moved, rewritten or sent anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from schemas.run_record import (
    ActualRoute,
    ExtractionAttempt,
    Gold,
    Observed,
    ReviewerObservation,
    RunInfo,
    RunRecord,
    SorterAttempt,
)

_SOURCE_HUMAN = "human"
# review_classify_node re-labels the document (class AND classification_confidence) only on these.
# ``decision_accepts`` belongs here too (T17): the decision lane's acceptance rewrites the parked
# manifest's ``doc_type`` and ``classification_confidence`` with the REVIEWER's values, so the
# snapshot's numbers are the reviewer's, and unlike ``reviewer_agrees_high`` the class may differ
# from what the sorter extracted — the attempt-1 pair is unrecoverable either way (``review_route``
# is never persisted to the manifest, so the old ``== "extract"`` tell cannot be used).
_REVIEWER_WON = ("reviewer_agrees_high", "reviewer_overrides", "decision_accepts")


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_labelled_manifests(
    manifests_dir: Path, *, since: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """Manifest dicts with a ``human_label``, oldest resolution first."""
    rows: list[tuple[dt.datetime, dict[str, Any]]] = []
    for path in sorted(Path(manifests_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        label = data.get("human_label") if isinstance(data, dict) else None
        if not isinstance(label, dict):
            continue
        resolved = _parse_ts(label.get("resolved_at")) or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc
        )
        if since is not None and resolved < since:
            continue
        rows.append((resolved, data))
    rows.sort(key=lambda item: (item[0], str(item[1].get("doc_id"))))
    return [data for _, data in rows]


def _subclass(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _subclass_edited(label: dict[str, Any], snap: dict[str, Any]) -> bool:
    """A person set, changed or cleared the subclass the model proposed.

    Snapshots written before ``doc_subclass`` was captured carry no such key; they say nothing.
    """
    if "doc_subclass" not in snap:
        return False
    return _subclass(label.get("doc_subclass")) != _subclass(snap.get("doc_subclass"))


def is_corrected(label: dict[str, Any]) -> bool:
    snap = label.get("model_snapshot") or {}
    if label.get("field_corrections"):
        return True
    human_class = label.get("doc_type")
    if human_class and snap.get("doc_type") and human_class != snap.get("doc_type"):
        return True
    return _subclass_edited(label, snap)


def is_wasted(label: dict[str, Any]) -> bool:
    """Model class and subclass confirmed, nothing edited, approved as is: the review added nothing."""
    snap = label.get("model_snapshot") or {}
    human_class = label.get("doc_type")
    return bool(
        label.get("decision") == "approved"
        and label.get("resolution") == "approve_as_is"
        and human_class
        and human_class == snap.get("doc_type")
        and not _subclass_edited(label, snap)
        and not label.get("field_corrections")
    )


def sorter_attempt_one(manifest: dict[str, Any], snap: dict[str, Any]) -> tuple[SorterAttempt | None, str | None]:
    """The first sorter attempt as the snapshot still shows it, or what is left of it.

    ``model_snapshot`` is the manifest as parked, and the manifest keeps only the LAST value of each
    stage: a ``retry_classify`` (``classification_attempts`` > 1) replaces attempt 1, and a winning
    reviewer verdict overwrites ``doc_type``, ``doc_subclass`` and ``classification_confidence`` with
    its own. Labelling those later values "attempt 1" would calibrate the sorter on another model's
    confidence. So the fields that no longer belong to attempt 1 are left unobserved (``None``), and
    with nothing left no attempt is exported. Returns ``(attempt | None, reason | None)``.
    """
    attempts = manifest.get("classification_attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts > 1:
        return None, "sorter_attempt_1_replaced_by_retry"
    reviewer_type = snap.get("reviewer_doc_type")
    verdict = manifest.get("review_verdict")
    if reviewer_type is not None:
        conf, rconf = _num(snap.get("classification_confidence")), _num(snap.get("reviewer_confidence"))
        overwritten = (
            verdict in _REVIEWER_WON
            or manifest.get("review_route") == "extract"
            # Older manifests carry no verdict: the snapshot then showing the reviewer's own pair is the tell.
            or (verdict is None and snap.get("doc_type") == reviewer_type and conf is not None and conf == rconf)
        )
        if overwritten:
            if verdict == "reviewer_agrees_high":  # same class by definition; only the numbers moved
                return SorterAttempt(attempt=1, doc_type=snap.get("doc_type")), "sorter_confidence_overwritten_by_reviewer"
            return None, "sorter_attempt_1_overwritten_by_reviewer"
    return (
        SorterAttempt(
            attempt=1,
            doc_type=snap.get("doc_type"),
            doc_subclass=snap.get("doc_subclass"),
            confidence=_num(snap.get("classification_confidence")),
        ),
        None,
    )


def _extraction_schema_evidence(
    doc_type: Any,
    payload: Any,
    *,
    parse_error: bool = False,
    attempt: int = 1,
) -> tuple[bool | None, bool, list[str]]:
    """Schema/hollow evidence for a parked snapshot's ``extracted_data`` (T1).

    Mirrors ``scripts/run_calibration_sweep.py:_extraction_attempt``: a payload dict is
    validated against the doc class's schema and checked for hollowness, so replay gates on
    what live routing gated on instead of assuming a passing extraction. Semantics:

    - No payload dict in the snapshot (or a non-dict): nothing to validate.
      ``schema_valid`` stays None and the payload is declared not hollow — routing skips
      the schema gate on a falsy extraction and treats a no-payload attempt as a pass.
    - A payload dict (empty or not) is validated: an empty/unparseable payload, a payload
      that fails its class schema or a validator crash all record ``schema_valid`` False
      (the conservative reading: replay retries or parks it), and a crash additionally
      lists the failure under ``diagnostics.schema_validation_errors`` in the sweep's
      format. The snapshot's ``parse_error`` flag is honored too — the parked
      ``extracted_data`` is cleaned of its ``_parse_error`` marker, so the flag is the
      only trace of a parse failure live routing never lets through.
    - A dict that is empty or carries no public fields is hollow (routing's
      ``extraction_hollow_review`` park arm).
    """
    from pipeline.reconsideration import extraction_is_hollow

    if not isinstance(payload, dict):
        return None, False, []
    schema_valid: bool | None
    errors: list[str] = []
    if parse_error:
        schema_valid = False  # a parse failure was parked; mark it invalid too
    else:
        from observability.scores import validate_extraction

        try:
            checked = validate_extraction(str(doc_type or ""), payload).get("schema_valid")
            schema_valid = checked if isinstance(checked, bool) else False
        except Exception as exc:  # scoring must never lose the record
            schema_valid = False
            errors.append(f"{doc_type}#{attempt}: {type(exc).__name__}: {str(exc)[:160]}")
    return schema_valid, extraction_is_hollow(payload), errors


def to_run_record(manifest: dict[str, Any], run_id: str) -> RunRecord:
    label = manifest["human_label"]
    snap = label.get("model_snapshot") or {}
    human_fields = label.get("field_corrections")
    first, unobserved_reason = sorter_attempt_one(manifest, snap)
    sorter = [first] if first is not None else []
    diagnostics: dict[str, Any] = {}
    if unobserved_reason:
        diagnostics["sorter_attempt_1"] = unobserved_reason
    reviewer_ran = snap.get("reviewer_doc_type") is not None
    reviewer = ReviewerObservation(
        ran=reviewer_ran,
        reviewer_doc_type=snap.get("reviewer_doc_type"),
        reviewer_confidence=_num(snap.get("reviewer_confidence")),
        causes=[c for c in (manifest.get("review_causes") or []) if isinstance(c, str)],
    )
    extraction: list[ExtractionAttempt] = []
    schema_errors: list[str] = []
    # A person-supplied extraction carries no model confidence worth calibrating. The snapshot has no
    # provenance field of its own (review_resolve.model_snapshot), so the manifest's final
    # extraction_source is the only signal: when it is human the snapshot's confidence is not trusted.
    if manifest.get("extraction_source") != _SOURCE_HUMAN and _num(
        snap.get("extraction_confidence")
    ) is not None:
        schema_valid, hollow, schema_errors = _extraction_schema_evidence(
            manifest.get("doc_type"),
            snap.get("extracted_data"),
            parse_error=bool(snap.get("parse_error")),
        )
        extraction.append(
            ExtractionAttempt(
                attempt=1,
                confidence=_num(snap.get("extraction_confidence")),
                schema_valid=schema_valid,
                hollow=hollow,
            )
        )
    if schema_errors:
        diagnostics["schema_validation_errors"] = schema_errors
    return RunRecord(
        run=RunInfo(run_id=run_id, mode="real", gt_in_state=False),
        doc_id=str(manifest.get("doc_id") or ""),
        filename=manifest.get("original_filename"),
        gold=Gold(
            expected_doc_type=label.get("doc_type"),
            expected_doc_subclass=label.get("doc_subclass"),
            human_decision=label.get("decision"),
            human_doc_type=label.get("doc_type"),
            human_doc_subclass=label.get("doc_subclass"),
            human_fields=human_fields if isinstance(human_fields, dict) else None,
            label_source="human",
        ),
        sorter=sorter,
        reviewer=reviewer,
        extraction=extraction,
        observed=Observed(
            sorter_attempts=len(sorter),
            reviewer=reviewer_ran,
            extraction_attempts=len(extraction),
            full_information=False,
        ),
        actual=ActualRoute(
            final_stage=str(manifest.get("stage") or "") or None,
            review_causes=list(reviewer.causes),
        ),
        diagnostics=diagnostics,
    )


def summarize(manifests: Iterable[dict[str, Any]]) -> dict[str, Any]:
    labels = [m["human_label"] for m in manifests]
    n = len(labels)
    decisions = Counter(str(l.get("decision")) for l in labels)
    resolutions = Counter(str(l.get("resolution")) for l in labels)
    wasted = sum(is_wasted(l) for l in labels)
    corrected = sum(is_corrected(l) for l in labels)
    per_class: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "agree": 0})
    field_edits: Counter[str] = Counter()
    for label in labels:
        snap = label.get("model_snapshot") or {}
        model_class, human_class = snap.get("doc_type"), label.get("doc_type")
        if model_class and human_class:
            slot = per_class[str(model_class)]
            slot["n"] += 1
            slot["agree"] += int(model_class == human_class)
        corrections = label.get("field_corrections")
        if isinstance(corrections, dict):
            field_edits.update(str(k) for k in corrections)
    for slot in per_class.values():
        slot["agreement"] = round(slot["agree"] / slot["n"], 4) if slot["n"] else None
    return {
        "n_labels": n,
        "decisions": dict(decisions),
        "resolutions": dict(resolutions),
        "wasted_review_rate": round(wasted / n, 4) if n else None,
        "wasted_reviews": wasted,
        "correction_rate": round(corrected / n, 4) if n else None,
        "corrections": corrected,
        "per_class_agreement": dict(sorted(per_class.items())),
        "field_edit_counts": dict(field_edits.most_common()),
    }


def export(
    manifests_dir: Path,
    *,
    out: Path | None = None,
    since: dt.datetime | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    manifests = load_labelled_manifests(manifests_dir, since=since)
    day = (today or dt.date.today()).isoformat()
    run_id = f"human-labels-{day}"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            for manifest in manifests:
                record = to_run_record(manifest, run_id)
                handle.write(record.model_dump_json() + "\n")
    summary = summarize(manifests)
    summary["run_id"] = run_id
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="RunRecord JSONL path (omit for summary only)")
    parser.add_argument("--since", help="only labels resolved on/after this ISO date")
    parser.add_argument("--manifests-dir", type=Path, help="override the manifests bin")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(argv)

    since = _parse_ts(args.since) if args.since else None
    if args.since and since is None:
        print(f"unparseable --since: {args.since}", file=sys.stderr)
        return 2
    mdir = args.manifests_dir
    if mdir is None:
        from pipeline.bins import manifests_dir

        mdir = manifests_dir()
    summary = export(mdir, out=None if args.summary_only else args.out, since=since)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
