"""Classification KPIs after ``merger_agreement`` became a live MAUD class.

Dojo 0.11.0 ``llm_dojo_scoring.mailroom.align_doc_type`` still maps
``merger_agreement`` → ``contract``. Mailroom must not use that helper:
predicting ``contract`` when GT is ``merger_agreement`` is a class miss
(Lane A), not an "aligned" hit.

Exact class match is the only class KPI. ``aligned_accuracy`` in HF report
JSON is a deprecated alias of exact so older The-Mailroom readers keep a
number; it is **not** a merger≡contract score.
"""

from __future__ import annotations

from typing import Any, Iterable


def normalize_class(token: Any) -> str:
    return str(token or "").strip().lower()


def classes_match(expected: Any, predicted: Any) -> bool:
    """True when predicted equals expected. MAUD is not CUAD."""
    exp = normalize_class(expected)
    pred = normalize_class(predicted)
    if not exp or not pred:
        return False
    return exp == pred


def score_exact_classification(
    expected: Iterable[Any],
    predicted: Iterable[Any],
) -> dict[str, Any]:
    """Run-level exact accuracy. ``aligned_*`` keys equal exact (deprecated)."""
    pairs = list(zip(expected, predicted))
    n = len(pairs)
    if not n:
        return {
            "n": 0,
            "exact_n": 0,
            "exact_accuracy": 0.0,
            "aligned_n": 0,
            "aligned_accuracy": 0.0,
            "aligned_equals_exact": True,
        }
    n_exact = sum(1 for e, p in pairs if classes_match(e, p))
    acc = round(n_exact / n, 3)
    return {
        "n": n,
        "exact_n": n_exact,
        "exact_accuracy": acc,
        "aligned_n": n_exact,
        "aligned_accuracy": acc,
        "aligned_equals_exact": True,
    }
