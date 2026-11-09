"""Per-document run record: the contract between experiments and the hill climb.

One JSON object per document per run, written as JSONL by
``scripts/run_calibration_sweep.py`` and read by ``scripts/hillclimb_eval.py``
and ``scripts/calibrate_thresholds.py``.

Why a separate record: the catalog keeps only the *last* confidence of each
stage (the reviewer even overwrites ``classification_confidence``), and runs
with ``ground_truth`` in state route differently from production
(``pipeline/reconsideration.class_misses_ground_truth``,
``coverage_below_floor``). A threshold can only be calibrated, and a routing
policy replayed offline, from records that keep every attempt's raw
confidence and the gold labels *outside* the routing state.

A *full-information* record observes the sorter, reviewer, extraction and judge
branches (sorter attempt 1 and the retry, the reviewer, every extraction attempt
and the judge on extraction attempt 1, or on every attempt when swept with
``--judge-attempts all``), so replay of those branches under any thresholds is
exact, not counterfactual. A route that sends a later, unjudged attempt to the
judge is replayed as ``unobserved``, and so is a route that reaches extraction
with a subclass the sweep did not extract (``extraction_candidates_collapsed``). The classification arbiter, the Lane B arbiter and the boss are
never run by the sweep, so a route that needs one of them stays unobserved even
on a full-information record.

Collapsed extraction candidates. The sweep extracts once per distinct candidate CLASS (sorter
attempt 1, the reviewer, the sorter retry). A later candidate of an already-extracted class is
listed in ``diagnostics.extraction_candidates_collapsed`` and its extraction is not run; each
entry carries ``subclass_differs``. The two kinds of difference are treated differently on
purpose. A different SUBCLASS changes the hint the specialist sees, so the record is not
``observed.full_information`` and a replayed route that arrives with that subclass is
``unobserved`` (``extraction_subclass_not_observed``). A difference ONLY in the handoff
confidence (printed to 2 decimals in the prompt) does not demote ``full_information``: it is
treated as below the model's own run-to-run noise, and the route reuses the extraction. That
is an approximation, so it is measured: ``policy_replay.evaluate`` reports
``replayed_with_confidence_only_handoff_diff``, the number of routes that reused such an
extraction, and ``hillclimb_eval.py`` prints it next to the G3 (unobserved) numbers.

Schema evidence. ``ExtractionAttempt.schema_valid`` is None only when there is no extracted
data to validate (a transient error, or no payload); the sweep records a bool whenever there
is an extraction (a validator crash is recorded as False and listed in
``diagnostics.schema_validation_errors``). Routing blocks only on an explicit False, and replay
mirrors that, so a None passes the gate. ``evaluate`` reports ``auto_archived_schema_unknown``,
the auto-archives whose accepted attempt had no schema verdict. ``observed`` says which branches were run; replay
counts a route that needs an unobserved branch as ``unobserved`` instead of
guessing its outcome.

Reviewer field names follow ``llm_dojo_scoring.scoring.reviewer.ReviewRecord``
so :func:`to_review_record` rows feed ``score_review_lane`` directly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RUN_RECORD_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunInfo(_Model):
    run_id: str
    git_sha: str | None = None
    taxonomy_sha256: str | None = None
    dojo_version: str | None = None
    dataset_id: str | None = None
    dataset_revision: str | None = None
    split: Literal["train", "test", "unsplit"] = "unsplit"
    mode: Literal["real", "mock", "synthetic"]
    # Must be False for any record used for calibration: GT in state changes routing.
    gt_in_state: bool = False


class Gold(_Model):
    expected_doc_type: str | None = None
    expected_doc_subclass: str | None = None
    # Routing-level gold from the corpus (§84 columns), when present.
    expected_stage: str | None = None
    review_expected: bool | None = None
    # Human-review labels (``scripts/export_review_labels.py``). ``label_source``
    # says whether gold came from the corpus or from a person's resolution.
    human_decision: str | None = None
    human_doc_type: str | None = None
    human_doc_subclass: str | None = None
    human_fields: dict[str, Any] | None = None
    label_source: Literal["corpus", "human"] | None = None


class SorterAttempt(_Model):
    attempt: int  # 1 = first pass (classify), 2+ = retry_classify
    doc_type: str | None = None
    doc_subclass: str | None = None
    confidence_raw: float | None = None  # model output, before apply_classification_guard
    confidence: float | None = None  # what routing saw (post-guard)
    guardrail_issues: list[str] = Field(default_factory=list)
    transient_error: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class ReviewerObservation(_Model):
    ran: bool = False
    mode: Literal["llm", "decision_only"] | None = None
    reviewer_doc_type: str | None = None
    reviewer_doc_subclass: str | None = None
    reviewer_confidence: float | None = None
    reviewer_verdict: str | None = None
    review_route: str | None = None  # decision lane only
    causes: list[str] = Field(default_factory=list)
    transient_error: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class ExtractionAttempt(_Model):
    attempt: int  # 1 = extract, 2+ = retry_extract
    # Class this extraction ran as. A full-information sweep extracts once per
    # distinct candidate class (sorter attempt 1 / reviewer / sorter retry),
    # so attempt numbers restart at 1 for each class. None on older records.
    doc_type: str | None = None
    confidence_raw: float | None = None
    confidence: float | None = None  # post-guard, what routing saw
    schema_valid: bool | None = None
    hollow: bool = False
    guardrail_issues: list[str] = Field(default_factory=list)
    conflict_detected: bool = False
    n_chunks: int = 1
    failed_chunks: int = 0
    # Per-window evidence of a chunked extraction (windows that parsed, document order;
    # same length). Lets a replay re-derive the merged confidence under another merge
    # rule (``policy_replay`` ``merge_rule``). None = recorded before CR6, or not chunked.
    window_confidences: list[float | None] | None = None
    window_has_fields: list[bool] | None = None
    # Offline Dojo score of THIS attempt vs gold fields (never seen by routing).
    extraction_score: float | None = None
    hallucination_rate: float | None = None
    transient_error: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class JudgeObservation(_Model):
    ran: bool = False
    on_attempt: int | None = None
    doc_type: str | None = None  # class of the extraction that was judged
    verdict: str | None = None  # complete | partial | incomplete | judge_error
    score: float | None = None
    source_verified: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class ArbiterObservation(_Model):
    """The arbiter's ruling after a judge pass on extraction attempt ``on_attempt``."""

    ran: bool = False
    on_attempt: int | None = None
    # Class of the extraction that was ruled on; attempt numbers restart per class in a
    # full-information sweep. None on older records (replay then needs the ruling to be unambiguous).
    doc_type: str | None = None
    # accept_with_caveats | retry_extraction | human_review (build_graph.arbiter_node)
    decision: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class Observed(_Model):
    """Which branches were actually executed for this document."""

    sorter_attempts: int = 0
    reviewer: bool = False
    extraction_attempts: int = 0
    judge: bool = False
    full_information: bool = False


