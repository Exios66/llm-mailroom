"""Re-score stored predictions under the current scorer (issue #97).

The re-score path is deliberately narrow: it takes records that still carry
their ground truth and prediction, re-runs the current deterministic scorer,
and stamps a fresh ``scorer_version``. It never reconstructs a missing side
from a stored score and never touches serving telemetry (timings and hardware
counters are not reproducible from a saved card).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..config import TASK_KINDS
from ..metrics.scorecard_honesty import SCORER_VERSION
from .tasks import score_task

__all__ = [
    "RescoreError",
    "rescore_record",
    "rescore_records",
    "rescore_file",
    "diff_scorecards",
]


class RescoreError(ValueError):
    """A record cannot be re-scored without inventing missing evidence."""


_EXTRACTION_TASKS = frozenset({"extraction", "archive", "field_extraction"})
_SERVING_TASKS = frozenset({"serving", "local_vs_api"})


def _headline(payload: Mapping[str, Any] | None) -> float | None:
    """Pick the documented headline number from a score payload, if present."""
    if not isinstance(payload, Mapping):
        return None
    for key in (
        "overall_score",
        "exact_match",
        # transcription (WER/CER) and MAUD extraction headlines
        "word_accuracy",
        "maud_question_accuracy",
        "accuracy",
        "overall_extraction_score",
        "f1_macro",
        "macro_f1",
        "mae",
        "score",
    ):
        value = payload.get(key)
        if _is_number(value):
            return float(value)
    return None


def _extraction_headline(block: Mapping[str, Any]) -> float | None:
    """An archive block's headline: ``headline_score`` when the type has one, else the field map.

    Contract (CUAD) and merger (MAUD) rows are judged by ``headline_score``
    (0.27.1); every other row keeps ``overall_score``.
    """
    value = block.get("headline_score")
    if _is_number(value):
        return float(value)
    return _headline(block)


def _is_number(value: Any) -> bool:
    """A finite int/float that is not a bool (stored NaN / Infinity never counts)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite(value: Any) -> Any:
    """``value`` with non-finite floats replaced by ``None``, so output stays strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    """Require a mapping, parsing the JSON-object strings used by stored rows."""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise RescoreError(f"{field} is not a JSON object: {exc}") from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise RescoreError(f"{field} must be an object, got {type(value).__name__}")


#: Scoring kinds whose scorers accept mappings or scalar text, not only arrays.
#: Matched on the resolved kind, so aliases (``wer``, ``cer``, ``maud_clause``)
#: take the same path.
_RAW_INPUT_KINDS = frozenset({"maud_extraction", "transcription", "intake"})


def _raw_input(value: Any) -> Any:
    """Normalise a raw-input side without forcing it into an array.

    Tuples become lists and JSON-array strings are decoded, so a stored batch
    is still scored as a batch; scalar text and mappings pass through.
    """
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, list):
            return parsed
    return value


def _sequence(value: Any, field: str) -> list[Any]:
    """Require a list, parsing the JSON-array strings used by stored rows."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise RescoreError(f"{field} is not a JSON array: {exc}") from exc
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    raise RescoreError(f"{field} must be an array, got {type(value).__name__}")


def _provenance(given: Mapping[str, Any] | None) -> dict[str, Any]:
    """Current-release provenance, with caller evidence retained."""
    block = dict(given or {})
    block["scorer_version"] = SCORER_VERSION
    return block


