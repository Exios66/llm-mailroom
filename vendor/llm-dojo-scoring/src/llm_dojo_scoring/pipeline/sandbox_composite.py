"""Fixed sandbox chained-evaluation compatibility policy."""
from __future__ import annotations


def chained_composite(sorter_exact, extract_overall) -> float:
    """Weight supported sorter/extraction scores without inventing a missing half."""
    if sorter_exact is None or extract_overall is None:
        raise RuntimeError(
            "chained eval: composite score cannot be derived — sorter "
            f"exact_match={sorter_exact!r}, extractor overall={extract_overall!r}; "
            "a 0-sentinel composite would silently hide the failed half"
        )
    return 0.25 * float(sorter_exact) + 0.75 * float(extract_overall)
