"""Normalize specialist extractions before guardrails and routing."""

from __future__ import annotations

from typing import Any


def normalize_specialist_extraction(doc_type: str, extracted: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a specialist payload to a complete, schema-shaped dict."""
    payload = dict(extracted or {})
    schema = None
    try:
        from langchain_agents.specialist_agents import get_extraction_schema

        schema = get_extraction_schema(doc_type)
    except Exception:
        schema = None
    if schema:
        from langchain_agents.specialist_agents import normalize_extraction

        payload = normalize_extraction(payload, schema)
    if doc_type == "correspondence":
        for key in ("sender", "recipient", "communication_type", "urgency"):
            if payload.get(key) is None:
                payload[key] = ""
    if doc_type == "insurance_claim":
        text = payload.pop("_source_text", None)
        if isinstance(text, str) and text.strip():
            from observability.posthoc_gt import extract_insurance_fields

            hints = extract_insurance_fields(text)
            for key, value in hints.items():
                if payload.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                    payload[key] = value
    return payload


def enrich_insurance_from_text(extracted: dict[str, Any], doc_text: str) -> dict[str, Any]:
    """Fill null insurance fields from conservative CMS/FNOL regexes."""
    payload = dict(extracted or {})
    payload["_source_text"] = doc_text
    return normalize_specialist_extraction("insurance_claim", payload)
