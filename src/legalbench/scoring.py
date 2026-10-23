"""LegalBench compatibility facade. Requires the new reviewed Dojo release.

The Dojo engine retains producer correctness flags, selected successful-row
populations, exact category/family keys, and binned calibration semantics.
"""
from llm_dojo_scoring.legalbench_scoring import (
    _mean, _safe_div, _binary_f1, expected_calibration_error as _ece,
    score_binary, score_multiclass,
)