class ActualRoute(_Model):
    """What the live graph did in this run (empty for sweep-only records)."""

    final_stage: str | None = None  # archived | review | failed
    route_path: list[str] = Field(default_factory=list)
    review_causes: list[str] = Field(default_factory=list)
    failure_class: str | None = None


class RunRecord(_Model):
    version: int = RUN_RECORD_VERSION
    run: RunInfo
    doc_id: str
    filename: str | None = None
    content_sha256: str | None = None
    text_chars: int | None = None
    truncated: bool = False
    gold: Gold = Field(default_factory=Gold)
    sorter: list[SorterAttempt] = Field(default_factory=list)
    reviewer: ReviewerObservation = Field(default_factory=ReviewerObservation)
    extraction: list[ExtractionAttempt] = Field(default_factory=list)
    judge: JudgeObservation = Field(default_factory=JudgeObservation)
    # Optional extra judge observations, one per extraction attempt the judge ran on
    # (``on_attempt``, and ``doc_type`` when the sweep extracted several classes). ``judge`` stays the primary one. A full-information sweep
    # fills this so replay can send ANY attempt to the judge without guessing.
    judges: list[JudgeObservation] = Field(default_factory=list)
    # Optional: the arbiter's ruling per judged attempt. Without it a judge verdict of
    # partial/incomplete cannot be replayed (the arbiter is not guessed).
    arbiters: list[ArbiterObservation] = Field(default_factory=list)
    observed: Observed = Field(default_factory=Observed)
    actual: ActualRoute = Field(default_factory=ActualRoute)
    thresholds_in_force: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Free-form sweep provenance that has no schema slot of its own: extraction
    # candidates, windows/chunks, downstream truncation, intake cost, known gaps.
    diagnostics: dict[str, Any] = Field(default_factory=dict)


