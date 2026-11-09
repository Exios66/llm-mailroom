"""Offline policy replay and the hill-climb objective.

Replays a confidence-threshold *policy* (a dict shaped like the ``confidence:``
block of ``config/taxonomy.yaml``, including ``by_class``) over per-document
:class:`schemas.run_record.RunRecord` rows, through the REAL routing functions
in ``graph.routing``, to a terminal outcome:

``auto_archived``  the policy would archive without a person
``human_review``   the policy would park the document for a person
``unobserved``     the policy needs a branch the record did not observe (a retry
                   attempt, the reviewer, the judge, the arbiter, the boss, a
                   transient-error retry). The outcome is never guessed; the
                   document is charged a human review and counted in guard G3.

Nothing here re-implements a routing decision. At each decision point a state
dict is synthesized from the record and handed to ``graph.routing.after_*``
(looked up on the module at call time, so monkeypatching a routing function
changes the replay). Two node-level derivations that routing reads are mirrored
because the record stores their *inputs*, not the policy-dependent result: the
LLM reviewer's verdict (``build_graph.review_classify_node``: agree/override
against the class ``high``) and the extraction payload's schema/hollow flags,
which are stood in for by a stub payload plus a patched ``validate_extraction``.

States are built WITHOUT ``ground_truth`` (production-faithful: routing reads
``ground_truth`` through ``class_misses_ground_truth`` / ``coverage_below_floor``
and live traffic has none). ``oracle=True`` adds the gold class to the state and
must be reported separately, never as the objective.

The objective (every constant is a parameter, see :class:`ObjectiveParams`)::

    cost per doc  = 0                     auto-archived and correct
                  = C_e                   auto-archived and wrong (or unknown)
                  = C_h                   human_review, or unobserved
                  + LLM_usd / review_cost_usd * C_h     on every document
    SCORE  U      = 1 - mean(cost) / C_h

LLM_usd is the spend of the nodes the policy visits plus the recorded intake
cost (``diagnostics.intake.cost_usd``), which every policy pays. An auto-archived
document whose human label is ``rejected`` counts as wrong.

All-human with free LLM scores 0; perfect free automation scores 1; a wrong
auto-archive at C_e = 20 C_h scores deeply negative. Chow's rule: auto-accept
only when P(correct) >= 1 - C_h / C_e (0.95 at the defaults), which is the
calibrator's default target. Guards G1..G4 can veto a high U.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Mapping, Sequence
from unittest import mock

import yaml

from schemas.run_record import (
    ArbiterObservation,
    ExtractionAttempt,
    JudgeObservation,
    ReviewerObservation,
    RunRecord,
    SorterAttempt,
)

AUTO = "auto_archived"
HUMAN = "human_review"
UNOBSERVED = "unobserved"

SPLITS = ("train", "test", "all")


# ---------------------------------------------------------------------------
# Objective parameters
# ---------------------------------------------------------------------------


@dataclass
class ObjectiveParams:
    """Every constant of the objective and its guards."""

    c_human: float = 1.0
    c_error: float = 20.0
    review_cost_usd: float = 4.0
    field_floor: float = 0.80
    risk: float = 0.02  # G1: wrong-auto Clopper-Pearson upper bound ceiling
    confidence: float = 0.95
    class_wrong_rate_max: float = 0.05  # G2
    class_min_auto: int = 30  # G2 applies to classes with at least this many auto-archives
    unobserved_max: float = 0.01  # G3
    cost_budget_usd: float | None = None  # G4 (None = off)
    oracle: bool = False

    @property
    def accept_target(self) -> float:
        """Chow's rule: accept only when P(correct) >= 1 - C_h / C_e."""
        return accept_target(self.c_human, self.c_error)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | "ObjectiveParams" | None) -> "ObjectiveParams":
        if data is None:
            return cls()
        if isinstance(data, cls):
            return data
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown objective parameter(s): {sorted(unknown)}")
        return cls(**dict(data))


def accept_target(c_human: float = 1.0, c_error: float = 20.0) -> float:
    """The implied auto-accept precision, ``1 - C_h / C_e``."""
    costs = (c_human, c_error)
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in costs):
        raise ValueError(f"c_human and c_error must be finite numbers, got {c_human!r}, {c_error!r}")
    if c_error <= c_human or c_human <= 0:
        raise ValueError("need c_error > c_human > 0")
    return 1.0 - c_human / c_error


# ---------------------------------------------------------------------------
# Records and policies
# ---------------------------------------------------------------------------


def load_records(path: str | Path) -> list[RunRecord]:
    """Read a JSONL file of :class:`RunRecord` rows (blank lines are skipped)."""
    records: list[RunRecord] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(RunRecord.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001 - re-raised with the location
                raise ValueError(f"{path}:{lineno}: not a valid RunRecord: {exc}") from exc
    return records


def dump_records(records: Iterable[RunRecord], path: str | Path) -> int:
    """Write records as JSONL; returns the number written."""
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec.model_dump_json() + "\n")
            n += 1
    return n


def select_split(records: Sequence[RunRecord], split: str) -> tuple[list[RunRecord], list[str]]:
    """Filter by ``run.split``. Returns ``(records, warnings)``.

    ``test`` falls back to every record, with a warning, when none are split at
    all (all ``unsplit``). A ``test``/``train`` request on a split corpus that
    has no such rows yields an empty list, which callers treat as bad input.
    """
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    warnings: list[str] = []
    if split == "all":
        return list(records), warnings
    picked = [r for r in records if r.run.split == split]
    if not picked and records and all(r.run.split == "unsplit" for r in records):
        warnings.append(
            f"records are unsplit: scoring and tuning on ALL {len(records)} records "
            "(no held-out data; the result is optimistic)"
        )
        return list(records), warnings
    return picked, warnings


