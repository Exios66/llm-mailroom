#!/usr/bin/env python3
"""Export human review resolutions as calibration labels.

Reads every manifest under the manifests bin that carries a ``human_label``
(written when a person resolves a parked document) and emits one ``RunRecord``
JSONL row per document (``run.mode="real"``, ``gold.label_source="human"``),
so ``calibrate_thresholds.py`` and ``hillclimb_eval.py`` can fit thresholds
against what reviewers actually decided. A summary says how much review effort
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


def is_corrected(label: dict[str, Any]) -> bool:
    snap = label.get("model_snapshot") or {}
    if label.get("field_corrections"):
        return True
    human_class = label.get("doc_type")
    return bool(human_class and snap.get("doc_type") and human_class != snap.get("doc_type"))


def is_wasted(label: dict[str, Any]) -> bool:
    """Model class confirmed, nothing edited, approved as is: the review added nothing."""
    snap = label.get("model_snapshot") or {}
    human_class = label.get("doc_type")
    return bool(
        label.get("decision") == "approved"
        and label.get("resolution") == "approve_as_is"
        and human_class
        and human_class == snap.get("doc_type")
        and not label.get("field_corrections")
    )


def to_run_record(manifest: dict[str, Any], run_id: str) -> RunRecord:
    label = manifest["human_label"]
    snap = label.get("model_snapshot") or {}
    human_fields = label.get("field_corrections")
    sorter = [
        SorterAttempt(
            attempt=1,
            doc_type=snap.get("doc_type"),
            doc_subclass=snap.get("doc_subclass"),
            confidence=_num(snap.get("classification_confidence")),
        )
    ]
    reviewer_ran = snap.get("reviewer_doc_type") is not None
    reviewer = ReviewerObservation(
        ran=reviewer_ran,
        reviewer_doc_type=snap.get("reviewer_doc_type"),
        reviewer_confidence=_num(snap.get("reviewer_confidence")),
        causes=[c for c in (manifest.get("review_causes") or []) if isinstance(c, str)],
    )
    extraction: list[ExtractionAttempt] = []
    # A person-supplied extraction carries no model confidence worth calibrating.
    if manifest.get("extraction_source") != _SOURCE_HUMAN and _num(
        snap.get("extraction_confidence")
    ) is not None:
        extraction.append(
            ExtractionAttempt(attempt=1, confidence=_num(snap.get("extraction_confidence")))
        )
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
            sorter_attempts=1,
            reviewer=reviewer_ran,
            extraction_attempts=len(extraction),
            full_information=False,
        ),
        actual=ActualRoute(
            final_stage=str(manifest.get("stage") or "") or None,
            review_causes=list(reviewer.causes),
        ),
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
