"""Scoring node family: ``compile_report -> score_common -> score_<type> -> catalog_write`` (spec §3.1, §5.1).

``score_common`` runs tier 0 (payload adherence) for every document and routes on
the final ``doc_type``, the one a person set when they corrected it. There is one
``score_<type>`` node per dojo ``DOC_SCORERS`` entry plus ``score_generic``; each
writes ``state["scoring"]`` from the dojo's ``score_archive_block``. All scoring
rules stay in the dojo: these nodes only gather the inputs.

A scorer that raises does not fail the document: the block records
``status: "error"`` and the failing node's visit shows which scorer it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "config" / "taxonomy.yaml"
GENERIC = "generic"


@lru_cache(maxsize=1)
def taxonomy_bytes() -> bytes:
    """The taxonomy the field map is read from, so ``field_map_sha`` names the map actually used."""
    return TAXONOMY_PATH.read_bytes()


def scorer_types() -> tuple[str, ...]:
    """Every document type with its own scoring node, then ``generic``."""
    from llm_dojo_scoring.scoring.doc_scorers import DOC_SCORERS

    return (*sorted(DOC_SCORERS), GENERIC)


def scorer_for(doc_type: str | None) -> str:
    """The registry entry that scores ``doc_type`` (unknown and missing types are ``generic``)."""
    from llm_dojo_scoring.scoring.doc_scorers import DOC_SCORERS, get_doc_scorer

    name = get_doc_scorer(doc_type or None).doc_type
    return name if name in DOC_SCORERS else GENERIC


def score_route(state: dict) -> str:
    """Conditional edge after ``score_common``: ``score_<type>`` or ``score_generic``."""
    return f"score_{scorer_for(state.get('doc_type'))}"


def model_payload(extracted: Any) -> Any:
    """The extraction as the scorer sees it: a dict without the pipeline's ``_``-keys (``_report``), else as is."""
    if isinstance(extracted, Mapping):
        return {k: v for k, v in extracted.items() if not str(k).startswith("_")}
    return extracted


def human_corrected(state_like: Mapping[str, Any]) -> bool:
    """True when a person changed the fields (spec §5.4), not when they approved the model's as is.

    The state's ``extraction_source`` is ``human`` for both (calibration keys on
    it); the ``human_label`` says which. Without a label the fields count as the
    person's.
    """
    if state_like.get("extraction_source") != "human":
        return False
    label = state_like.get("human_label")
    if not isinstance(label, Mapping):
        return True
    return bool(label.get("resolution") == "correct" or label.get("field_corrections")
                or label.get("removed_fields"))


def adherence_for(state_like: Mapping[str, Any]) -> dict[str, Any]:
    """Tier 0: ``assess_extraction_payload`` on the final payload against the mailroom pydantic schema."""
    from llm_dojo_scoring import assess_extraction_payload
    from schemas.documents import get_extraction_schema

    return assess_extraction_payload(
        model_payload(state_like.get("extracted_data")),
        state_like.get("doc_type") or "",
        state_like.get("doc_subclass"),
        schema_lookup=get_extraction_schema,
    )


def score_document_block(state_like: Mapping[str, Any], *, adherence: dict | None = None) -> dict[str, Any]:
    """Score one document from its final state (or a stored row, for the rescore CLI).

    Returns ``{"scoring": <score_archive_block output + scored_payload>, "extraction_adherence": ...}``.
    Reuses supplied adherence; scoring errors propagate to the caller.
    """
    from llm_dojo_scoring import score_archive_block
    from observability.extraction_gt import scoring_ground_truth

    doc_type = state_like.get("doc_type") or ""
    gt = state_like.get("ground_truth") if isinstance(state_like.get("ground_truth"), Mapping) else {}
    predicted = model_payload(state_like.get("extracted_data"))
    adherence = adherence if adherence is not None else adherence_for(state_like)
    block = score_archive_block(
        doc_type,
        predicted if isinstance(predicted, Mapping) else None,
        scoring_ground_truth(dict(gt)),
        taxonomy_bytes=taxonomy_bytes(),
        doc_text=state_like.get("doc_text"),
        expected_subclass=gt.get("expected_subclass"),
        doc_subclass=state_like.get("doc_subclass"),
    )
    block["scored_payload"] = "human_corrected" if human_corrected(state_like) else "model"
    return {"scoring": block, "extraction_adherence": adherence}


def score_common_node(state: dict) -> dict[str, Any]:
    """Run tier 0 and record the chosen scorer, or an error if adherence fails."""
    route = scorer_for(state.get("doc_type"))
    try:
        adherence = adherence_for(state)
        error = None
    except Exception as exc:  # scoring never fails the document
        logger.warning("scoring_common_failed", doc_id=state.get("doc_id"), exc_info=True)
        adherence, error = None, f"{type(exc).__name__}: {exc}"
    return {
        "scoring_common": {"doc_type": state.get("doc_type"), "scorer": route, "adherence": adherence, "error": error},
        "extraction_adherence": adherence,
    }


def _make_type_node(scorer: str):
    """Build a scorer node that records scoring failures without failing the document."""
    def node(state: dict) -> dict[str, Any]:
        try:
            return score_document_block(state, adherence=state.get("extraction_adherence"))
        except Exception as exc:  # scoring never fails the document
            logger.warning("scoring_failed", doc_id=state.get("doc_id"), scorer=scorer, exc_info=True)
            return {"scoring": {
                "status": "error", "reason": f"{type(exc).__name__}: {exc}",
                "scorer": {"doc_type": scorer, "module": None, "subclass": None, "tiers_run": []},
                "scored_payload": "human_corrected" if human_corrected(state) else "model",
            }}

    node.__name__ = f"score_{scorer}_node"
    node.__qualname__ = node.__name__
    return node


def span_name(node: str) -> str:
    """Langfuse span of a scoring node: ``score_merger_agreement`` -> ``score-merger-agreement``."""
    return node.replace("_", "-")


def add_scoring_nodes(graph, *, next_node: str = "catalog_write", wrap=None) -> None:
    """Add ``score_common`` and one ``score_<type>`` node per registry entry, routed to ``next_node``.

    ``wrap(span, fn)`` wraps each node (the graph passes ``traced_node`` + ``_bounded``).
    """
    wrap = wrap or (lambda _span, fn: fn)
    graph.add_node("score_common", wrap(span_name("score_common"), score_common_node))
    routes = {}
    for scorer in scorer_types():
        name = f"score_{scorer}"
        graph.add_node(name, wrap(span_name(name), _make_type_node(scorer)))
        graph.add_edge(name, next_node)
        routes[name] = name
    graph.add_conditional_edges("score_common", score_route, routes)


__all__ = [
    "GENERIC", "add_scoring_nodes", "adherence_for", "human_corrected", "model_payload", "score_common_node", "score_document_block",
    "score_route", "scorer_for", "scorer_types", "span_name", "taxonomy_bytes",
]
