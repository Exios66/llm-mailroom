"""Deterministic scoring for LegalBench runs — every number computed locally.

No LLM grading anywhere: scores are derived from the model's structured
answer vs. the corpus ground truth. All functions take the runner's result
rows (each with ``expected``/``predicted``/``correct``/``confidence``) and
return plain dicts that serialize into the experiment-log record and the
site's breakdown card.
"""

from __future__ import annotations

from typing import Any


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _binary_f1(correct_yes: int, pred_yes: int, label_yes: int) -> float:
    precision = _safe_div(correct_yes, pred_yes)
    recall = _safe_div(correct_yes, label_yes)
    return _safe_div(2 * precision * recall, precision + recall)


def _ece(pairs: list[tuple[float, bool]], bins: int = 10) -> float:
    """Expected calibration error over confidence/outcome pairs."""
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

    n = len(ok)
    accuracy = _safe_div(sum(1 for r in ok if r["correct"]), n)
    macro_cat = _mean([_safe_div(sum(v), len(v)) for v in per_category.values()])
    return {
        "accuracy": round(accuracy, 4),
        "macro_category_accuracy": macro_cat,
        "yes_f1": round(_binary_f1(correct_yes, pred_yes, label_yes), 4),
        "n_questions": n,
        "n_yes": n_yes,
        "n_no": n_no,
        "n_error": len(results) - n,
        "confidence_mean": _mean([c for c, _ in conf_pairs]),
        "calibration_error": _ece(conf_pairs),
        "per_category": {
            cat: round(_safe_div(sum(v), len(v)), 4) for cat, v in sorted(per_category.items())
        },
    }


def score_multiclass(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict/equiv family accuracy, macro-F1, per-family breakdown."""
    from langchain_agents.sorter_agent import equivalent_subtypes

    ok = [r for r in results if r.get("status") == "ok" and r.get("predicted") not in (None, "error")]
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
            if r["correct"] or equivalent_subtypes(r.get("expected", ""), r.get("predicted", ""))
        ),
        n,
    )
    # Macro F1: per-family one-vs-rest, mean over families with labels present.
    f1s: list[float] = []
    for fam, flags in per_family.items():
        tp = sum(1 for f in flags if f)
        # predicted-count for this family (correct==True with this label)
        pred_fam = sum(1 for r in ok if r.get("predicted") == fam)
        label_fam = len(flags)
        f1s.append(_safe_div(2 * _safe_div(tp, pred_fam) * _safe_div(tp, label_fam),
                             _safe_div(tp, pred_fam) + _safe_div(tp, label_fam)))
    return {
        "accuracy": round(strict, 4),
        "accuracy_equiv": round(equiv, 4),
        "macro_f1": _mean(f1s),
        "n_documents": n,
        "n_error": len(results) - n,
        "n_other_predicted": n_other_pred,
        "n_other_labeled": n_other_label,
        "confidence_mean": _mean([c for c, _ in conf_pairs]),
        "calibration_error": _ece(conf_pairs),
        "per_family": {
            fam: round(_safe_div(sum(v), len(v)), 4) for fam, v in sorted(per_family.items())
        },
    }
