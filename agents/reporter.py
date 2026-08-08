import structlog

from llm.retry import retry_chat_completion
from observability.tracing import langfuse_call_attrs

logger = structlog.get_logger(__name__)


_COMPILE_SYSTEM_PROMPT = """You are a big-picture legal report synthesizer at a transactional law firm.
Your job is to take the extracted data from a document and produce a clean, structured summary
suitable for inclusion in a matter record. You do not extract new data — you compile and refine
what was already extracted by the specialist agents.

Rules:
1. Summarize the extracted data clearly — this goes into a client-facing matter record.
2. Preserve all key facts: parties, dates, obligations, risks, filing details.
3. If extraction data is sparse or low-confidence, note it in the summary.
4. Format the summary as clean structured text, not raw JSON.
5. Do not add facts not present in the extracted data.
6. Produce a confidence score reflecting the overall quality of the underlying extraction."""


def compile_matter_record(
    manifest_data: dict,
    report_llm,
    report_model: str,
    temperature: float = 0.2,
) -> dict:
    doc_type = manifest_data.get("doc_type", "unknown")
    extracted = manifest_data.get("extracted_data", {})
    classification_confidence = manifest_data.get("classification_confidence")
    extraction_confidence = manifest_data.get("extraction_confidence")

    cleaned_extracted = {k: v for k, v in (extracted or {}).items() if k != "confidence"}

    user_message = f"""Document type: {doc_type}
Classification confidence: {classification_confidence}
Extraction confidence: {extraction_confidence}

Extracted data:
{cleaned_extracted}

Please compile this into a clean matter-record summary."""

    messages = [
        {"role": "system", "content": _COMPILE_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    from pipeline.config import get_agent_config

    try:
        max_tokens = get_agent_config("reporter").get("max_tokens", 2048)
    except Exception:
        max_tokens = 2048
    response = retry_chat_completion(
        report_llm,
        model=report_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **langfuse_call_attrs("reporter"),
    )
    summary = response.choices[0].message.content or ""
    logger.info("report_compiled", doc_type=doc_type, length=len(summary))

    return {
        "summary": summary,
        "doc_type": doc_type,
        "extracted_data": cleaned_extracted,
        "classification_confidence": classification_confidence,
        "extraction_confidence": extraction_confidence,
    }