def _rescore_extraction(record: Mapping[str, Any], given: Mapping[str, Any] | None) -> dict[str, Any]:
    """Re-score one extraction record through the archive contract."""
    from ..pipeline.archive import score_archive_block

    doc_class = (
        record.get("doc_class")
        or record.get("doc_type")
        or record.get("expected_doc_class")
    )
    if not doc_class:
        raise RescoreError("extraction record has no doc_class")
    expected = record.get("expected", record.get("expected_fields"))
    predicted = record.get("predicted", record.get("pred"))
    if expected is None:
        raise RescoreError("extraction record has no expected fields")
    if predicted is None:
        raise RescoreError("extraction record has no predicted fields")
    field_types = record.get("field_types")
    if field_types is not None:
        field_types = dict(_mapping(field_types, "field_types"))
    block = score_archive_block(
        str(doc_class),
        _mapping(predicted, "predicted"),
        _mapping(expected, "expected"),
        field_types=field_types,
        field_map=str(record.get("field_map") or "taxonomy.yaml"),
        doc_text=record.get("doc_text"),
        provenance=_provenance(given),
        # Same keys the suites accept (``expected_subclass``, else
        # ``doc_subclass``), so a CMS row stored either way stays CMS.
        expected_subclass=(
            record["expected_subclass"]
            if record.get("expected_subclass") is not None
            else record.get("doc_subclass")
        ),
    )
    return {
        "doc_id": record.get("doc_id", record.get("id", record.get("item_id"))),
        "task": "extraction",
        "doc_class": str(doc_class),
        "score": _extraction_headline(block),
        "payload": block,
        "provenance": block.get("provenance") or _provenance(given),
        "rescored_from": {
            "scorer_version": record.get("scorer_version")
            or (record.get("provenance") or {}).get("scorer_version"),
            "score": record.get("old_score", record.get("score")),
        },
    }


def _rescore_classification(record: Mapping[str, Any], given: Mapping[str, Any] | None) -> dict[str, Any]:
    """Re-score one label-based task through ``score_task``."""
    task = str(record.get("task") or "docclass")
    expected = record.get("expected")
    predicted = record.get("predicted", record.get("pred"))
    if expected is None:
        raise RescoreError("classification record has no expected labels")
    if predicted is None:
        raise RescoreError("classification record has no predicted labels")
    if TASK_KINDS.get(task, task) in _RAW_INPUT_KINDS:
        # These scorers take label maps / scalar text themselves; forcing an
        # array would refuse valid stored records.
        expected_list, predicted_list = _raw_input(expected), _raw_input(predicted)
    else:
        expected_list = _sequence(expected, "expected")
        predicted_list = _sequence(predicted, "predicted")
    kwargs: dict[str, Any] = {}
    if record.get("expected_subclass") is not None:
        kwargs["expected_subclass"] = _sequence(record["expected_subclass"], "expected_subclass")
    if record.get("predicted_subclass") is not None:
        kwargs["predicted_subclass"] = _sequence(record["predicted_subclass"], "predicted_subclass")
    if record.get("valid") is not None:
        kwargs["valid"] = record["valid"]
    if record.get("categories") is not None:
        kwargs["categories"] = _sequence(record["categories"], "categories")
    payload = score_task(task, expected_list, predicted_list, **kwargs)
    payload["provenance"] = _provenance(given)
    return {
        "doc_id": record.get("doc_id", record.get("id", record.get("item_id"))),
        "task": task,
        "doc_class": record.get("doc_class"),
        "score": _headline(payload),
        "payload": payload,
        "provenance": payload["provenance"],
        "rescored_from": {
            "scorer_version": record.get("scorer_version")
            or (record.get("provenance") or {}).get("scorer_version"),
            "score": record.get("old_score", record.get("score")),
        },
    }


def _reviewer_headline(payload: Mapping[str, Any]) -> Any:
    """The review lane's headline: ``quality.class_accuracy`` unless a generic one exists."""
    quality = payload.get("quality") or {}
    headline = _headline(quality)
    if headline is None:
        headline = quality.get("class_accuracy")
    return headline


def _rescore_reviewer(record: Mapping[str, Any], given: Mapping[str, Any] | None) -> dict[str, Any]:
    """Re-score one reviewer record through the review-lane scorecard."""
    try:
        from .reviewer import score_review_lane
    except ImportError as exc:  # pragma: no cover - package always ships it
        raise RescoreError("reviewer scorecard is not available in this release") from exc

    records = record.get("review_records")
    if not isinstance(records, (list, tuple)) or not records:
        raise RescoreError("reviewer record needs a non-empty review_records list")
    payload = score_review_lane(records)
    if given:
        canonical_metric_id = payload["provenance"].get("metric_id")
        payload["provenance"].update(dict(given))
        if canonical_metric_id is not None:
            # Caller evidence never relabels the measure this lane computed.
            payload["provenance"]["metric_id"] = canonical_metric_id
    payload["provenance"]["scorer_version"] = SCORER_VERSION
    headline = _reviewer_headline(payload)
    return {
        "doc_id": record.get("doc_id", record.get("id", record.get("item_id"))),
        "task": "reviewer",
        "doc_class": None,
        "score": headline,
        "payload": payload,
        "provenance": payload["provenance"],
        "rescored_from": {
            "scorer_version": record.get("scorer_version")
            or (record.get("provenance") or {}).get("scorer_version"),
            "score": record.get("old_score", record.get("score")),
        },
    }