def current_policy() -> dict[str, Any]:
    """The ``confidence:`` block in force right now (taxonomy, or an active override)."""
    import pipeline.config as cfg

    return copy.deepcopy(dict(cfg.load_config().get("confidence") or {}))


def merge_policy(base: Mapping[str, Any], overlay: Mapping[str, Any] | None) -> dict[str, Any]:
    """Overlay ``overlay`` on ``base``; ``by_class`` entries merge per class."""
    out = copy.deepcopy(dict(base))
    for key, value in (overlay or {}).items():
        if key == "by_class":
            by_class = dict(out.get("by_class") or {})
            for cls, entry in (value or {}).items():
                merged = dict(by_class.get(cls) or {})
                merged.update(entry or {})
                by_class[cls] = merged
            out["by_class"] = by_class
        else:
            out[key] = value
    return out


def load_policy(path: str | Path | None, *, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load a policy YAML (``{confidence: {...}}`` or a bare block) over ``base``.

    ``None`` returns ``base`` (default: the current taxonomy ``confidence:``).
    A file may be partial; it is overlaid on the base.
    """
    base_policy = copy.deepcopy(dict(base)) if base is not None else current_policy()
    if path is None:
        return base_policy
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: policy must be a mapping")
    block = data.get("confidence", data)
    if not isinstance(block, dict):
        raise ValueError(f"{path}: 'confidence' must be a mapping")
    return merge_policy(base_policy, block)


@contextlib.contextmanager
def policy_override(policy: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Make ``pipeline.config`` serve ``policy`` as the ``confidence:`` block.

    Patches ``pipeline.config.load_config`` (which ``get_confidence_thresholds``,
    ``get_review_mode`` and ``resolve_extract_class`` call through), so routing
    sees the candidate thresholds. On exit the original function is restored and
    the config caches are cleared. Not thread-safe. Modules that did
    ``from pipeline.config import load_config`` keep the original and are not
    affected (routing and reconsideration are not among them).
    """
    import pipeline.config as cfg

    original = cfg.load_config
    effective = copy.deepcopy(dict(policy)) if policy is not None else current_policy()
    patched_cfg = copy.deepcopy(dict(original()))
    patched_cfg["confidence"] = effective

    def _load_config() -> dict:
        return patched_cfg

    _load_config.cache_clear = lambda: None  # type: ignore[attr-defined]
    cfg.load_config = _load_config  # type: ignore[assignment]
    try:
        yield effective
    finally:
        cfg.load_config = original  # type: ignore[assignment]
        cfg.clear_config_cache()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass
class ReplayOutcome:
    outcome: str  # auto_archived | human_review | unobserved
    final_class: str | None = None
    path: list[str] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    llm_calls: int = 0
    extraction_score: float | None = None
    reasons: list[str] = field(default_factory=list)
    extraction_attempt: int | None = None  # the attempt that produced the archived/final payload
    doc_id: str = ""
    # The accepted/final extraction attempt has no recorded schema verdict (``schema_valid`` None).
    # Routing blocks only on an explicit False, so such an attempt passes the schema gate on
    # missing evidence; ``evaluate`` counts the auto-archived ones.
    schema_unknown: bool = False
    # The extraction was reused from a collapsed candidate whose handoff confidence differed from
    # the extracted one (same class and subclass; see ``extraction_candidates_collapsed``).
    handoff_confidence_diff: bool = False


class _NullLogger:
    """Swallows the structlog calls routing makes on every decision."""

    def __getattr__(self, _name: str):
        return lambda *a, **k: None


_QUIET_DEPTH = 0
_SCHEMA_VALID: dict[str, bool] = {"value": True}
_MERGE_RULE: dict[str, str] = {"value": "min"}


@contextlib.contextmanager
def _merge_rule(rule: str) -> Iterator[None]:
    """Chunk-window merge rule in force for a replay: ``min`` or ``cr6`` (see ``merge_window_confidences``)."""
    if rule not in ("min", "cr6"):
        raise ValueError(f"merge_rule must be 'min' or 'cr6', got {rule!r}")
    previous = _MERGE_RULE["value"]
    _MERGE_RULE["value"] = rule
    try:
        yield
    finally:
        _MERGE_RULE["value"] = previous


@contextlib.contextmanager
def _replay_env() -> Iterator[None]:
    """Quiet routing's loggers and stand in for ``validate_extraction`` (re-entrant)."""
    global _QUIET_DEPTH
    import graph.routing as routing
    import pipeline.reconsideration as recon

    if _QUIET_DEPTH:
        _QUIET_DEPTH += 1
        try:
            yield
        finally:
            _QUIET_DEPTH -= 1
        return

    def _fake_validate(_doc_type: str, _data: Any) -> dict:
        return {"parse_error": False, "schema_valid": _SCHEMA_VALID["value"]}

    _QUIET_DEPTH = 1
    try:
        with (
            mock.patch.object(routing, "logger", _NullLogger()),
            mock.patch.object(recon, "logger", _NullLogger(), create=True),
            mock.patch.object(routing, "validate_extraction", _fake_validate),
        ):
            yield
    finally:
        _QUIET_DEPTH = 0


def _handoff_text(confidence: Any) -> str | None:
    """A handoff confidence as the sweep prints it into ``extraction_candidates_collapsed`` (2 decimals)."""
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        return None
    return f"{float(confidence):.2f}"


def _att(items: Sequence[Any], n: int) -> Any | None:
    for item in items:
        if item.attempt == n:
            return item
    return None


def _conf(att: SorterAttempt | ExtractionAttempt) -> float | None:
    return att.confidence if att.confidence is not None else att.confidence_raw


def _window_evidence_usable(att: ExtractionAttempt) -> bool:
    confs, fields = att.window_confidences, att.window_has_fields
    return confs is not None and fields is not None and len(confs) == len(fields) > 0


def _extraction_confidence(att: ExtractionAttempt, cls: str | None) -> float | None:
    """The extraction confidence routing sees under the thresholds in force.

    A chunked extraction that lost a window is capped strictly below the class
    ``low`` at extraction time (``specialist_agents.finalize_chunked_merge``), so
    the recorded value embeds the sweep's thresholds. Re-apply the cap under the
    candidate's, otherwise lowering ``low`` below the recorded cap would let a
    partial extraction through that the live graph would retry.
    """
    conf = _conf(att)
    if _window_evidence_usable(att):
        # Re-derive the merge from the per-window evidence under the rule in force, so ``min``
        # and ``cr6`` are compared on the same records whichever rule the sweep ran under.
        from langchain_agents.specialist_agents import merge_window_confidences

        derived = merge_window_confidences(att.window_confidences, att.window_has_fields, _MERGE_RULE["value"])
        if derived is not None:
            conf = derived
            if att.guardrail_issues:  # the extraction guard clamps after the merge (guards.py)
                from pipeline.guards import _GUARD_CONFIDENCE_CEILING

                conf = min(conf, _GUARD_CONFIDENCE_CEILING)
    if att.failed_chunks <= 0:
        return conf
    from langchain_agents.specialist_agents import failed_chunk_ceiling
    from pipeline.config import load_config

    specialist = next(
        (c.get("specialist") for c in load_config().get("doc_classes") or [] if c.get("key") == cls), None
    )
    ceiling = failed_chunk_ceiling(specialist or cls or "")
    return ceiling if conf is None else min(conf, ceiling)


def _for_class(items: Sequence[Any], cls: str | None) -> list[Any]:
    """Observations of class ``cls``: a sweep extracts once per candidate class and
    restarts attempt numbers per class. Untyped (older) observations match any class."""
    return [i for i in items if getattr(i, "doc_type", None) in (None, cls)]


def _judge_for(record: RunRecord, k: int, cls: str | None = None) -> JudgeObservation | None:
    """The judge observation that judged extraction attempt ``k`` of class ``cls``."""
    ran = [j for j in _for_class([record.judge, *record.judges], cls) if j.ran]
    for j in ran:
        if j.on_attempt == k:
            return j
    last = max((a.attempt for a in _for_class(record.extraction, cls)), default=None)
    for j in ran:
        if j.on_attempt is None and k == last:
            return j
    return None


def _arbiter_for(record: RunRecord, k: int, cls: str | None = None) -> ArbiterObservation | None:
    """The arbiter ruling that followed the judge pass on extraction attempt ``k`` of class ``cls``.

    A sweep restarts attempt numbers per class, so several rulings can share ``on_attempt``. A ruling
    typed with ``cls`` wins. An untyped (older) ruling is used only when it is the only ruling for
    that attempt: beside any other ruling its class is unknowable, so it counts as unobserved.
    """
    ran = [a for a in record.arbiters if a.ran and a.on_attempt == k]
    typed = [a for a in ran if a.doc_type == cls]
    if typed:
        return typed[0]
    return ran[0] if len(ran) == 1 and ran[0].doc_type is None else None


def llm_review_verdict(
    sorter_type: str | None, reviewer_type: str | None, reviewer_confidence: float | None
) -> str:
    """The LLM reviewer's verdict under the thresholds in force.

    Mirrors ``graph.build_graph.review_classify_node`` (agree uses the sorter
    class's ``high``, override the reviewer class's ``high``): the verdict is a
    function of the policy, so a recorded verdict cannot be reused.
    """
    from pipeline.config import get_confidence_thresholds

    if reviewer_type is None or reviewer_confidence is None:
        return "reviewer_error"
    agree_high = get_confidence_thresholds(sorter_type).get("high", 0.97)
    override_high = get_confidence_thresholds(reviewer_type).get("high", 0.97)
    if reviewer_type == sorter_type:
        return "reviewer_agrees_high" if reviewer_confidence >= agree_high else "reviewer_agrees_low"
    return "reviewer_overrides" if reviewer_confidence >= override_high else "reviewer_conflicts"


def _apply_reviewer(state: dict[str, Any], rev: ReviewerObservation) -> str | None:
    """Fold the reviewer step into ``state`` as ``review_classify_node`` would.

    Returns an ``unobserved`` reason, or ``None`` when ``state`` is ready for
    ``after_review_classify``. The LLM lane's verdict is recomputed under the
    thresholds in force; a decision-model lane's route is taken as observed
    (its cutoffs live in ``reviewer:``, not in a confidence policy).
    """
    from pipeline.config import get_review_mode

    config_mode = get_review_mode(state.get("doc_type"))
    if rev.mode is not None and rev.mode != config_mode:
        return f"reviewer_mode_mismatch: record={rev.mode} config={config_mode}"
    state.update(
        reviewer_doc_type=rev.reviewer_doc_type,
        reviewer_doc_subclass=rev.reviewer_doc_subclass,
        reviewer_confidence=rev.reviewer_confidence,
        reviewer_causes=list(rev.causes),
    )
    if config_mode == "decision_only":
        if rev.review_route is None:
            return "decision_route_not_observed"
        state["review_verdict"] = rev.reviewer_verdict
        state["review_route"] = rev.review_route
        if rev.review_route == "extract" and rev.reviewer_doc_type is not None:
            state["doc_type"] = rev.reviewer_doc_type
            state["doc_subclass"] = rev.reviewer_doc_subclass
            state["classification_confidence"] = rev.reviewer_confidence
    else:
        verdict = llm_review_verdict(state.get("doc_type"), rev.reviewer_doc_type, rev.reviewer_confidence)
        state["review_verdict"] = verdict
        if verdict in ("reviewer_agrees_high", "reviewer_overrides"):
            # Mirror review_classify_node: a winning verdict re-labels the subclass too (None
            # when the reviewer named none, the same way live clears it). Leaving the sorter's
            # subclass in place would let the collapsed-candidate guard compare against an
            # extraction hint the route never had, and an override to another class would
            # inherit the previous class's subclass.
            state["doc_type"] = rev.reviewer_doc_type
            state["doc_subclass"] = rev.reviewer_doc_subclass
            state["classification_confidence"] = rev.reviewer_confidence
    return None


def reviewer_route(record: RunRecord, policy: Mapping[str, Any] | None = None) -> str | None:
    """Would the reviewer's answer on sorter attempt 1 be accepted (``extract``) under ``policy``?

    Independent of whether the sorter's own confidence sends the document to
    the reviewer. ``None`` when the reviewer or attempt 1 was not observed.
    """
    a1, rev = _att(record.sorter, 1), record.reviewer
    if a1 is None or not rev.ran or rev.transient_error:
        return None
    with policy_override(policy) if policy is not None else contextlib.nullcontext():
        import graph.routing as routing

        with _replay_env():
            state = {
                "doc_id": record.doc_id,
                "doc_type": a1.doc_type,
                "classification_confidence": _conf(a1),
                "classification_attempts": 1,
                "transient_error": False,
            }
            if _apply_reviewer(state, rev):
                return None
            dest = routing.after_review_classify(state)
    return "extract" if dest == "extract" else "human_review"


def _intake_cost_usd(record: RunRecord) -> float:
    """LLM spend of the intake step as the sweep recorded it (``diagnostics.intake.cost_usd``).

    Intake runs before any routing decision, so it is the same under every
    policy and is charged once per document. Absent or malformed means 0.
    """
    intake = record.diagnostics.get("intake") if isinstance(record.diagnostics, dict) else None
    value = intake.get("cost_usd") if isinstance(intake, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


class _Walk:
    def __init__(self, record: RunRecord, oracle: bool) -> None:
        self.record = record
        self.oracle = oracle
        self.path: list[str] = []
        self.cost = _intake_cost_usd(record)
        self.calls = 1 if self.cost > 0 else 0
        self.reasons: list[str] = []
        self.final_class: str | None = None
        self.score: float | None = None
        self.attempt: int | None = None
        self.schema_unknown = False
        self.handoff_confidence_diff = False

    def visit(self, node: str, cost: float, calls: int = 1) -> None:
        self.path.append(node)
        self.cost += float(cost or 0.0)
        self.calls += calls

    def gt(self) -> dict[str, Any]:
        if self.oracle and self.record.gold.expected_doc_type:
            return {"ground_truth": {"expected_doc_class": self.record.gold.expected_doc_type}}
        return {}

    def done(self, outcome: str, *reasons: str) -> ReplayOutcome:
        self.reasons.extend(reasons)
        return ReplayOutcome(
            outcome=outcome,
            final_class=self.final_class,
            path=self.path,
            llm_cost_usd=round(self.cost, 10),
            llm_calls=self.calls,
            extraction_score=self.score,
            reasons=self.reasons,
            extraction_attempt=self.attempt,
            doc_id=self.record.doc_id,
            schema_unknown=self.schema_unknown,
            handoff_confidence_diff=self.handoff_confidence_diff,
        )

    def unobserved(self, reason: str) -> ReplayOutcome:
        return self.done(UNOBSERVED, reason)


def replay(
    record: RunRecord,
    policy: Mapping[str, Any] | None = None,
    *,
    oracle: bool = False,
    merge_rule: Literal["min", "cr6"] = "min",
) -> ReplayOutcome:
    """Replay ``policy`` over one record through the real routing functions.

    ``policy=None`` replays under whatever ``pipeline.config`` currently serves
    (the taxonomy, or an enclosing :func:`policy_override`). A policy is
    applied for the duration of the call. ``merge_rule`` re-derives a chunked extraction's
    confidence from its per-window evidence; a chunked attempt recorded without that evidence
    is ``unobserved`` under any rule but ``min``.
    """
    if policy is not None:
        with policy_override(policy):
            return replay(record, None, oracle=oracle, merge_rule=merge_rule)
    with _replay_env(), _merge_rule(merge_rule):
        return _replay(record, oracle)


def _replay(record: RunRecord, oracle: bool) -> ReplayOutcome:
    import graph.routing as routing

    w = _Walk(record, oracle)
    if record.error:
        return w.unobserved(f"record_error: {record.error[:80]}")

    # ---- classify -------------------------------------------------------
    a1 = _att(record.sorter, 1)
    if a1 is None:
        return w.unobserved("sorter_attempt_1_not_observed")
    w.visit("classify", a1.cost_usd)
    if a1.transient_error:
        return w.unobserved("sorter_attempt_1_transient_error")
    state: dict[str, Any] = {
        "doc_id": record.doc_id,
        "doc_type": a1.doc_type,
        "doc_subclass": a1.doc_subclass,
        "classification_confidence": _conf(a1),
        "classification_attempts": 1,
        "transient_error": False,
        **w.gt(),
    }
    w.final_class = a1.doc_type
    dest = routing.after_classify(state)

    if dest == "retry_classify":
        a2 = _att(record.sorter, 2)
        if a2 is None:
            return w.unobserved("sorter_retry_not_observed")
        w.visit("retry_classify", a2.cost_usd)
        if a2.transient_error:
            return w.unobserved("sorter_attempt_2_transient_error")
        state.update(
            doc_type=a2.doc_type,
            doc_subclass=a2.doc_subclass,
            classification_confidence=_conf(a2),
            classification_attempts=2,
        )
        w.final_class = a2.doc_type
        dest = routing.after_retry_classify(state)

    if dest == "review_classify":
        rev = record.reviewer
        if not rev.ran:
            return w.unobserved("reviewer_not_observed")
        w.visit("review_classify", rev.cost_usd)
        if rev.transient_error:
            return w.unobserved("reviewer_transient_error")
        reason = _apply_reviewer(state, rev)
        if reason:
            return w.unobserved(reason)
        w.final_class = state.get("doc_type")
        dest = routing.after_review_classify(state)
        if dest == "classification_arbiter":
            return w.unobserved("classification_arbiter_not_observed")

    if dest == HUMAN:
        w.path.append("human_review")
        return w.done(HUMAN)
    if dest != "extract":
        return w.unobserved(f"classification_route_not_replayable: {dest}")

    # ---- extract / judge / arbiter / report -------------------------------
    cls = state.get("doc_type")
    w.final_class = cls
    # The sweep extracts each class once, on its first candidate. When a later candidate
    # of the same class had another subclass, its extraction was not run: a route that
    # arrives with that subclass cannot reuse the first candidate's extraction.
    for c in record.diagnostics.get("extraction_candidates_collapsed") or []:
        if (
            isinstance(c, dict)
            and c.get("doc_type") == cls
            and c.get("subclass_differs")
            and state.get("doc_subclass") != c.get("extracted_doc_subclass")
        ):
            return w.unobserved("extraction_subclass_not_observed")
    # A confidence-only difference (the handoff prints it to 2 decimals) does not make the route
    # unobserved: the extraction is reused. It is counted so the effect is measurable.
    handoff = _handoff_text(state.get("classification_confidence"))
    for c in record.diagnostics.get("extraction_candidates_collapsed") or []:
        if (
            isinstance(c, dict)
            and c.get("doc_type") == cls
            and not c.get("subclass_differs")
            and c.get("handoff_confidence") == handoff
            and c.get("handoff_confidence") != c.get("extracted_handoff_confidence")
        ):
            w.handoff_confidence_diff = True
    k = 1
    judge_passes = 0
    arbiter_retries = 0
    judge_verified = False  # judge_source_verified persists in state across attempts
    while True:
        att = _att(_for_class(record.extraction, cls), k)
        if att is None:
            return w.unobserved(f"extraction_attempt_{k}_not_observed")
        w.visit("extract" if k == 1 else "retry_extract", att.cost_usd, calls=max(1, att.n_chunks))
        if att.transient_error:
            return w.unobserved(f"extraction_attempt_{k}_transient_error")
        if _MERGE_RULE["value"] != "min" and att.n_chunks > 1 and not _window_evidence_usable(att):
            return w.unobserved(f"extraction_attempt_{k}_window_evidence_not_observed")
        w.score = att.extraction_score
        w.attempt = k
        w.schema_unknown = att.schema_valid is None
        _SCHEMA_VALID["value"] = att.schema_valid is not False
        # The live specialists stamp lost windows INTO the payload (``_failed_chunks``), and
        # ``_chunks_lost`` refuses to let a judge verdict clear hard evidence of missing data.
        # Without the key here a recorded complete+source-verified judge pass would archive a
        # partial extraction the live graph retried.
        ex_state: dict[str, Any] = {
            "doc_id": record.doc_id,
            "doc_type": cls,
            "extraction_confidence": _extraction_confidence(att, cls),
            "extraction_attempts": k,
            "extracted_data": {} if att.hollow else {
                "replay_field": "x",
                **({"_failed_chunks": att.failed_chunks} if att.failed_chunks else {}),
            },
            "extraction_guardrail": list(att.guardrail_issues),
            "conflict_detected": bool(att.conflict_detected),
            "transient_error": False,
            "judge_source_verified": judge_verified,
            "judge_pass_count": judge_passes,
            "arbiter_retry_count": arbiter_retries,
            **w.gt(),
        }
        gated = routing.after_extraction_gated if k == 1 else routing.after_retry_extraction_gated
        dest = gated(ex_state)
        if dest == "retry_extract":
            k += 1
            continue
        if dest == "judge_verify":
            j = _judge_for(record, k, cls)
            if j is None:
                return w.unobserved(f"judge_not_observed_on_attempt_{k}")
            w.visit("judge_verify", j.cost_usd)
            judge_passes += 1
            judge_verified = bool(j.source_verified)
            ex_state.update(
                judge_verdict=j.verdict,
                judge_source_verified=judge_verified,
                judge_pass_count=judge_passes,
            )
            dest = routing.after_judge(ex_state)
            if dest == "arbiter":
                arb = _arbiter_for(record, k, cls)
                if arb is None:
                    return w.unobserved(f"arbiter_not_observed_on_attempt_{k}")
                w.visit("arbiter", arb.cost_usd)
                if arb.decision == "retry_extraction":
                    arbiter_retries += 1  # arbiter_node counts approvals, not rulings
                ex_state.update(arbiter_decision=arb.decision, arbiter_retry_count=arbiter_retries)
                dest = routing.after_arbiter(ex_state)
                if dest == "retry_extract":
                    k += 1
                    continue
            elif dest == "judge_verify":
                return w.unobserved("judge_retry_not_replayable")
        break

    if dest == "boss_escalation":
        return w.unobserved("boss_escalation_not_observed")
    if dest == HUMAN:
        w.path.append("human_review")
        return w.done(HUMAN)
    if dest != "compile_report":
        return w.unobserved(f"extraction_route_not_replayable: {dest}")

    w.path.append("compile_report")
    ex_state["extracted_data"] = {"replay_field": "x", "_report": {"ok": True}}
    if routing.after_report(ex_state) != "catalog_write":
        w.path.append("human_review")
        return w.done(HUMAN)
    w.path.extend(["catalog_write", "archive"])
    return w.done(AUTO)


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------


def grade_auto(record: RunRecord, out: ReplayOutcome, params: ObjectiveParams) -> str:
    """``correct`` | ``wrong`` | ``unscored`` for an auto-archived document.

    ``unscored`` (no gold class, or right class with no extraction score) is
    charged as wrong in the objective and in G1/G2: unknown is not correct.
    """
    if str(record.gold.human_decision or "").strip().lower() == "rejected":
        return "wrong"  # a person rejected the document: archiving it is an error whatever the fields say
    gold = record.gold.expected_doc_type
    if gold is None:
        return "unscored"
    if out.final_class != gold:
        return "wrong"
    if out.extraction_score is None:
        return "unscored"
    return "correct" if out.extraction_score >= params.field_floor else "wrong"


def sorter_pairs(records: Iterable[RunRecord]) -> dict[str, list[tuple[float, bool]]]:
    """Per predicted class of sorter attempt 1: ``(confidence routing saw, class correct)``."""
    out: dict[str, list[tuple[float, bool]]] = {}
    for rec in records:
        a1 = _att(rec.sorter, 1)
        gold = rec.gold.expected_doc_type
        if a1 is None or gold is None or a1.doc_type is None:
            continue
        conf = _conf(a1)
        if conf is None:
            continue
        out.setdefault(a1.doc_type, []).append((float(conf), a1.doc_type == gold))
    return out


def extraction_evidence(
    records: Iterable[RunRecord], field_floor: float = 0.80
) -> tuple[dict[str, list[tuple[float, bool]]], dict[str, list[int]]]:
    """Per class: pooled ``(confidence, extraction_score >= floor)`` pairs, and each pair's document.

    Returns ``(pairs, docs)``; ``docs[cls][i]`` is the position in ``records`` of the document
    that produced ``pairs[cls][i]``, so a caller can count documents separately from attempts.
    See :func:`extraction_pairs` for exactly what is pooled.

    A document is identified by its ``doc_id``: records of one document from several sweeps share
    an index (the first row that carried it), so its attempts stay together. A record without a
    ``doc_id`` is its own document.
    """
    pairs: dict[str, list[tuple[float, bool]]] = {}
    docs: dict[str, list[int]] = {}
    first_row: dict[str, int] = {}
    for index, rec in enumerate(records):
        a1 = _att(rec.sorter, 1)
        gold = rec.gold.expected_doc_type
        if a1 is None or gold is None or a1.doc_type != gold:
            continue
        doc = first_row.setdefault(rec.doc_id, index) if rec.doc_id else index
        for att in _for_class(rec.extraction, gold):
            conf = _conf(att)
            if att.extraction_score is None or conf is None:
                continue
            if att.schema_valid is False or att.hollow:
                # Routing retries these whatever their confidence (the schema and hollow gates in
                # ``after_extraction``), so they can never be auto-archived at any cutoff: a poor score
                # here would only mark a confident, valid retry's cutoff as unearned.
                continue
            pairs.setdefault(gold, []).append((float(conf), att.extraction_score >= field_floor))
            docs.setdefault(gold, []).append(doc)
    return pairs, docs


def extraction_pairs(
    records: Iterable[RunRecord], field_floor: float = 0.80
) -> dict[str, list[tuple[float, bool]]]:
    """Per class: ``(extraction confidence, extraction_score >= floor)``, EVERY scored attempt pooled.

    What is pooled: for each record whose sorter attempt 1 named the gold class (so the pairs
    isolate extraction calibration from classification errors), every extraction attempt of
    the gold class that has both a confidence and an offline score and that routing could
    archive (not ``schema_valid`` False, not hollow: those are retried at any confidence). A
    document with three scored attempts contributes three pairs; an attempt of another class
    never counts.

    Why every attempt and not the document's final one: routing auto-archives ANY attempt
    whose confidence clears the cutoff, including attempt 1 before a retry would have run
    (``after_extraction``: a confident attempt 1 goes straight to the report). The final
    attempt is usually the best one, so scoring only it would credit a confidently wrong
    attempt 1 to the retry's accuracy and overstate the accuracy at a cutoff. A retry is only
    reached when the earlier attempts fell below the cutoff, so pooling all attempts also
    over-weights low-confidence evidence, which is the conservative direction for a cutoff.

    Counts over these pairs are ATTEMPTS, not documents, and attempts of one document are not
    independent samples (:func:`extraction_evidence` keeps the document index for the count).
    """
    return extraction_evidence(records, field_floor)[0]


def _cp_upper(errors: int, n: int, confidence: float) -> float | None:
    from llm_dojo_scoring.analysis.calibration import clopper_pearson_upper

    return clopper_pearson_upper(errors, n, confidence=confidence) if n > 0 else None


def sorter_calibration(records: Iterable[RunRecord]) -> dict[str, dict[str, Any]]:
    """ECE / Brier / AUROC of the sorter's attempt-1 confidence, per class."""
    from llm_dojo_scoring.analysis.calibration import auroc, brier_score, expected_calibration_error

    table: dict[str, dict[str, Any]] = {}
    for cls, pairs in sorted(sorter_pairs(records).items()):
        table[cls] = {
            "n": len(pairs),
            "accuracy": sum(1 for _, ok in pairs if ok) / len(pairs),
            "ece": expected_calibration_error(pairs),
            "brier": brier_score(pairs),
            "auroc": auroc([c for c, _ in pairs], [ok for _, ok in pairs]),
        }
    return table


def evaluate(
    records: Sequence[RunRecord],
    policy: Mapping[str, Any] | None = None,
    objective_params: ObjectiveParams | Mapping[str, Any] | None = None,
    *,
    merge_rule: Literal["min", "cr6"] = "min",
) -> dict[str, Any]:
    """Replay ``policy`` over ``records`` and score it: metrics, guards, SCORE.

    ``merge_rule`` is the chunk-window confidence rule (see :func:`replay`).

    ``policy=None`` uses the current taxonomy. ``result["score"]`` is U;
    ``result["status"]`` is ``ok`` or ``guard_fail``; ``result["guards"]`` lists
    G1..G4 with ``pass`` True/False (G4 ``skipped`` when no budget is set).
    ``result["per_doc_cost"]`` maps ``doc_id`` to its cost in C_h units, for
    paired comparisons (:func:`paired_delta`); a repeated ``doc_id`` (concatenated
    runs) is keyed ``"<doc_id>#2"``, ``"#3"``... in input order, so evaluating the
    same records under two policies still pairs row for row.
    ``llm_calls_per_doc`` is a lower bound (see ``llm_calls_basis``).

    Two counters say how much of a result rests on weaker evidence than a real observation:
    ``auto_archived_schema_unknown`` (auto-archives whose accepted extraction attempt has
    ``schema_valid`` None: routing, and so replay, blocks only on an explicit False) and
    ``replayed_with_confidence_only_handoff_diff`` (routes that reused an extraction run for a
    collapsed candidate whose handoff confidence differed; see ``schemas/run_record.py``).
    """
    p = ObjectiveParams.from_mapping(objective_params)
    accept_target(p.c_human, p.c_error)  # finite and c_error > c_human > 0, before anything is scored
    if not (isinstance(p.review_cost_usd, (int, float)) and math.isfinite(p.review_cost_usd) and p.review_cost_usd > 0):
        raise ValueError(f"review_cost_usd must be a finite number > 0, got {p.review_cost_usd!r}")
    if not records:
        raise ValueError("no records to evaluate")
    n = len(records)
    counts = {AUTO: 0, HUMAN: 0, UNOBSERVED: 0}
    grades = {"correct": 0, "wrong": 0, "unscored": 0}
    per_class: dict[str, dict[str, Any]] = {}
    per_doc_cost: dict[str, float] = {}
    total_cost = 0.0
    total_usd = 0.0
    total_calls = 0
    unobserved_reasons: dict[str, int] = {}
    schema_unknown_auto = 0
    handoff_diff = 0
    seen_ids: dict[str, int] = {}

    with policy_override(policy):
        used_policy = current_policy()
        with _replay_env(), _merge_rule(merge_rule):
            for rec in records:
                out = _replay(rec, p.oracle)
                counts[out.outcome] += 1
                total_usd += out.llm_cost_usd
                total_calls += out.llm_calls
                cls = out.final_class or (rec.sorter[0].doc_type if rec.sorter else None) or "(none)"
                row = per_class.setdefault(
                    cls,
                    {"n": 0, AUTO: 0, "correct": 0, "wrong": 0, "unscored": 0, HUMAN: 0, UNOBSERVED: 0},
                )
                row["n"] += 1
                row[out.outcome] += 1
                handoff_diff += out.handoff_confidence_diff
                if out.outcome == AUTO:
                    schema_unknown_auto += out.schema_unknown
                    grade = grade_auto(rec, out, p)
                    grades[grade] += 1
                    row[grade] += 1
                    doc_cost = 0.0 if grade == "correct" else p.c_error
                else:
                    doc_cost = p.c_human
                    if out.outcome == UNOBSERVED:
                        key = out.reasons[0].split(":")[0] if out.reasons else "unknown"
                        unobserved_reasons[key] = unobserved_reasons.get(key, 0) + 1
                doc_cost += out.llm_cost_usd / p.review_cost_usd * p.c_human
                # Concatenated runs repeat doc_ids: keep one cost per scored row (the 2nd
                # occurrence is "<id>#2", ...) so it neither overwrites nor drops out of a pairing.
                # A real doc_id may itself end in "#<n>" and occupy a generated key, so a key that
                # is taken walks further up until it is free: the first occurrence keeps the bare
                # id, and a later occurrence shifts (a record literally named "d#2" keeps "d#2";
                # a second "d" then lands on "d#3", not on the "d#2" row's cost).
                seen_ids[rec.doc_id] = seen_ids.get(rec.doc_id, 0) + 1
                occurrence = seen_ids[rec.doc_id]
                key = rec.doc_id if occurrence == 1 else f"{rec.doc_id}#{occurrence}"
                while key in per_doc_cost:
                    occurrence += 1
                    seen_ids[rec.doc_id] = occurrence
                    key = f"{rec.doc_id}#{occurrence}"
                per_doc_cost[key] = doc_cost
                total_cost += doc_cost

    n_auto = counts[AUTO]
    wrong_like = grades["wrong"] + grades["unscored"]  # unknown is charged as wrong
    for row in per_class.values():
        bad = row["wrong"] + row["unscored"]
        row["wrong_rate"] = bad / row[AUTO] if row[AUTO] else None
        row["wrong_cp_upper"] = _cp_upper(bad, row[AUTO], p.confidence)
    score = 1.0 - (total_cost / n) / p.c_human

    guards: list[dict[str, Any]] = []
    cp = _cp_upper(wrong_like, n_auto, p.confidence)
    guards.append(
        {
            "id": "G1",
            "name": "overall wrong-auto upper bound",
            "pass": cp is None or cp <= p.risk,
            "value": cp,
            "limit": p.risk,
            "detail": (
                f"{wrong_like}/{n_auto} wrong auto-archives, Clopper-Pearson "
                f"{p.confidence:.0%} upper bound "
                + ("n/a (nothing auto-archived)" if cp is None else f"{cp:.4f}")
                + f" <= {p.risk:.4f}"
            ),
        }
    )
    offenders = {
        cls: row["wrong_rate"]
        for cls, row in sorted(per_class.items())
        if row[AUTO] >= p.class_min_auto and row["wrong_rate"] > p.class_wrong_rate_max
    }
    checked = sorted(c for c, r in per_class.items() if r[AUTO] >= p.class_min_auto)
    guards.append(
        {
            "id": "G2",
            "name": "per-class wrong-auto rate",
            "pass": not offenders,
            "value": offenders or None,
            "limit": p.class_wrong_rate_max,
            "detail": (
                f"classes with >= {p.class_min_auto} auto-archives: {checked or 'none'}; "
                + (
                    "over limit: " + ", ".join(f"{c}={v:.3f}" for c, v in offenders.items())
                    if offenders
                    else f"all <= {p.class_wrong_rate_max:.3f}"
                )
            ),
        }
    )
    unobs_share = counts[UNOBSERVED] / n
    guards.append(
        {
            "id": "G3",
            "name": "unobserved share",
            "pass": unobs_share <= p.unobserved_max,
            "value": unobs_share,
            "limit": p.unobserved_max,
            "detail": f"{counts[UNOBSERVED]}/{n} unobserved = {unobs_share:.4f} <= {p.unobserved_max:.4f}",
        }
    )
    usd_per_doc = total_usd / n
    if p.cost_budget_usd is None:
        guards.append(
            {"id": "G4", "name": "LLM USD per doc", "pass": True, "skipped": True, "value": usd_per_doc,
             "limit": None, "detail": f"no budget set (mean ${usd_per_doc:.4f}/doc)"}
        )
    else:
        guards.append(
            {"id": "G4", "name": "LLM USD per doc", "pass": usd_per_doc <= p.cost_budget_usd,
             "value": usd_per_doc, "limit": p.cost_budget_usd,
             "detail": f"mean ${usd_per_doc:.4f}/doc <= ${p.cost_budget_usd:.4f}"}
        )

    return {
        "n_docs": n,
        "params": asdict(p),
        "policy": used_policy,
        "oracle": p.oracle,
        "merge_rule": merge_rule,
        "outcomes": counts,
        "stp_rate": n_auto / n,
        "review_rate": counts[HUMAN] / n,
        "unobserved_rate": unobs_share,
        "unobserved_reasons": unobserved_reasons,
        # Auto-archives whose accepted extraction attempt has no recorded schema verdict
        # (``schema_valid`` None). Routing blocks only on an explicit False, so each of these
        # passed the schema gate on missing evidence.
        "auto_archived_schema_unknown": schema_unknown_auto,
        # Routes that reused the extraction of a collapsed candidate whose handoff confidence
        # differed (same class and subclass), which the sweep does not count against
        # ``observed.full_information``. Reported next to G3 so the approximation is measurable.
        "replayed_with_confidence_only_handoff_diff": handoff_diff,
        "auto": {
            "n": n_auto,
            "correct": grades["correct"],
            "wrong": grades["wrong"],
            "unscored": grades["unscored"],
            "wrong_rate": wrong_like / n_auto if n_auto else None,
            "wrong_cp_upper": cp,
            "wrong_rate_of_docs": wrong_like / n,
        },
        "per_class": per_class,
        "mean_cost_units": total_cost / n,
        "llm_usd_per_doc": usd_per_doc,
        "llm_calls_per_doc": total_calls / n,
        # one call per visited node (extraction: one per chunk). A judge result makes two
        # calls and sliding windows add more, so the count is a floor, not a measurement.
        "llm_calls_basis": "lower_bound: one call per visited node plus extraction chunks",
        "sorter_calibration": sorter_calibration(records),
        "score": score,
        "status": "ok" if all(g["pass"] for g in guards) else "guard_fail",
        "guards": guards,
        "per_doc_cost": per_doc_cost,
    }


def paired_delta(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any], c_human: float = 1.0, *, seed: int = 42
) -> dict[str, Any] | None:
    """Paired bootstrap of the per-document utility difference (candidate - baseline).

    Per-doc utility is ``1 - cost / C_h``. Uses the Dojo's
    ``delta_significance`` against an all-zero sample, which makes its
    two-sample mean-difference bootstrap exactly a one-sample bootstrap of the
    paired differences. ``None`` when fewer than 2 shared documents.
    """
    from llm_dojo_scoring.analysis.bootstrap import delta_significance

    shared = [d for d in candidate["per_doc_cost"] if d in baseline["per_doc_cost"]]
    diffs = [
        (1.0 - candidate["per_doc_cost"][d] / c_human) - (1.0 - baseline["per_doc_cost"][d] / c_human)
        for d in shared
    ]
    if len(diffs) < 2:
        return None
    result = delta_significance([0.0] * len(diffs), diffs, seed=seed)
    if result is not None:
        result["n_paired"] = len(diffs)
    return result


def result_for_json(result: Mapping[str, Any], *, drop_per_doc: bool = True) -> dict[str, Any]:
    """A JSON-safe copy (NaN -> null; optionally without the per-document costs)."""
    def clean(value: Any) -> Any:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value

    out = clean(dict(result))
    if drop_per_doc:
        out.pop("per_doc_cost", None)
    json.dumps(out)  # fail loudly on anything non-serializable
    return out
