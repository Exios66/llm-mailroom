"""Exact pipeline classification and offline lifecycle reports.

These are compatibility policies, separate from regex label recovery and the
sorter failure taxonomy. Raw statistics retain every supplied row, including
missing GT and error predictions; callers choose populations explicitly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable
from ._paired import require_equal_lengths

PIPELINE_ALIGNMENT: dict[str, str] = {}
PIPELINE_FAILURE_MODES = (
    "ok", "wrong_class", "review", "failed", "empty_extract", "error", "unknown_stage",
)


def normalize_class(token: Any) -> str:
    """Trim/lowercase only; never recover labels from prose or JSON."""
    return str(token or "").strip().lower()


def classes_match(expected: Any, predicted: Any) -> bool:
    """Match nonempty normalized class tokens; MAUD remains distinct from CUAD."""
    exp, pred = normalize_class(expected), normalize_class(predicted)
    return bool(exp and pred and exp == pred)


def score_exact_classification(expected: Iterable[Any], predicted: Iterable[Any]) -> dict:
    """Three-decimal pipeline accuracy; deprecated aligned keys equal exact.

    Missing/error predictions are misses. Unequal cardinalities raise before
    scoring, including for generators (the former pipeline zip silently lost GT).
    """
    expected, predicted = list(expected), list(predicted)
    require_equal_lengths(expected, predicted)
    n = len(expected)
    hits = sum(classes_match(e, p) for e, p in zip(expected, predicted))
    acc = round(hits / n, 3) if n else 0.0
    return {"n": n, "exact_n": hits, "exact_accuracy": acc,
            "aligned_n": hits, "aligned_accuracy": acc, "aligned_equals_exact": True}


def _ratio(num: float, den: float) -> float:
    """Unrounded ratio with the legacy zero-denominator convention."""
    return num / den if den else 0.0


def raw_classification_stats(expected: list, predicted: list, *, correct=None,
                             groups=None, label_universe: str = "expected") -> dict:
    """Unrounded raw-label counts for selected rows, with no error filtering.

    Default correctness is the strict pipeline predicate. Supplied correctness
    flags are trusted (LegalBench's producer contract). Groups default to raw
    expected labels; an explicit group vector preserves LegalBench family and
    category support even when group != expected. Expected-only macro F1 omits
    predicted-only classes; union includes false-positive classes. Empty GT
    stays a group and never becomes an automatically correct empty prediction.
    """
    require_equal_lengths(expected, predicted)
    flags = ([classes_match(e, p) for e, p in zip(expected, predicted)]
             if correct is None else [bool(x) for x in correct])
    groups = list(expected) if groups is None else list(groups)
    require_equal_lengths(expected, flags)
    require_equal_lengths(expected, groups)
    if label_universe not in ("expected", "union"):
        raise ValueError("label_universe must be expected or union")
    support, hits, false_positive = Counter(groups), Counter(), Counter()
    for group, pred, ok in zip(groups, predicted, flags):
        if ok:
            hits[group] += 1
        elif pred:
            false_positive[str(pred)] += 1
    labels = set(support) | (set(false_positive) if label_universe == "union" else set())
    per_class = {}
    for label in labels:
        tp, n = hits[label], support[label]
        # LegalBench's grouping contract counts raw predictions in each family;
        # experiment union mode instead assigns successful normalized hits to GT.
        pred_n = (tp + false_positive[label] if label_universe == "union"
                  else sum(p == label for p in predicted))
        precision, recall = _ratio(tp, pred_n), _ratio(tp, n)
        f1 = _ratio(2 * precision * recall, precision + recall)
        per_class[label] = {"n": n, "correct": tp, "precision": precision,
                            "recall": recall, "f1": f1, "accuracy": _ratio(tp, n)}
    n = len(expected)
    return {"n": n, "correct": sum(flags), "accuracy": _ratio(sum(flags), n),
            "macro_accuracy": _ratio(sum(_ratio(hits[g], support[g]) for g in support), len(support)),
            "macro_f1": _ratio(sum(s["f1"] for s in per_class.values()), len(per_class)),
            "per_class": per_class}


def score_experiment_rows(rows: list[dict]) -> dict[str, float]:
    """Four-decimal experiment KPIs; stage accuracy uses only has_stage rows."""
    if not rows:
        return {}
    stats = raw_classification_stats([r["expected"] for r in rows],
                                     [r["got"] for r in rows], label_universe="union")
    out = {"class_accuracy": round(stats["accuracy"], 4),
           "macro_class_accuracy": round(stats["macro_accuracy"], 4),
           "macro_f1": round(stats["macro_f1"], 4)}
    staged = [r for r in rows if r.get("has_stage")]
    if staged:
        out["stage_accuracy"] = round(sum(bool(r["stage_ok"]) for r in staged) / len(staged), 4)
    return out


def pipeline_labels_equal(expected, predicted) -> bool:
    """Literal equality for nonblank labels; missing GT is never a hit."""
    return bool(str(expected or "").strip() and str(predicted or "").strip() and expected == predicted)


def pipeline_classes_aligned(expected, predicted, *, aliases=None) -> bool:
    """Visualizer's literal, case-sensitive equality and explicit alias hook.

    Unlike classes_match, no trim/lowercase applies. Missing prediction is a
    miss. Missing alias entries never match by an absent None sentinel.
    """
    if not str(expected or "").strip() or not str(predicted or "").strip():
        return False
    if pipeline_labels_equal(expected, predicted):
        return True
    aliases = PIPELINE_ALIGNMENT if aliases is None else aliases
    return ((expected in aliases and aliases[expected] == predicted)
            or (predicted in aliases and aliases[predicted] == expected))


def pipeline_subclass_match(expected, predicted) -> bool:
    """Visualizer's nonempty-input, case-insensitive exact subclass predicate.

    Whitespace-only tokens are absent, not matching GT. No subtype equivalence,
    unknown collapse, or class gating applies.
    """
    expected, predicted = str(expected or "").strip().lower(), str(predicted or "").strip().lower()
    return bool(expected and predicted and expected == predicted)

def classify_pipeline_failure(row: dict) -> str:
    """Preserve lifecycle priority, including archived versus archive emptiness."""
    stage = (row.get("stage") or "").lower()
    if row.get("error") and stage in ("failed", ""):
        return "failed" if stage == "failed" else "error"
    if stage == "failed":
        return "failed"
    if stage in ("review", "human_review"):
        return "review"
    if row.get("expected") and not row.get("exact_ok") and not row.get("aligned_ok"):
        return "wrong_class"
    if row.get("expected") and not row.get("exact_ok") and row.get("aligned_ok"):
        return "ok"
    extract = row.get("extracted_data")
    if isinstance(extract, dict) and not any(
        v not in (None, "", [], {}) for k, v in extract.items() if not str(k).startswith("_")
    ):
        if stage not in ("archived",):
            return "empty_extract"
    if stage in ("archived", "archive", "report", "catalog"):
        return "ok"
    if not stage:
        return "unknown_stage"
    return "ok" if row.get("aligned_ok") else "wrong_class"


def score_pipeline_rows(rows: list[dict]) -> dict:
    """Score supplied flags over ALL rows; subclass GT alone selects its support.

    Explicit False subclass flags override recomputation; None falls back to the
    already lifted predicted_subclass. No-GT rows remain in class denominators
    and sparse confusion under unknown/none. Telemetry belongs to adapters.
    """
    n = len(rows)
    exact = sum(1 for r in rows if r.get("exact_ok"))
    aligned_n = sum(1 for r in rows if r.get("aligned_ok"))
    modes = Counter(classify_pipeline_failure(r) for r in rows)
    by_class: dict[str, dict] = defaultdict(lambda: {"n": 0, "exact": 0, "aligned": 0})
    for r in rows:
        cls = r.get("expected") or "unknown"
        by_class[cls]["n"] += 1
        by_class[cls]["exact"] += int(bool(r.get("exact_ok")))
        by_class[cls]["aligned"] += int(bool(r.get("aligned_ok")))
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        exp, pred = r.get("expected") or "unknown", r.get("predicted") or "none"
        confusion[exp][pred] += 1
    subclass_rows = [r for r in rows if str(r.get("expected_subclass") or "").strip()]
    n_sub = len(subclass_rows)
    subclass_hits = 0
    for r in subclass_rows:
        ok = r.get("subclass_ok")
        if ok is None:
            ok = pipeline_subclass_match(r.get("expected_subclass"), r.get("predicted_subclass"))
        subclass_hits += int(bool(ok))
    return {
        "n": n,
        "exact_accuracy": exact / n if n else 0.0,
        "aligned_accuracy": aligned_n / n if n else 0.0,
        "n_subclass": n_sub,
        "subclass_accuracy": (subclass_hits / n_sub) if n_sub else None,
        "failure_modes": dict(modes),
        "by_class": {k: dict(v) for k, v in by_class.items()},
        "confusion": {k: dict(v) for k, v in confusion.items()},
    }
