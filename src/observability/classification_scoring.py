"""Pipeline exact KPI compatibility facade; requires reviewed Dojo API release.

Aligned keys remain deprecated exact aliases. No regex recovery or class
alignment applies; error predictions count as misses.
"""
from llm_dojo_scoring.pipeline_scoring import (
    normalize_class, classes_match, score_exact_classification,
)
