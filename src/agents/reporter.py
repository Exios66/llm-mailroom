import structlog

from llm.prompt_doctrine import REPORTER as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt
from llm.retry import retry_chat_completion
from observability.tracing import langfuse_call_attrs

logger = structlog.get_logger(__name__)


COMPILE_SYSTEM_PROMPT_V0 = """You are a big-picture legal report synthesizer at a transactional law firm.
Your job is to take the extracted data from a document and produce a clean, structured summary
suitable for inclusion in a matter record. You do not extract new data — you compile and refine
what was already extracted by the specialist agents.

Rules:
1. Summarize the extracted data clearly — this goes into a client-facing matter record.
2. Preserve all key facts: parties, dates, obligations, risks, filing details.
3. If extraction data is sparse or low-confidence, note it in the summary.
4. Format the summary as clean structured text, not raw JSON.
5. Do not add facts not present in the extracted data.
6. Produce a confidence score reflecting the overall quality of the underlying extraction.
7. Treat null, empty lists, redaction markers, and placeholders such as "[•]" as absent
   information. Do not turn them into dates, names, statuses, or claims that the extraction
   did not establish; say "not stated" when the report needs to mention the gap.
8. Return only the matter-record summary. Do not claim that a fact was verified, is pending,
   or requires follow-up unless that statement appears in the extracted data."""

COMPILE_SYSTEM_PROMPT = COMPILE_SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


def compile_matter_record(
    manifest_data: dict,
    report_llm,
    report_model: str,
    temperature: float = 0.2,
) -> dict:
    doc_type = manifest_data.get("doc_type", "unknown")
    contract_subtype = manifest_data.get("contract_subtype")
    doc_subclass = manifest_data.get("doc_subclass")
    extracted = manifest_data.get("extracted_data", {})
    classification_confidence = manifest_data.get("classification_confidence")
    extraction_confidence = manifest_data.get("extraction_confidence")

    cleaned_extracted = {
        k: v for k, v in (extracted or {}).items()
        if k not in ("confidence", "reasoning")
    }

    user_message = f"""Document type: {doc_type}
Contract subtype: {contract_subtype}
Document subclass: {doc_subclass}
Classification confidence: {classification_confidence}
Extraction confidence: {extraction_confidence}

Extracted data:
{cleaned_extracted}

Please compile this into a clean matter-record summary."""

    prompt_text, prompt_obj = get_managed_prompt("reporter", COMPILE_SYSTEM_PROMPT)
    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": user_message},
    ]
    from pipeline.config import get_agent_config

    try:
        agent_config = get_agent_config("reporter")
        max_tokens = agent_config.get("max_tokens", 2048)
        reasoning_effort = agent_config.get("reasoning_effort")
    except Exception:
        max_tokens = 2048
        reasoning_effort = None
    kwargs = {
        "model": report_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        kwargs["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
    kwargs.update(langfuse_call_attrs("reporter"))
    if prompt_obj is not None:
        kwargs["langfuse_prompt"] = prompt_obj
    from pipeline.limits import get_run_deadline, record_usage

    kwargs["run_deadline"] = get_run_deadline()
    response = retry_chat_completion(report_llm, **kwargs)
    record_usage(getattr(response, "usage", None), report_model)
    summary = response.choices[0].message.content or ""
    logger.info("report_compiled", doc_type=doc_type, length=len(summary))

    return {
        "summary": summary,
        "doc_type": doc_type,
        "contract_subtype": contract_subtype,
        "doc_subclass": doc_subclass,
        "extracted_data": cleaned_extracted,
        "classification_confidence": classification_confidence,
        "extraction_confidence": extraction_confidence,
    }
