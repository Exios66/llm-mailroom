"""LegalBench compatibility facade. Requires the new reviewed Dojo release.

The Dojo engine retains producer correctness flags, selected successful-row
populations, exact category/family keys, and binned calibration semantics.
"""
from llm_dojo_scoring.legalbench_scoring import (
    _mean, _safe_div, _binary_f1, expected_calibration_error as _ece,
    score_binary, score_multiclass as _score_multiclass,
)


def score_multiclass(results):
    """Bind the established producer groups while Dojo computes the report."""
    from langchain_agents.sorter_agent import SUBTYPE_EQUIVALENCES

    return _score_multiclass(results, subtype_equivalences=SUBTYPE_EQUIVALENCES)
