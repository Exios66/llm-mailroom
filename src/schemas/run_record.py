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
``--judge-attempts all``), plus, unless the sweep ran with ``--arbiter off``, the
two arbiters that follow them: the Lane B ``arbiter`` after every judge pass whose
verdict is ``partial`` or ``incomplete`` (``arbiters``), and the
``classification_arbiter`` after a decision-lane reviewer that did not accept
(``classification_arbiter``). Replay of those branches under any thresholds is
exact, not counterfactual. A route that sends a later, unjudged attempt to the
judge is replayed as ``unobserved``, and so is a route that reaches extraction
with a subclass the sweep did not extract (``extraction_candidates_collapsed``).
The boss is never run by the sweep, so a route that needs it stays unobserved even
on a full-information record. ``observed`` says which branches were run; replay
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
    # Class of the extraction whose judge verdict was arbitrated (a sweep extracts
    # several candidate classes and restarts attempt numbers per class). None on
    # older records: they match any class.
    doc_type: str | None = None
    # accept_with_caveats | retry_extraction | human_review (build_graph.arbiter_node)
    decision: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class ClassificationArbiterObservation(_Model):
    """The classification arbiter's ruling on a parked decision-lane review.

    Stores the agent's RAW answer, not the graph's verdict: whether the ruling is
    accepted depends on the class cutoff (``reviewer.by_class.<class>.arbiter_cutoff``,
    else the class's ``high``), which a policy changes, so replay re-applies the
    cutoff through the real node. ``sorter_*`` / ``reviewer_*`` are the two answers the
    arbiter was shown; replay uses the ruling only for a route that shows it the same
    pair. Default (``ran=False``): the sweep did not run it, or no review needed it.
    """

    ran: bool = False
    # accept_sorter | accept_reviewer | human_review, as the agent answered
    # (``human_review`` with no confidence when the call failed or the document
    # was too long to arbitrate).
    decision: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    sorter_doc_type: str | None = None
    sorter_doc_subclass: str | None = None
    reviewer_doc_type: str | None = None
    reviewer_doc_subclass: str | None = None
    transient_error: bool = False
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
    # Lane B arbiter ran after every partial/incomplete judge verdict (vacuously
    # true when there was none). False on records from before the arbiter was swept
    # and when swept with ``--arbiter off``.
    arbiter: bool = False
    # The classification arbiter ran wherever the live router would reach it
    # (vacuously true when no review needed it); same False cases as ``arbiter``.
    classification_arbiter: bool = False
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
    # Optional: the Lane B arbiter's ruling per judged attempt (``on_attempt``, and
    # ``doc_type`` when the sweep extracted several classes). Without it a judge verdict
    # of partial/incomplete cannot be replayed (the arbiter is not guessed).
    arbiters: list[ArbiterObservation] = Field(default_factory=list)
    # Optional: the classification arbiter's ruling after a decision-lane reviewer that
    # did not accept. Without it that route cannot be replayed.
    classification_arbiter: ClassificationArbiterObservation = Field(
        default_factory=ClassificationArbiterObservation
    )
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
