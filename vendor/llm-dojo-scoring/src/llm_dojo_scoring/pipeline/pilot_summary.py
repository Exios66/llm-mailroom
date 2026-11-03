"""Pilot classification support; display/usage aggregation stays producer-local."""
from __future__ import annotations
from .pipeline_scoring import classes_match, score_exact_classification


def pilot_classification_summary(rows):
    """Three-decimal exact/subclass support over the pilot's actual row flags.

    Nonempty predictions override exact_ok. Missing predictions use that flag
    for per-class counts but the class scorer still rejects empty GT. Subclass
    support is selected by non-None subclass_ok, even without expected_subclass.
    """
    class_scores = score_exact_classification(
        [r.get("expected") for r in rows],
        [r.get("predicted") if r.get("predicted") not in (None, "") else
         (r.get("expected") if r.get("exact_ok") else "") for r in rows],
    )
    classes, strata = {}, {}
    subclass_n = subclass_correct = 0
    for row in rows:
        kind = row.get("expected") or "unknown"
        sub = str(row.get("expected_subclass") or "").strip() or "_"
        exact = (classes_match(row.get("expected"), row.get("predicted"))
                 if row.get("predicted") not in (None, "") else bool(row.get("exact_ok")))
        supported = row.get("subclass_ok") is not None
        correct = bool(row.get("subclass_ok"))
        subclass_n += int(supported)
        subclass_correct += int(supported and correct)
        for buckets, key in ((classes, kind), (strata, (kind, sub))):
            v = buckets.setdefault(key, {"n": 0, "exact": 0, "subclass": 0, "subclass_n": 0})
            v["n"] += 1
            v["exact"] += int(exact)
            v["subclass_n"] += int(supported)
            v["subclass"] += int(supported and correct)
    for buckets in (classes, strata):
        for v in buckets.values():
            v["exact_accuracy"] = round(v["exact"] / v["n"], 3) if v["n"] else 0.0
            v["subclass_accuracy"] = round(v["subclass"] / v["subclass_n"], 3) if v["subclass_n"] else None
    return {**class_scores, "subclass_n": subclass_n, "subclass_correct": subclass_correct,
            "subclass_accuracy": round(subclass_correct / subclass_n, 3) if subclass_n else None,
            "per_class": classes, "per_subclass": strata}


def pilot_calibration_error(confidence_pairs):
    """Return round-last mean absolute confidence error, or None without support.

    Producers select their supported raw confidence/correctness pairs. Unlike
    averaging rounded per-row scores, raw arithmetic preserves pilot reports.
    """
    pairs = list(confidence_pairs)
    return round(sum(abs(c - m) for c, m in pairs) / len(pairs), 3) if pairs else None
