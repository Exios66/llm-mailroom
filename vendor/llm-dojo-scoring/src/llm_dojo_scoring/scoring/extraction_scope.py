"""Scope extraction GT to the live class schema before scoring (SAND-026).

Moved from local-mailroom-sandbox ``eval/extraction_scope.py`` in dojo 0.27.0 so the
sandbox, mailroom and eval-environment share one copy.

Hub ``ground_truth`` / ``gt_fields`` often carries a **union** of specialist keys.
Empty insurance-claim placeholders on a correspondence row (and the symmetric case)
must not count as extraction misses.

``score_extraction`` already skips ``None`` / ``""`` but **does not** skip empty
lists. An expected ``denial_reasons: []`` with a missing prediction scores 0.0 and
pulls ``overall_score`` down; ``extraction_binary_metrics`` already skips ``[]``.
This module drops those empties *and* keys that do not belong to the scored document
class before the suite runs, so ``overall_extraction_score`` and F1 agree.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..schemas import doc_classes

# Live extraction keys that the class suite scores: for each of the five live
# classes, exactly the fields of ``schemas/doc_classes.py`` (edit the registry,
# not this table). Keep in lockstep with vendored ``schemas.documents`` /
# ``langchain_agents.specialist_agents``. The contract's v1 parse-compat
# ``merger_consideration`` / ``maud_clauses`` are not in its registry fields, so
# scoping drops them. Trace-only keys (``reasoning``, ``confidence``) are never
# scoring events.
LIVE_SCHEMA_FIELDS: dict[str, frozenset[str]] = {
    key: frozenset(doc_classes.field_names(key)) for key in doc_classes.LIVE_DOC_TYPES
}

# Corpus differentiators scored as content extras, not extraction fields.
# Left on the pair so ``peel_non_extraction_fields`` still sees them.
NON_EXTRACTION_KEEP: frozenset[str] = frozenset(
    {
        "content_topic",
        "topic_evidence",
        "sentiment_label",
        "sentiment_score",
        "sentiment_evidence",
        "label_evidence",
        "maud_clause_labels",
    }
)

# Never scoring events — even when Hub/GT copies them onto the row.
TRACE_ONLY_KEYS: frozenset[str] = frozenset({"reasoning", "confidence"})

# Hub annotation metadata, not extraction schema.
HUB_METADATA_KEYS: frozenset[str] = frozenset(
    {"intent_source", "intent_confidence", "intent_status"}
)

# Hub union keys that name the same fact under another class's schema.
# Applied before allow-list filtering so a correspondence row that stores
# the demanded dollars as ``claimed_amount`` still scores ``demand_amount``.
HUB_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "correspondence": {"claimed_amount": "demand_amount"},
}

_EMPTY: tuple[Any, ...] = (None, "", [], {})


def is_empty_gt(value: Any) -> bool:
    """True when a Hub/GT value is absence, not a stated fact.

    Numeric zero is a stated value. Empty lists / dicts / blank strings are not.
    """
    if value in _EMPTY:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple)):
        return all(is_empty_gt(item) for item in value)
    return False


def _apply_hub_aliases(doc_type: str, fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy Hub union-key values onto the live schema name when dest is empty."""
    out = dict(fields or {})
    aliases = HUB_FIELD_ALIASES.get(str(doc_type or "").strip()) or {}
    for src, dest in aliases.items():
        if src not in out:
            continue
        src_val = out[src]
        dest_val = out.get(dest)
        if not is_empty_gt(src_val) and is_empty_gt(dest_val):
            out[dest] = src_val
    return out


def _allowed_fields(doc_type: str) -> frozenset[str] | None:
    """Return live schema keys, including all registered corporate extensions.

    Return None for an unknown document type. Corporate registry import
    errors propagate.
    """
    resolved = str(doc_type or "").strip()
    allowed = LIVE_SCHEMA_FIELDS.get(resolved)
    if resolved == "corporate_record":
        from ..schemas.corporate_record_schema import SUBCLASS_SCHEMAS

        extensions = {e.key for schema in SUBCLASS_SCHEMAS.values() for e in schema.entities}
        return frozenset(allowed or ()) | extensions
    if resolved == "insurance_claim" and allowed is not None:
        # The CMS-row marker is not a registry field, but a prediction that states a
        # table must reach the scorer, which scores it against the subclass (#132).
        return allowed | {"cms_source_table"}
    return allowed


def applicable_expected_fields(
    doc_type: str,
    expected: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """GT keys that may count as extraction events for ``doc_type``.

    Applies Hub aliases, drops trace/Hub metadata, and keeps schema keys plus
    shared content-scoring fields. Corporate records retain ``gt_presence`` and blank
    schema values so Dojo can distinguish annotated absence from unlabeled
    fields. Other classes drop empty placeholders. Unknown document types
    keep remaining nonempty keys without an allow-list.
    """
    allowed = _allowed_fields(doc_type)
    corporate = str(doc_type or "").strip() == "corporate_record"
    out: dict[str, Any] = {}
    for key, value in _apply_hub_aliases(doc_type, expected).items():
        if key in TRACE_ONLY_KEYS or key in HUB_METADATA_KEYS:
            continue
        # Dojo needs original annotation metadata and blank labeled values
        # to distinguish explicit absence from fields never annotated.
        if corporate and (key == "gt_presence" or key in (allowed or ())):
            out[key] = value
            continue
        if is_empty_gt(value):
            continue
        if key in NON_EXTRACTION_KEEP:
            out[key] = value
            continue
        if allowed is not None and key not in allowed:
            continue
        out[key] = value
    return out


def scope_predicted_fields(
    doc_type: str,
    predicted: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Drop class-mismatched predicted keys so they are not false positives.

    A correspondence specialist must not be F1-penalized for omitting
    ``claim_number``. In-schema extras the model invented still count as FP.
    """
    allowed = _allowed_fields(doc_type)
    out: dict[str, Any] = {}
    for key, value in _apply_hub_aliases(doc_type, predicted).items():
        if key in TRACE_ONLY_KEYS or key in HUB_METADATA_KEYS:
            continue
        if allowed is None:
            out[key] = value
            continue
        if key in NON_EXTRACTION_KEEP or key in allowed:
            out[key] = value
    return out


def scope_extraction_pair(
    doc_type: str,
    predicted: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(predicted, expected)`` scoped to this class before scoring."""
    return (
        scope_predicted_fields(doc_type, predicted),
        applicable_expected_fields(doc_type, expected),
    )