def to_review_record(record: RunRecord) -> dict[str, Any]:
    """Map a run record onto ``llm_dojo_scoring`` ``ReviewRecord`` keys.

    Uses sorter attempt 1 (whatever its position in the list) as the sorter
    opinion, with post-guard confidence; a missing attempt 1 yields ``None``
    sorter fields. Reviewer opinion fields, token counts and cost are ``None``
    unless it ran (an unrun reviewer is unpriced, not free); its causes are
    copied regardless. ``route`` is left unset: it is a property of the policy,
    which replay decides (``calibrate_thresholds`` fills it from
    ``policy_replay.reviewer_route``).
    """
    first = next((a for a in record.sorter if a.attempt == 1), None)
    rev = record.reviewer
    return {
        "doc_id": record.doc_id,
        "expected_doc_type": record.gold.expected_doc_type,
        "expected_doc_subclass": record.gold.expected_doc_subclass,
        "sorter_doc_type": first.doc_type if first else None,
        "sorter_doc_subclass": first.doc_subclass if first else None,
        "sorter_confidence": first.confidence if first else None,
        "reviewer_doc_type": rev.reviewer_doc_type if rev.ran else None,
        "reviewer_doc_subclass": rev.reviewer_doc_subclass if rev.ran else None,
        "reviewer_confidence": rev.reviewer_confidence if rev.ran else None,
        "reviewer_verdict": rev.reviewer_verdict if rev.ran else None,
        "causes": list(rev.causes),
        "human_decision": record.gold.human_decision,
        "human_doc_type": record.gold.human_doc_type,
        "human_doc_subclass": record.gold.human_doc_subclass,
        "prompt_tokens": rev.prompt_tokens if rev.ran else None,
        "completion_tokens": rev.completion_tokens if rev.ran else None,
        "cost_usd": rev.cost_usd if rev.ran else None,
        "dataset_revision": record.run.dataset_revision,
        "split": record.run.split,
    }


_WINDOW_KEYS = ("window_confidences", "window_has_fields")


def _drop_empty_window_keys(value):
    if isinstance(value, dict):
        return {
            k: _drop_empty_window_keys(v)
            for k, v in value.items()
            if not (k in _WINDOW_KEYS and v is None)
        }
    if isinstance(value, list):
        return [_drop_empty_window_keys(v) for v in value]
    return value


def record_jsonl_line(record: "RunRecord") -> str:
    """Compact JSON for one record, without per-window keys that carry no evidence.

    Main and earlier branches validate with ``extra="forbid"`` and do not know the CR6
    window fields, so an attempt without window evidence is written without them and
    stays readable there. A chunked attempt that carries evidence keeps them: that data
    needs this schema to be read.
    """
    import json

    return json.dumps(_drop_empty_window_keys(record.model_dump(mode="json")), separators=(",", ":"))
