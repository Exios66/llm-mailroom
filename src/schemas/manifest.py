from pydantic import BaseModel, Field
from datetime import datetime, timezone
from enum import Enum
import uuid


class PipelineStage(str, Enum):
    INBOX = "inbox"
    PROCESSING = "processing"
    CLASSIFIED = "classified"
    REVIEW = "review"
    FAILED = "failed"
    ARCHIVED = "archived"


class DocumentManifest(BaseModel):
    doc_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    matter_id: str
    original_filename: str
    stage: PipelineStage = PipelineStage.INBOX
    doc_type: str | None = None
    contract_subtype: str | None = None
    doc_subclass: str | None = None
    classification_confidence: float | None = None
    classification_attempts: int = 0
    extracted_data: dict | None = None
    extraction_confidence: float | None = None
    extraction_attempts: int = 0
    trace_id: str | None = None
    escalation_reason: str | None = None
    review_decision: str | None = None
    # Lane B arbitration — durable on archive AND review/failed terminals.
    arbiter_decision: str | None = None
    arbiter_reasoning: str | None = None
    arbiter_handoff: str | None = None
    arbiter_fields_to_fix: list[str] | None = None
    arbiter_retry_count: int = 0
    judge_verdict: str | None = None
    judge_score: float | None = None
    judge_findings: list[str] | None = None
    # LangGraph interrupt() thread id so in-process Command(resume=...) can
    # continue the paused human_review node. Empty on older manifests and
    # when the checkpointer was lost (process restart + MemorySaver) — resume
    # then falls back to a fresh extract invoke.
    checkpoint_thread_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self):
        self.updated_at = datetime.now(timezone.utc)
