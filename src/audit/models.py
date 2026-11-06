"""Value types for the unified audit row writers (spec §4, roadmap M3).

These are plain dataclasses: the terminal nodes, the review API, the
relations step and the rescore CLI build them, and ``audit.writer`` turns
them into ``audit_log`` / ``audit_attempts`` / ``audit_addenda`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EVENTS = ("archived", "failed", "triage_archived", "triage_reviewed")
ADDENDUM_KINDS = ("rescore", "resumed", "review_decision", "relations_linked", "recovered_detail")
STAGES = ("classification", "extraction", "judge")
TRIGGERS = ("initial", "retry", "arbiter_rescue", "human_correction", "transient", "resume")


class AuditWriteError(RuntimeError):
    """Base class for refusals by the audit writers."""


class AuditMigrationPending(AuditWriteError):
    """``audit_meta`` has no migration record, so ``audit_log`` may still hold
    legacy per-event rows. The writers refuse rather than mix row formats; the
    terminal nodes take the §5.6 fallback (roadmap M10)."""


class NoAuditRow(AuditWriteError):
    """An addendum was written for a document that has no row (spec §4.3)."""


@dataclass
class AttemptRow:
    """One attempt sub-row (spec §4.2). ``attempt_id`` becomes its ``entry_id``."""

    attempt_id: str
    stage: str
    attempt_no: int
    trigger: str
    outcome: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for an unknown stage or trigger."""
        if self.stage not in STAGES:
            raise ValueError(f"unknown attempt stage {self.stage!r}; expected one of {STAGES}")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"unknown attempt trigger {self.trigger!r}; expected one of {TRIGGERS}")


@dataclass
class TerminalRow:
    """The one fixed row a document gets when it reaches a terminal node (§4.1)."""

    doc_id: str
    matter_id: str
    event: str
    actor: str
    detail: dict[str, Any]
    attempts: list[AttemptRow] = field(default_factory=list)


@dataclass
class Addendum:
    """Something that happened to a document after its row (§4.3)."""

    doc_id: str
    kind: str
    actor: str
    detail: dict[str, Any]
    attempts: list[AttemptRow] = field(default_factory=list)


@dataclass(frozen=True)
class WriteResult:
    entry_id: str
    entry_hash: str
    kind: Literal["row", "addendum"]
    seq: int
