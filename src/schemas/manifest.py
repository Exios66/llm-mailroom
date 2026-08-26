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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self):
        self.updated_at = datetime.now(timezone.utc)
