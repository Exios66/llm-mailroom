"""Deterministic scoring for LegalBench runs — every number computed locally.

No LLM grading anywhere: scores are derived from the model's structured
answer vs. the corpus ground truth. All functions take the runner's result
rows (each with ``expected``/``predicted``/``correct``/``confidence``) and
return plain dicts that serialize into the experiment-log record and the
site's breakdown card.
"""

from __future__ import annotations

import math
from typing import Any
from .pipeline_scoring import raw_classification_stats
from .equivalences import equivalent_subtypes
from .config import Settings


def _mean(values: list[float]) -> float:
    """Legacy rounded mean, zero for empty support."""
    return round(sum(values) / len(values), 4) if values else 0.0


def _safe_div(num: float, den: float) -> float:
    """Legacy unrounded zero-safe division."""
    return num / den if den else 0.0


def _binary_f1(correct_yes: int, pred_yes: int, label_yes: int) -> float:
    """Binary F1 with unrounded precision/recall until report rounding."""
    precision = _safe_div(correct_yes, pred_yes)
    recall = _safe_div(correct_yes, label_yes)
    return _safe_div(2 * precision * recall, precision + recall)


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 10) -> float:
    """Equal-width binned ECE; preserve the historical direct-call contract.

    Reports select only numeric [0,1] confidences first, excluding NaN/infinity.
    Direct callers must provide valid pairs/bins: NaN propagates; +infinity
    raises OverflowError during binning. Empty support returns 0.0.
    """
    if not pairs:
        return 0.0
    buckets: dict[int, list[tuple[float, bool]]] = {b: [] for b in range(bins)}
    for conf, correct in pairs:
        b = min(bins - 1, int(conf * bins)) if conf > 0 else 0
        buckets[b].append((conf, correct))
    total = 0.0
    weight_sum = 0
    for bucket in buckets.values():
        if not bucket:
            continue
        acc = sum(1 for _, c in bucket if c) / len(bucket)
        conf_mean = sum(c for c, _ in bucket) / len(bucket)
        total += (len(bucket) / len(pairs)) * abs(conf_mean - acc)
        weight_sum += len(bucket)
    return round(total, 4) if weight_sum else 0.0


def score_binary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy, yes-class F1, macro per-category accuracy, calibration."""
    ok = [r for r in results if r.get("status") == "ok" and r.get("predicted") not in (None, "error")]
    per_category: dict[str, list[bool]] = {}
    conf_pairs: list[tuple[float, bool]] = []
    n_yes = n_no = correct_yes = pred_yes = label_yes = 0
    for r in ok:
        cat = r.get("category", "unknown")
        per_category.setdefault(cat, []).append(bool(r["correct"]))
        if r["predicted"] == "yes":
            pred_yes += 1
            if r["correct"]:
                correct_yes += 1
        if r["expected"] == "yes":
            label_yes += 1
            n_yes += 1
        else:
            n_no += 1
        conf = r.get("confidence")
        if isinstance(conf, (int, float)) and 0 <= conf <= 1:
            conf_pairs.append((float(conf), bool(r["correct"])))

    stats = raw_classification_stats(
        [r["expected"] for r in ok], [r["predicted"] for r in ok],
        correct=[r["correct"] for r in ok], groups=[r.get("category", "unknown") for r in ok],
    )
    n = len(ok)
    accuracy = stats["accuracy"]
    macro_cat = round(stats["macro_accuracy"], 4)
    return {
        "accuracy": round(accuracy, 4),
        "macro_category_accuracy": macro_cat,
        "yes_f1": round(_binary_f1(correct_yes, pred_yes, label_yes), 4),
        "n_questions": n,
        "n_yes": n_yes,
        "n_no": n_no,
        "n_error": len(results) - n,
        "confidence_mean": _mean([c for c, _ in conf_pairs]),
        "calibration_error": expected_calibration_error(conf_pairs),
        "per_category": {
            cat: round(_safe_div(sum(v), len(v)), 4) for cat, v in sorted(per_category.items())
        },
    }


def _has_label(value: Any) -> bool:
    """Exclude missing/blank labels and float NaN without discarding zero or False."""
    return (value is not None
            and not (isinstance(value, float) and math.isnan(value))
            and bool(str(value).strip()))


def score_multiclass(results: list[dict[str, Any]], *, subtype_equivalences=None) -> dict[str, Any]:
    """Score labeled family rows with optional immutable consumer equivalences.

    Omitted groups preserve the configured Dojo default. Explicit groups bind
    a producer's compatibility policy without mutating process-wide settings.
    Rows with absent, blank or float-NaN labels count as errors, not support.
    """
    settings = (None if subtype_equivalences is None else
                Settings(subtype_equivalences=list(subtype_equivalences)))
    ok = [r for r in results if r.get("status") == "ok"
          and r.get("predicted") not in (None, "error")
          and _has_label(r.get("predicted")) and _has_label(r.get("expected"))]
    per_family: dict[str, list[bool]] = {}
    conf_pairs: list[tuple[float, bool]] = []
    n_other_pred = n_other_label = 0
    for r in ok:
        fam = r.get("family", r.get("category", "unknown"))
        per_family.setdefault(fam, []).append(bool(r["correct"]))
        if r["predicted"] == "other":
            n_other_pred += 1
        if r["expected"] == "other":
            n_other_label += 1
        conf = r.get("confidence")
        if isinstance(conf, (int, float)) and 0 <= conf <= 1:
            conf_pairs.append((float(conf), bool(r["correct"])))

    n = len(ok)
    strict = _safe_div(sum(1 for r in ok if r["correct"]), n)
    equiv = _safe_div(
        sum(
            1
            for r in ok
            if r["correct"] or equivalent_subtypes(
                r.get("expected", ""), r.get("predicted", ""), settings=settings)
        ),
        n,
    )
    stats = raw_classification_stats(
        [r["expected"] for r in ok], [r["predicted"] for r in ok],
        correct=[r["correct"] for r in ok],
        groups=[r.get("family", r.get("category", "unknown")) for r in ok],
    )
    return {
        "accuracy": round(strict, 4),
        "accuracy_equiv": round(equiv, 4),
        "macro_f1": round(stats["macro_f1"], 4),
        "n_documents": n,
        "n_error": len(results) - n,
        "n_other_predicted": n_other_pred,
        "n_other_labeled": n_other_label,
        "confidence_mean": _mean([c for c, _ in conf_pairs]),
        "calibration_error": expected_calibration_error(conf_pairs),
        "per_family": {
            fam: round(_safe_div(sum(v), len(v)), 4) for fam, v in sorted(per_family.items())
        },
    }
