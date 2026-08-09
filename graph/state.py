from typing import TypedDict, Annotated, Any
from langgraph.graph.message import add_messages


class DocumentState(TypedDict, total=False):
    doc_id: str
    matter_id: str
    original_filename: str
    stage: str
    doc_type: str | None
    classification_confidence: float | None
    classification_attempts: int
    extracted_data: dict[str, Any] | None
    extraction_confidence: float | None
    extraction_attempts: int
    trace_id: str | None
    escalation_reason: str | None
    review_decision: str | None
    retry_count: int
    conflict_detected: bool
    extraction_guardrail: list[str]
    classification_guardrail: list[str]
    file_path: str
    doc_text: str
    # Page-image data-URIs rendered at ingest for vision-capable input agents
    # (PDFs rendered page-by-page; image files passed through). Sent to the
    # sorter/specialist prompts when the agent's model is vision-capable;
    # `doc_text` above is always produced regardless for text-only paths.
    doc_pages: list[str]
    error_message: str | None
    run_deadline: float
    run_aborted: bool
    # Transient provider-error retry (connection errors etc.): the node sets
    # `transient_error` and `transient_retries` so routing can retry the SAME
    # node (self-loop) instead of consuming the confidence-based retry budget.
    transient_error: bool
    transient_retries: int
    # Attempt number of this pipeline run for a document (observability: trace
    # tags/metadata + seed suffix beyond the first run).
    run_attempt: int
    messages: Annotated[list, add_messages]
