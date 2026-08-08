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
    extracted_data: dict[Any, Any] | None
    extraction_confidence: float | None
    extraction_attempts: int
    trace_id: str | None
    escalation_reason: str | None
    review_decision: str | None
    retry_count: int
    conflict_detected: bool
    file_path: str
    doc_text: str
    error_message: str | None
    messages: Annotated[list, add_messages]
