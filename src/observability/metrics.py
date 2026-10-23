"""Adapt pipeline diagnostic rows to the Dojo-owned metric engine."""

from __future__ import annotations

from llm_dojo_scoring import EntityListScore
from llm_dojo_scoring import diagnostics as _dojo_diagnostics
from llm_dojo_scoring.diagnostics import (
    DATE_FIELDS, DURATION_FIELDS, is_entity_list, parse_date,
    parse_duration_days, parse_money,
)


def extraction_diagnostics(
    rows: list[dict],
    field_types: dict[str, str],
    master: dict | None = None,
) -> dict:
    """Preserve pipeline row and report contracts while Dojo computes metrics.

    ``master`` remains accepted and unused. Pipeline rows can carry the
    scorer's EntityListScore objects; Dojo accepts their mapping form.
    The historical report advertises list headlines only for CUAD clauses.
    """
    adapted = []
    for row in rows:
        scores = {}
        for field, score in (row.get("entity_list_scores") or {}).items():
            if score is None or isinstance(score, dict):
                scores[field] = score
            elif isinstance(score, EntityListScore):
                scores[field] = score.to_dict()
            else:
                scores[field] = {
                    key: getattr(score, key, 0)
                    for key in ("precision", "recall", "f1", "matched",
                                "n_predicted", "n_expected")
                }
        adapted.append({**row, "entity_list_scores": scores})
    result = _dojo_diagnostics.extraction_diagnostics(adapted, field_types)
    if result.pop("list_headline_field", None) != "cuad_clauses":
        for key in ("list_precision", "list_recall", "list_f1"):
            result.pop(key, None)
    return result