def rescore_record(
    record: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
    force_rescore: bool = False,
) -> dict[str, Any]:
    """Re-score one stored record without inventing a missing side.

    Extraction records need ``doc_class`` plus ``expected``/``predicted``.
    Label records need a task in :data:`TASK_KINDS` plus two parallel lists.
    Reviewer records need ``review_records``. Serving records are refused.
    A record already scored by this release is returned as is unless
    ``force_rescore`` is set.
    """
    task = str(record.get("task") or "").strip().lower()
    if task in _SERVING_TASKS:
        raise RescoreError("serving runs are not re-scorable: timings are not reproducible")
    stored_payload = record.get("payload")
    stored_prov = record.get("provenance")
    if (
        not force_rescore
        and isinstance(stored_payload, Mapping)
        and isinstance(stored_prov, Mapping)
        and str(stored_prov.get("scorer_version")) == SCORER_VERSION
    ):
        # Re-running the output is a no-op, not a missing-evidence error.
        return {
            "doc_id": record.get("doc_id", record.get("id", record.get("item_id"))),
            "task": task or "unknown",
            "doc_class": record.get("doc_class"),
            "score": (
                _reviewer_headline(stored_payload)
                if task == "reviewer"
                # same headline as a fresh extraction rescore (headline_score first)
                else _extraction_headline(stored_payload)
            ),
            "payload": _finite(dict(stored_payload)),
            "provenance": dict(stored_prov),
            "rescored_from": record.get("rescored_from") or {},
            "already_current": True,
        }
    if task in _EXTRACTION_TASKS or (
        not task
        and record.get("doc_class")
        and isinstance(record.get("expected", record.get("expected_fields")), (Mapping, str))
    ):
        return _rescore_extraction(record, provenance)
    if task in ("reviewer", "review_lane", "decision_reviewer"):
        return _rescore_reviewer(record, provenance)
    if task in TASK_KINDS:
        return _rescore_classification({**record, "task": task}, provenance)
    raise RescoreError(
        f"unknown or missing task {task!r}; expected extraction, reviewer, or one of "
        f"{sorted(TASK_KINDS)}"
    )


