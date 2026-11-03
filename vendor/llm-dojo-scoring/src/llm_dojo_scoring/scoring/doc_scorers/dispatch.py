"""Compose the scoring tiers for one document; ``score_document`` is the public entry.

``score_tiers`` runs tier 0 (``score_common``), then the type module's tier 1/2
extras and headline. The headline swap keeps the field-map score as
``suite_overall_extraction_score`` and marks ``scoring_method`` with ``+<benchmark>``
(``+maud``, ``+cuad``).
"""

from __future__ import annotations

from typing import Any, Mapping

from ...pipeline.sandbox_schema_adherence import SchemaLookup, assess_extraction_payload, coerce_predicted_payload
from ..extraction_scope import scope_extraction_pair
from . import canonical_doc_type, get_doc_scorer
from .common import score_common


def score_tiers(
    doc_type: str,
    predicted: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    *,
    doc_text: str | None = None,
    doc_subclass: str | None = None,
    expected_subclass: str | None = None,
) -> dict[str, Any]:
    """Tier 0 plus the type's extras and headline for one document (no adherence)."""
    doc_type = canonical_doc_type(doc_type)
    scorer = get_doc_scorer(doc_type)
    payload = score_common(
        doc_type,
        predicted,
        expected,
        doc_text=doc_text,
        doc_subclass=doc_subclass,
        expected_subclass=expected_subclass,
        detailed=scorer.detailed,
    )
    raw_expected = expected or {}
    scoped_predicted, _ = scope_extraction_pair(doc_type, predicted or {}, raw_expected)
    sub = scorer.subclass(doc_subclass or expected_subclass)
    extras = scorer.extras(scoped_predicted, raw_expected, subclass=sub, doc_text=doc_text)
    payload.update(extras)
    score, metric = scorer.headline(payload, extras)
    if metric is not None:
        payload["suite_overall_extraction_score"] = payload["overall_extraction_score"]
        payload["overall_extraction_score"] = score
        payload["scoring_method"] = f"{payload['scoring_method']}+{metric.split('_', 1)[0]}"
    return payload


def _scorer_info(scorer: Any, doc_type: str, raw_subclass: Any) -> dict[str, Any]:
    """The ``scorer`` record: which module ran, on which subclass, through which tiers."""
    tiers = ["common"]
    if scorer.doc_type != "generic":
        tiers.append("type")
        if scorer.subclasses and raw_subclass:
            tiers.append("subclass")
    return {
        "doc_type": doc_type,
        # Each type module is named after its doc type (one file per type, spec §3.1).
        "module": f"{__package__}.{scorer.doc_type}",
        "subclass": scorer.subclass(raw_subclass) if raw_subclass else None,
        "tiers_run": tiers,
    }


def score_document(
    doc_type: str,
    predicted: Any,
    expected: Mapping[str, Any] | None,
    *,
    doc_text: str | None = None,
    doc_subclass: str | None = None,
    expected_subclass: str | None = None,
    schema_lookup: SchemaLookup | None = None,
) -> dict[str, Any]:
    """Score one document's extraction: the sandbox ``score_extraction_row`` payload plus ``scorer``.

    Tier 0 (scoping, class suite, PRF keys, ``unlabeled_fields``), the type module's
    extras and headline (MAUD/CUAD, correspondence topic/sentiment, claims consistency),
    and ``assess_extraction_payload`` adherence keys on the prediction as given. A
    prediction that is not a mapping (``None``, JSON text, prose, a list) is coerced when
    it holds a JSON object and field-scored as empty otherwise; it never raises.

    Departures from the sandbox's ``score_extraction_row`` (0.27.0): a ``None``
    prediction is a parse error (the sandbox called it adherent); adherence reports
    keys outside the class schema (the sandbox checked the scoped prediction); and the
    correspondence / insurance extras are reported (the sandbox dropped them). Pydantic
    models for adherence come from ``schema_lookup`` (the dojo does not import mailroom).
    """
    doc_type = canonical_doc_type(doc_type)
    raw_subclass = str(doc_subclass or expected_subclass or "").strip() or None
    if isinstance(predicted, Mapping):
        scored_predicted: Mapping[str, Any] = predicted
    else:
        coerced, _meta = coerce_predicted_payload(predicted)
        scored_predicted = coerced or {}
    payload = score_tiers(
        doc_type,
        scored_predicted,
        expected if isinstance(expected, Mapping) else None,
        doc_text=doc_text,
        doc_subclass=doc_subclass,
        expected_subclass=expected_subclass,
    )
    # Adherence sees the prediction as the specialist returned it: foreign keys that
    # scoping drops still count as extra keys, and a null prediction is a parse error.
    payload.update(assess_extraction_payload(predicted, doc_type, raw_subclass, schema_lookup=schema_lookup))
    scorer = get_doc_scorer(doc_type)
    payload["scorer"] = _scorer_info(scorer, doc_type, raw_subclass)
    return payload
