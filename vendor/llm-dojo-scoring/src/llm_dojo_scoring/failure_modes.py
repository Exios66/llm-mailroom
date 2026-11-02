"""Failure-mode taxonomy and classification for sorter / subtype evals.

The failure-mode logic previously lived inside ``scripts/eval/run_subtype_eval.py``
(llm-entity-extraction); it is now a library function any runner or post-hoc
analysis can import, so every report aggregates the SAME mode definitions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Optional

from .config import SORTER_FAILURE_MODES, DOCCLASS_FAILURE_MODES
from .equivalences import normalize_subtype
from .field_scoring import get_bipartite_match_threshold

SORTER_MODE_ORDER = ["function_over_form", "other_fallback",
                     "equivalent_family", "family_confusion"]


def classify_extraction_failure(
    *,
    parse_ok: float | None = None,
    schema_valid: float | None = None,
    overall_score: float | None = None,
) -> str:
    """Format/parse vs extraction capability (#20)."""
    from .scorecard_honesty import classify_extraction_failure as _classify

    return _classify(
        parse_ok=parse_ok,
        schema_valid=schema_valid,
        overall_score=overall_score,
    )


def classify_failure(sorter: dict, subtype_unknown: str = "other") -> str:
    """Classify a failed sorter row into an insight-relevant failure mode.

    - ``function_over_form``: the sorter judged the document a non-contract
      (doc_type miss) — usually a document whose function (e.g. an SEC joint
      filing agreement) overrode its contract form.
    - ``other_fallback``: the sorter answered "other" for a contract that the
      corpus files under a family.
    - ``equivalent_family``: the predicted family is a defensible equivalent
      of the expected one (recovered by ``subtype_ok_equiv``).
    - ``family_confusion``: a genuine wrong-family pick.

    Expected input keys (the sorter composite row): ``doc_type_ok``,
    ``contract_subtype``, ``subtype_ok_equiv``. Returns "ok" for rows that
    did not fail.
    """
    if sorter.get("subtype_ok"):
        return "ok"
    if not sorter.get("doc_type_ok"):
        return "function_over_form"
    if sorter.get("contract_subtype") == subtype_unknown:
        return "other_fallback"
    if sorter.get("subtype_ok_equiv"):
        return "equivalent_family"
    return "family_confusion"


def classify_docclass_failure(row: dict) -> str:
    """Failure mode for the hierarchical docclass task: ``doc_type_miss`` when
    the doc_type is wrong, else ``subclass_miss``."""
    if not row.get("doc_type_ok", True):
        return "doc_type_miss"
    return "subclass_miss"


def summarize_failures(rows: list[dict], subtype_unknown: str = "other") -> dict:
    """Aggregate failure modes across sorter result rows.

    Each row: ``{"doc_type_ok", "contract_subtype", "subtype_ok",
    "subtype_ok_equiv"}`` (and optionally ``reasoning``). Returns
    ``{"n_total", "n_failed", "n_ok", "mode_counts", "rate",
    "mode_rate", "failures"}``.
    """
    mode_counts: Counter = Counter()
    failures: list[dict] = []
    n_ok = 0
    for i, row in enumerate(rows):
        mode = classify_failure(row, subtype_unknown)
        if mode == "ok":
            n_ok += 1
            continue
        mode_counts[mode] += 1
        failure = {
            "index": i,
            "mode": mode,
            "expected_subtype": row.get("expected_subtype"),
            "predicted_subtype": row.get("contract_subtype"),
        }
        for key in ("reasoning", "filename", "confidence"):
            if row.get(key) is not None:
                failure[key] = row[key]
        failures.append(failure)
    n_total = len(rows)
    n_failed = n_total - n_ok
    return {
        "n_total": n_total,
        "n_failed": n_failed,
        "n_ok": n_ok,
        "mode_counts": dict(mode_counts),
        "rate": round(n_failed / n_total, 4) if n_total else 0.0,
        "mode_rate": {
            mode: round(mode_counts[mode] / n_total, 4) if n_total else 0.0
            for mode in SORTER_MODE_ORDER
        },
        "failures": failures,
    }


def per_subtype_accuracy(rows: list[dict], keys: list[str] | None = None,
                         equivalences: bool = True) -> dict:
    """Per-subtype strict (and optionally equivalence-aware) accuracy.

    Each row: ``{"expected_subtype", "contract_subtype", "subtype_ok",
    "subtype_ok_equiv"}``. Returns ``{key: {"n", "correct", "accuracy",
    "correct_equiv", "accuracy_equiv"}}`` for every subtype with support.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "correct": 0, "correct_equiv": 0}
    )
    for row in rows:
        key = normalize_subtype(row.get("expected_subtype"))
        bucket = buckets[key]
        bucket["n"] += 1
        bucket["correct"] += int(bool(row.get("subtype_ok")))
        if equivalences:
            # A correct subtype is trivially equivalence-correct even when the
            # row omits subtype_ok_equiv (real rows always set it True for
            # correct classifications, but be robust to missing flags).
            bucket["correct_equiv"] += int(
                bool(row.get("subtype_ok")) or bool(row.get("subtype_ok_equiv"))
            )
    result: dict[str, dict] = {}
    for key, b in buckets.items():
        out = {
            "n": b["n"],
            "correct": b["correct"],
            "accuracy": round(b["correct"] / b["n"], 4) if b["n"] else 0.0,
        }
        if equivalences:
            out["correct_equiv"] = b["correct_equiv"]
            out["accuracy_equiv"] = round(b["correct_equiv"] / b["n"], 4) if b["n"] else 0.0
        result[key] = out
    if keys is not None:
        ordered = {k: result[k] for k in keys if k in result}
        ordered.update({k: v for k, v in result.items() if k not in keys})
        return ordered
    return result


def confusion_from_rows(rows: list[dict], keys: list[str] | None = None,
                        unknown: str = "other") -> tuple[list[list[int]], list[str]]:
    """Expected x predicted confusion matrix from sorter rows.

    Each row: ``{"expected_subtype", "contract_subtype"}`` (already-normalized
    keys preferred; values are passed through ``normalize_subtype``).
    """
    pairs = [
        (normalize_subtype(r.get("expected_subtype")), normalize_subtype(r.get("contract_subtype")))
        for r in rows
    ]
    if keys is None:
        keys = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    else:
        keys = list(keys) + [k for k in sorted({e for e, _ in pairs} | {p for _, p in pairs})
                             if k not in keys]
    index = {k: i for i, k in enumerate(keys)}
    matrix = [[0] * len(keys) for _ in keys]
    for e, p in pairs:
        matrix[index[e]][index[p]] += 1
    return matrix, keys


__all__ = [
    "SORTER_MODE_ORDER", "SORTER_FAILURE_MODES", "DOCCLASS_FAILURE_MODES",
    "classify_failure", "classify_extraction_failure", "classify_docclass_failure",
    "summarize_failures",
    "per_subtype_accuracy", "confusion_from_rows",
]