def rescore_records(
    records: Iterable[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any] | None = None,
    force_rescore: bool = False,
) -> dict[str, Any]:
    """Re-score a batch and report every skipped record with its reason.

    ``force_rescore`` skips the already-current shortcut (used when GT was
    joined from a dataset and may differ from what the payload was scored on).
    Returned records carry no non-finite floats.
    """
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        try:
            results.append(_finite(rescore_record(record, provenance=provenance, force_rescore=force_rescore)))
        except ValueError as exc:  # RescoreError, or a scorer refusing a malformed row
            skipped.append(
                {
                    "index": index,
                    "doc_id": record.get("doc_id", record.get("id", record.get("item_id"))),
                    "reason": str(exc),
                }
            )
    metric_ids = sorted(
        {
            str((result.get("payload") or {}).get("metric_id"))
            for result in results
            if (result.get("payload") or {}).get("metric_id")
        }
    )
    return {
        "scorer_version": SCORER_VERSION,
        "n_records": len(results) + len(skipped),
        "n_rescored": len(results),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "metric_id_conflict": metric_ids if len(metric_ids) > 1 else [],
        "records": results,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read nonblank JSON objects from one JSONL file."""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RescoreError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise RescoreError(f"{path}:{line_number}: expected a JSON object")
        rows.append(row)
    return rows


def _join_dataset(items: list[dict[str, Any]], dataset_path: Path) -> list[dict[str, Any]]:
    """Join run-store items to dataset rows by id, without dropping unmatched rows."""
    truth = {str(row.get("id")): row for row in _read_jsonl(dataset_path)}
    joined: list[dict[str, Any]] = []
    for item in items:
        doc_id = str(item.get("item_id") or item.get("doc_id") or item.get("id") or "")
        row = truth.get(doc_id, {})
        expected = row.get("expected_fields")
        if expected is None and row.get("expected") is not None:
            expected = row["expected"]
        if expected is None:
            # A dataset row without truth must not erase truth the item carries.
            expected = item.get("expected", item.get("expected_fields"))
        expected_subclass = row.get("expected_subclass")
        if expected_subclass is None:
            expected_subclass = item.get("expected_subclass")
        joined.append(
            {
                **item,
                "doc_id": doc_id or None,
                "task": item.get("task") or "extraction",
                "doc_class": item.get("doc_class") or row.get("expected_doc_class"),
                "expected": expected,
                "predicted": item.get("predicted", item.get("pred")),
                "expected_subclass": expected_subclass,
                "predicted_subclass": item.get("predicted_subclass"),
                "scorer_version": item.get("scorer_version")
                or (item.get("provenance") or {}).get("scorer_version"),
            }
        )
    return joined


def rescore_file(
    items_path: str | Path,
    *,
    dataset_path: str | Path | None = None,
    task: str | None = None,
    doc_class: str | None = None,
    out_path: str | Path | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-score a stored run file and write one JSONL record per document.

    ``items_path`` is the run store's ``items.jsonl`` shape (``item_id``,
    ``pred``, ``score``). ``dataset_path`` supplies GT by id. When a record
    already carries ``expected``/``predicted``, no dataset join is needed.
    """
    items = _read_jsonl(Path(items_path))
    records = _join_dataset(items, Path(dataset_path)) if dataset_path else items
    for record in records:
        if task and task != "auto":
            record["task"] = task
        if doc_class:
            record["doc_class"] = doc_class
    # Joined GT may differ from what the stored payload was scored against.
    result = rescore_records(records, provenance=provenance, force_rescore=bool(dataset_path))
    target = Path(out_path or Path(items_path).with_suffix(".rescored.jsonl"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for record in result["records"]:
            handle.write(json.dumps(_finite(record), sort_keys=True, default=str, allow_nan=False) + "\n")
    result["output"] = str(target)
    result["moves"] = diff_scorecards([record for record in records], result["records"])
    return result


def _row_id(row: Mapping[str, Any]) -> str | None:
    """Resolve a stored or re-scored row's id through ``doc_id``/``id``/``item_id``."""
    for key in ("doc_id", "id", "item_id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def diff_scorecards(
    before: Iterable[Mapping[str, Any]],
    after: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize old-to-new headline moves for the records that carry both.

    Records are matched by id (``doc_id``, then ``id``, then ``item_id``, the
    same chain the re-scorers use), so a skipped row cannot shift every later
    comparison. Only a new record without any id falls back to its position;
    an id with no old match is left unpaired rather than paired by position.
    """
    before_rows = list(before)
    after_rows = list(after)
    before_by_id = {
        row_id: row for row in before_rows if (row_id := _row_id(row)) is not None
    }
    old_values: list[float] = []
    new_values: list[float] = []
    changed = 0
    for index, new_record in enumerate(after_rows):
        doc_id = _row_id(new_record)
        if doc_id is not None:
            old_record = before_by_id.get(doc_id)
        else:
            old_record = before_rows[index] if index < len(before_rows) else None
        if old_record is None:
            continue
        old_score = old_record.get("old_score", old_record.get("score"))
        if isinstance(old_score, Mapping):
            old_score = _headline(old_score)
        new_score = new_record.get("score")
        if not _is_number(old_score) or not _is_number(new_score):
            continue
        old_values.append(float(old_score))
        new_values.append(float(new_score))
        if round(float(old_score), 6) != round(float(new_score), 6):
            changed += 1
    n = len(old_values)
    old_mean = sum(old_values) / n if n else None
    new_mean = sum(new_values) / n if n else None
    return {
        "n": n,
        "n_changed": changed,
        "old_mean": old_mean,
        "new_mean": new_mean,
        "delta": (new_mean - old_mean) if n else None,
    }
