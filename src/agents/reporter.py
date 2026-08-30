"""Procedural matter-record assembler (reporter LLM retired).

Compiles classification + extraction (+ optional arbiter caveats) into a
structured summary suitable for ``extracted_data._report``. No LLM calls.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Procedural matter-record assembler — no LLM. Kept as a syncable template so
# Langfuse prompt registry / cutover listings stay complete.
COMPILE_SYSTEM_PROMPT_V0 = """You are a procedural matter-record assembler.
You do not call an LLM. The pipeline formats classification and extraction
fields into a structured summary for the archive sidecar and catalog.

Include arbiter caveats when accept_with_caveats was decided.
Do not invent facts beyond the extracted fields."""

COMPILE_SYSTEM_PROMPT = COMPILE_SYSTEM_PROMPT_V0


def _fmt_value(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "not stated"
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if v not in (None, "")]
        return "; ".join(items) if items else "not stated"
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def compile_matter_record(
    manifest_data: dict,
    report_llm=None,
    report_model: str | None = None,
    temperature: float = 0.0,
) -> dict:
    """Assemble a deterministic matter-record summary from extracted fields.

    ``report_llm`` / ``report_model`` / ``temperature`` are accepted for call-
    site compatibility but ignored — the reporter agent is procedural.
    """
    del report_llm, report_model, temperature  # unused (procedural)
    doc_type = manifest_data.get("doc_type", "unknown")
    contract_subtype = manifest_data.get("contract_subtype")
    doc_subclass = manifest_data.get("doc_subclass")
    extracted = manifest_data.get("extracted_data", {}) or {}
    classification_confidence = manifest_data.get("classification_confidence")
    extraction_confidence = manifest_data.get("extraction_confidence")

    cleaned_extracted = {
        k: v for k, v in extracted.items()
        if k not in ("confidence", "reasoning", "_report")
    }

    lines = [
        f"Document type: {doc_type}",
        f"Subclass: {doc_subclass or contract_subtype or 'not stated'}",
        f"Classification confidence: {classification_confidence}",
        f"Extraction confidence: {extraction_confidence}",
        "",
        "Extracted fields:",
    ]
    for key in sorted(cleaned_extracted.keys()):
        lines.append(f"- {key}: {_fmt_value(cleaned_extracted[key])}")

    arbiter_decision = manifest_data.get("arbiter_decision")
    arbiter_reasoning = manifest_data.get("arbiter_reasoning")
    arbiter_handoff = manifest_data.get("arbiter_handoff")
    if arbiter_decision == "accept_with_caveats" or arbiter_reasoning or arbiter_handoff:
        lines.append("")
        lines.append("Caveats:")
        if arbiter_decision:
            lines.append(f"- arbiter_decision: {arbiter_decision}")
        if arbiter_reasoning:
            lines.append(f"- reasoning: {arbiter_reasoning}")
        if arbiter_handoff:
            lines.append(f"- handoff: {arbiter_handoff}")

    summary = "\n".join(lines).strip() + "\n"
    logger.info("report_compiled", doc_type=doc_type, length=len(summary), procedural=True)

    return {
        "summary": summary,
        "doc_type": doc_type,
        "contract_subtype": contract_subtype,
        "doc_subclass": doc_subclass,
        "extracted_data": cleaned_extracted,
        "classification_confidence": classification_confidence,
        "extraction_confidence": extraction_confidence,
        "procedural": True,
    }
