"""Exact GT support summaries for pipeline pilot and factuality adapters.

These policies deliberately differ: pilot required fields exclude only None and
empty strings; schema coverage also excludes empty containers. Audit means give
one vote per eligible field, never one vote per predicted entity.
"""
from __future__ import annotations


def pilot_ground_truth_scores(class_correct, stage_correct, extracted, expected, confidence):
    """Preserve pilot truthiness, required-field support and 3-decimal error.

    Empty expected lists/dicts, numeric zero and False are requirements. Predicted
    zero/False/empty containers are missing; a nonempty list (even [None]) counts.
    Calibration is per-row absolute error, not binned run-level ECE.
    """
    scores = {"class_correct": int(class_correct), "stage_correct": int(stage_correct)}
    required = {k: v for k, v in (expected or {}).items() if v not in (None, "")}
    if required:
        present = 0
        for key in required:
            value = extracted.get(key)
            present += int(len(value) > 0 if isinstance(value, list) else bool(value))
        scores["expected_field_presence"] = round(present / len(required), 3)
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        scores["confidence_calibration_error"] = round(abs(float(confidence) - scores["class_correct"]), 3)
    return scores


def schema_gt_support(names, expected):
    """Labeled schema support in supplied schema order; 0/0 coverage is None."""
    names = list(names)
    present = [name for name in names if (expected or {}).get(name) not in (None, "", [], {})]
    return {"n_schema": len(names), "n_labeled": len(present), "labeled_fields": present,
            "coverage": round(len(present) / len(names), 3) if names else None}


def field_audit_support(audits):
    """Return exact eligible-field support and four-decimal factuality means.

    Existing n_predicted truthiness is authoritative, including a real rate of
    zero. Missing keys on an eligible malformed audit still raise KeyError.
    """
    eligible = [a for a in audits.values() if a.get("n_predicted")]
    n = len(eligible)
    return {"n_audited": n,
            "verified_precision": round(sum(a["verified_precision"] for a in eligible) / n, 4) if n else None,
            "hallucination_rate": round(sum(a["hallucination_rate"] for a in eligible) / n, 4) if n else None}
