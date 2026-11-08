#!/usr/bin/env python3
"""Full-information calibration sweep: one ``RunRecord`` JSON line per document.

Why this exists: the live graph keeps only each stage's LAST confidence (the
reviewer even overwrites ``classification_confidence``), ``retry_*`` nodes write
no audit, and pilot runs put ``ground_truth`` into state, which changes routing
(``pipeline.reconsideration.class_misses_ground_truth`` / ``coverage_below_floor``).
A confidence threshold cannot be calibrated from such runs. This sweep runs the
REAL node functions of ``graph/build_graph.py`` in a fixed order, whatever the
router would have done, so every sorter, reviewer, extraction and judge branch
is observed for every document (arbiter and boss branches never are; see below):

    intake_node
      -> classify_node (sorter attempt 1)
      -> retry_classify_node (sorter attempt 2, always)
      -> review_classify_node (reviewer, always)
      -> per candidate class (sorter attempt 1 / reviewer / sorter attempt 2,
         distinct classes, at most ``--max-extraction-candidates``):
           extract_node, then retry_extract_node ``retry_max`` more times,
           judge_verify_node on attempt 1 (``--judge-attempts all``: every attempt)

Gold labels never enter node state (``run.gt_in_state`` is False); they are
attached to the record afterwards and used only for offline Dojo scoring.
Boss / arbiter (classification_arbiter and the Lane B arbiter) / human review /
report / archive nodes are never run, so ``observed.full_information`` covers the
sorter, reviewer, extraction and judge branches ONLY. A route that needs an arbiter
ruling or the boss stays unobserved on every record, ``full_information`` or not, and
policy replay reports it as ``unobserved`` (``diagnostics.unobserved_branches`` names
them, plus ``judge_on_retry_attempts`` unless ``--judge-attempts all``); see
``schemas/run_record.py``.

Usage:
    PYTHONPATH=src python src/scripts/run_calibration_sweep.py \\
        --dataset mailroom-fixtures --mock --out /tmp/records.jsonl
    PYTHONPATH=src python src/scripts/run_calibration_sweep.py \\
        --hf-split train --per-class 20 --seed 7 --mock --out records.jsonl
    PYTHONPATH=src python src/scripts/run_calibration_sweep.py \\
        --manifest samples/manifest.csv --mock --out records.jsonl --resume
    # Spends money; refused unless the operator sets MAILROOM_ALLOW_REAL=1:
    MAILROOM_ALLOW_REAL=1 PYTHONPATH=src python src/scripts/run_calibration_sweep.py \\
        --hf-split test --per-class 20 --real --out records.jsonl

Fidelity gaps against the live graph are listed in ``KNOWN_GAPS`` and copied into
every record's ``diagnostics.known_gaps``.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from schemas.run_record import (  # noqa: E402
    ExtractionAttempt,
    Gold,
    JudgeObservation,
    Observed,
    ReviewerObservation,
    RunInfo,
    RunRecord,
    SorterAttempt,
)

SWEEP_VERSION = 1
FIXTURES_DATASET = "mailroom-fixtures"
ALLOW_REAL_ENV = "MAILROOM_ALLOW_REAL"
SORTER_ATTEMPTS = 2  # classify + one retry_classify: all the live graph ever runs
DEFAULT_TRANSIENT_RETRIES = 2  # mirrors graph.routing._TRANSIENT_MAX_RETRIES
# Keys that would let routing see the answer. Nodes must never receive them.
GOLD_STATE_KEYS = ("ground_truth", "expected_doc_class", "expected_doc_type", "expected_stage",
                   "expected_fields", "expected")

KNOWN_GAPS = [
    "no ground_truth in state: the live graph's GT-aware routing arms (class_misses_ground_truth, "
    "coverage_below_floor) are deliberately not reproduced",
    "per-agent outcome memory is disabled (documents are independent and --resume is stable); live "
    "retry prompts may carry a 'what similar cases taught us' appendix",
    "boss / matter-conflict path not observed: each document gets its own matter, so "
    "conflict_detected is always False",
    "judge runs on extraction attempt 1 (or every attempt with --judge-attempts all) with its "
    "gate forced open; the live graph judges whichever attempt the router stopped on",
    "extraction handoff context carries the classification confidence of the candidate's source "
    "stage; candidates sharing a class are collapsed onto the first source",
    "reviewer and judge truncate long inputs exactly as live (see diagnostics.downstream_truncation)",
    "arbiter nodes (classification_arbiter, Lane B arbiter) are never run: routes that need an arbiter "
    "ruling stay unobserved even when full_information is true",
    "candidates that share a class are extracted once, on the first source; when their subclass differs "
    "the record is not full_information, a confidence-only difference (the handoff prints it to 2 "
    "decimals) is listed in diagnostics.extraction_candidates_collapsed",
]
# Branches the sweep never runs; replay reports a route that needs one as unobserved.
UNOBSERVED_BRANCHES = ["classification_arbiter", "arbiter", "boss"]


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------


@dataclass
class Doc:
    """One document to sweep plus its gold labels (kept OUT of node state)."""

    doc_id: str
    filename: str
    path: Path | None = None
    text: str | None = None  # materialised to a file by the worker when set
    gold_class: str | None = None
    gold_subclass: str | None = None
    expected_stage: str | None = None
    review_expected: bool | None = None
    expected_fields: dict | None = None
    content_sha256: str | None = None
    gold_meta: dict = field(default_factory=dict)


@dataclass
class SweepContext:
    run: RunInfo
    base_dir: Path
    mock: bool
    judge_attempts: str = "first"
    max_candidates: int = 2
    concurrency: int = 1
    thresholds: dict = field(default_factory=dict)
    field_scoring_embedding: bool = False


# --------------------------------------------------------------------------------------
# Thread-local capture: raw (pre-guard) values the node updates do not carry
# --------------------------------------------------------------------------------------

_TLS = threading.local()


class _Capture:
    def __init__(self) -> None:
        self.sorter: list[dict] = []
        self.reviewer: list[dict] = []
        self.extract_conf: list[float | None] = []
        self.agents: list[Any] = []
        self.raw_chars: int | None = None


def _cap() -> _Capture | None:
    return getattr(_TLS, "cap", None)


@contextlib.contextmanager
def _capturing() -> Iterator[_Capture]:
    cap = _Capture()
    prev = getattr(_TLS, "cap", None)
    _TLS.cap = cap
    try:
        yield cap
    finally:
        _TLS.cap = prev


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@contextlib.contextmanager
def installed_hooks() -> Iterator[None]:
    """Non-invasive wrappers around the live seams. Installed once per sweep.

    * ``SorterAgent.classify_json`` / ``SorterReviewerAgent.review``: the model's
      own confidence, before ``apply_classification_guard`` clamps it.
    * ``_enrich_contract_result``: the extraction's raw ``confidence`` (the node
      pops it from the result and replaces it with the post-guard value).
    * ``_instantiate_specialist``: the agent, for ``_last_n_chunks``.
    * ``_read_file_text``: the length of the text exactly as ingested.
    * ``judge_should_run``: forced open so the judge always runs.
    * ``langchain_agents.memory``: outcome memory off (documents stay independent).
    """
    import graph.build_graph as bg
    import langchain_agents.memory as memory
    from agents.sorter import SorterAgent
    from agents.sorter_reviewer import SorterReviewerAgent

    orig_classify = SorterAgent.classify_json
    orig_review = SorterReviewerAgent.review
    orig_enrich = bg._enrich_contract_result
    orig_instantiate = bg._instantiate_specialist
    orig_read = bg._read_file_text

    def classify_json(self, *args, **kwargs):
        out = orig_classify(self, *args, **kwargs)
        cap = _cap()
        if cap is not None and isinstance(out, dict):
            cap.sorter.append({
                "confidence": _num(out.get("confidence")),
                "doc_type": out.get("doc_type"),
                "windows": getattr(self, "_last_windows", None),
            })
        return out

    def review(self, *args, **kwargs):
        out = orig_review(self, *args, **kwargs)
        cap = _cap()
        if cap is not None and isinstance(out, dict):
            cap.reviewer.append({"confidence": _num(out.get("confidence")), "doc_type": out.get("doc_type")})
        return out

    def enrich(result, state):
        cap = _cap()
        if cap is not None and isinstance(result, dict):
            cap.extract_conf.append(_num(result.get("confidence")))
        return orig_enrich(result, state)

    def instantiate(agent_fn, handoff_context):
        agent = orig_instantiate(agent_fn, handoff_context)
        cap = _cap()
        if cap is not None:
            cap.agents.append(agent)
        return agent

    def read_file_text(file_path):
        text, ok = orig_read(file_path)
        cap = _cap()
        if cap is not None:
            cap.raw_chars = len(text)
        return text, ok

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(SorterAgent, "classify_json", classify_json))
        stack.enter_context(patch.object(SorterReviewerAgent, "review", review))
        stack.enter_context(patch.object(bg, "_enrich_contract_result", enrich))
        stack.enter_context(patch.object(bg, "_instantiate_specialist", instantiate))
        stack.enter_context(patch.object(bg, "_read_file_text", read_file_text))
        stack.enter_context(patch.object(bg, "judge_should_run", lambda state: True))
        stack.enter_context(patch.object(memory, "recent_context", lambda *a, **k: ""))
        stack.enter_context(patch.object(memory, "record_outcome", lambda *a, **k: False))
        yield


# --------------------------------------------------------------------------------------
# Mock LLM: the run_pilot fake, extended to the lanes the live mock never reaches
# --------------------------------------------------------------------------------------


def mock_expectation(doc: Doc) -> dict:
    """Same per-sample expectation ``run_pilot.run_sample`` builds (gold class, 0.95-0.99).

    ``subclass`` is extra: a valid catalog token for the class, so the classification
    guard does not clamp every non-contract mock answer (``doc_subclass_missing``).
    """
    doc_type = doc.gold_class or "contract"
    subclass = None
    try:
        from langchain_agents.doc_inventories import sorter_subclass_catalog

        catalog = list(sorter_subclass_catalog(doc_type))
        subclass = doc.gold_subclass if doc.gold_subclass in catalog else (catalog[0] if catalog else None)
    except Exception:
        pass
    return {
        "doc_type": doc_type,
        "conf": 0.95 + (sum(doc.doc_id.encode()) % 5) / 100,
        "subclass": "other" if doc_type == "contract" else subclass,
    }


def _mock_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=40),
        model="mock-model",
    )


def _special_mock_content(user_text: str, expect: dict) -> str | None:
    """Canned JSON for the reviewer and judge prompts, which ``run_pilot._fake_client`` ignores."""
    if "CLASSIFY THIS DOCUMENT" in user_text:
        is_contract = expect["doc_type"] == "contract"
        return json.dumps({
            "doc_type": expect["doc_type"],
            "contract_subtype": "other" if is_contract else None,
            "doc_subclass": expect.get("subclass"),
            "confidence": expect["conf"],
            "reasoning": "mock",
        })
    if "Evaluate extraction completeness" in user_text:
        return json.dumps({"completeness": 1.0, "completeness_label": "complete", "reasoning": "mock"})
    if "Audit the factual accuracy" in user_text:
        return json.dumps({"extraction_correctness": 1.0, "extraction_correctness_label": "accurate",
                           "reasoning": "mock"})
    return None


@contextlib.contextmanager
def mock_llm() -> Iterator[None]:
    """Patch every LLM entry point with the deterministic fake (no network, thread-safe).

    ``scripts.run_pilot._fake_client`` / ``_make_mock_langchain_llm`` are the fakes
    ``run_experiment --mock`` uses; the expectation is read per thread so documents
    can run concurrently.
    """
    import scripts.run_pilot as rp
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent

    def get_llm(agent_name):
        expect = _TLS.expect
        base_create = rp._fake_client(expect).chat.completions.create

        def create(**kwargs):
            last = (kwargs.get("messages") or [{}])[-1]
            text = last.get("content", "") if isinstance(last, dict) else ""
            special = _special_mock_content(text if isinstance(text, str) else "", expect)
            return _mock_response(special) if special is not None else base_create(**kwargs)

        client = MagicMock()
        client.chat.completions.create = create
        return client, "mock-model"

    def langchain_llm(self):
        fake = rp._make_mock_langchain_llm(_TLS.expect)(self)
        fake.classification["doc_subclass"] = _TLS.expect.get("subclass")
        return fake

    with patch("llm.client.get_llm", side_effect=get_llm), \
         patch("agents.base.get_llm", side_effect=get_llm), \
         patch.object(_LangChainBaseAgent, "llm", new=langchain_llm):
        yield


# --------------------------------------------------------------------------------------
# Node runner
# --------------------------------------------------------------------------------------


@dataclass
class Step:
    update: dict
    state: dict  # state merged with the (final) update
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    transient_error: bool
    transient_retries: int
    cap: _Capture


def _assert_no_gold(state: dict, node: str) -> None:
    leaked = [k for k in GOLD_STATE_KEYS if k in state]
    if leaked:
        raise RuntimeError(f"gold labels leaked into {node} state: {leaked}")


def run_node(name: str, state: dict, *, transient_retries: int | None = None) -> Step:
    """Run ``graph.build_graph.<name>`` once, retrying transient provider errors like the router.

    Tokens, cost and latency come from ``pipeline.limits`` (the pipeline's own meter,
    fed by ``BaseAgent._call_llm`` and the vendored LangChain agents) and are summed
    over the transient retries. Node functions are looked up at call time.
    """
    import graph.build_graph as bg
    from pipeline import limits

    if transient_retries is None:
        transient_retries = DEFAULT_TRANSIENT_RETRIES
    fn = getattr(bg, name)
    pt = ct = 0
    cost = latency = 0.0
    retries = 0
    while True:
        _assert_no_gold(state, name)
        with _capturing() as cap:
            limits.reset_run_usage()
            started = time.perf_counter()
            update = fn(state)
            latency += time.perf_counter() - started
            usage = limits.usage_summary()
            cost += limits.estimate_cost()
        pt += usage["prompt_tokens"]
        ct += usage["completion_tokens"]
        merged = {**state, **update}
        if update.get("transient_error") and retries < transient_retries:
            retries += 1
            state = merged
            continue
        return Step(update, merged, latency, pt, ct, round(cost, 6),
                    bool(update.get("transient_error")), retries, cap)


def build_initial_state(file_path: Path, matter_id: str) -> dict:
    """The state ``run_pipeline`` hands the graph, minus ``ground_truth`` and tracing ids."""
    return {
        "doc_id": "",
        "matter_id": matter_id,
        "original_filename": file_path.name,
        "stage": "inbox",
        "doc_type": None,
        "classification_confidence": None,
        "classification_attempts": 0,
        "extracted_data": None,
        "extraction_confidence": None,
        "extraction_attempts": 0,
        "trace_id": None,
        "escalation_reason": None,
        "review_decision": None,
        "retry_count": 0,
        "conflict_detected": False,
        "boss_conflict_fingerprint": None,
        "file_path": str(file_path),
        "doc_text": "",
        "doc_pages": [],
        "error_message": None,
        "messages": [],
        "transient_error": False,
        "transient_retries_classify": 0,
        "transient_retries_extract": 0,
        "run_attempt": 0,
    }


# --------------------------------------------------------------------------------------
# One document
# --------------------------------------------------------------------------------------


def _subclass_of(update: dict) -> str | None:
    return update.get("doc_subclass") or update.get("contract_subtype")


def _sorter_attempt(n: int, step: Step) -> SorterAttempt:
    u = step.update
    raw = step.cap.sorter[-1]["confidence"] if step.cap.sorter else None
    ok = not step.transient_error
    return SorterAttempt(
        attempt=n,
        doc_type=u.get("doc_type") if ok else None,
        doc_subclass=_subclass_of(u) if ok else None,
        confidence_raw=raw,
        confidence=_num(u.get("classification_confidence")) if ok else None,
        guardrail_issues=[str(i) for i in (u.get("classification_guardrail") or [])],
        transient_error=step.transient_error,
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost_usd=step.cost_usd,
        latency_s=round(step.latency_s, 4),
    )


def _score_extraction(doc: Doc, doc_type: str, extracted: Any, text: str, diag: dict) -> tuple[float | None, float | None]:
    """Offline Dojo score of one extraction vs gold fields (never seen by routing)."""
    if not doc.expected_fields or not doc.gold_class or not isinstance(extracted, dict):
        return None, None
    from observability.classification_scoring import classes_match

    if not classes_match(doc.gold_class, doc_type):
        diag.setdefault("unscored_class_mismatch", []).append(doc_type)
        return None, None
    try:
        from llm_dojo_scoring import get_field_types
        from observability.suite_scoring import score_with_suite

        result, _extras = score_with_suite(
            doc_type, extracted, doc.expected_fields,
            field_types=get_field_types(doc_type), doc_text=text,
        )
        support = result.audit_support or {}
        return _num(result.overall_score), _num(support.get("hallucination_rate"))
    except Exception as exc:  # scoring must never lose the record
        diag.setdefault("scoring_errors", []).append(f"{type(exc).__name__}: {str(exc)[:160]}")
        return None, None


def _extraction_attempt(doc: Doc, n: int, doc_type: str, step: Step, text: str, diag: dict) -> ExtractionAttempt:
    from observability.scores import validate_extraction
    from pipeline.reconsideration import extraction_is_hollow

    u = step.update
    extracted = u.get("extracted_data") if not step.transient_error else None
    raw = step.cap.extract_conf[-1] if step.cap.extract_conf else None
    n_chunks = 1
    failed_chunks = 0
    for agent in step.cap.agents:  # both specialist implementations keep the counts on the instance
        n_chunks = max(n_chunks, int(getattr(agent, "_last_n_chunks", 1) or 1))
        failed_chunks = max(failed_chunks, int(getattr(agent, "_last_failed_chunks", 0) or 0))
    schema_valid = None
    if extracted:
        try:
            schema_valid = validate_extraction(doc_type, extracted).get("schema_valid")
        except Exception:
            schema_valid = None
    score, halluc = (None, None) if step.transient_error else _score_extraction(doc, doc_type, extracted, text, diag)
    return ExtractionAttempt(
        attempt=n,
        doc_type=doc_type,
        confidence_raw=raw,
        confidence=_num(u.get("extraction_confidence")) if not step.transient_error else None,
        schema_valid=schema_valid if isinstance(schema_valid, bool) else None,
        hollow=bool(extracted is not None and extraction_is_hollow(extracted)),
        guardrail_issues=[str(i) for i in (u.get("extraction_guardrail") or [])],
        conflict_detected=bool(u.get("conflict_detected")),
        n_chunks=n_chunks,
        failed_chunks=failed_chunks,
        extraction_score=score,
        hallucination_rate=halluc,
        transient_error=step.transient_error,
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost_usd=step.cost_usd,
        latency_s=round(step.latency_s, 4),
    )


def _judge_observation(step: Step, *, doc_type: str, attempt: int) -> JudgeObservation:
    u = step.update
    verdict = u.get("judge_verdict")
    ran = (not step.transient_error) and verdict in ("complete", "partial", "incomplete", "judge_error")
    return JudgeObservation(
        ran=bool(ran),
        on_attempt=attempt,
        doc_type=doc_type,
        verdict=verdict if ran else None,
        score=_num(u.get("judge_score")),
        source_verified=bool(u.get("judge_source_verified")),
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost_usd=step.cost_usd,
        latency_s=round(step.latency_s, 4),
    )


def _reviewer_observation(state_in: dict, step: Step, mode: str) -> ReviewerObservation:
    u = step.update
    if mode == "decision_only":
        ran = u.get("reviewer_model") is not None and not step.transient_error
    else:
        verdict = u.get("review_verdict")
        ran = bool(verdict) and verdict != "reviewer_error" and not step.transient_error
    return ReviewerObservation(
        ran=bool(ran),
        mode=mode,  # type: ignore[arg-type]
        reviewer_doc_type=u.get("reviewer_doc_type") if ran else None,
        reviewer_doc_subclass=u.get("reviewer_doc_subclass") if ran else None,
        reviewer_confidence=_num(u.get("reviewer_confidence")) if ran else None,
        reviewer_verdict=u.get("review_verdict") if ran else None,
        review_route=u.get("review_route") if (ran and mode == "decision_only") else None,
        causes=[str(c) for c in (u.get("reviewer_causes") or [])],
        transient_error=step.transient_error,
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost_usd=step.cost_usd,
        latency_s=round(step.latency_s, 4),
    )


def _fresh_extraction_state(cand_state: dict) -> dict:
    drop = {"extracted_data", "extraction_confidence", "extraction_guardrail", "conflict_details",
            "extraction_gate", "judge_verdict", "judge_score", "judge_findings", "judge_source_verified"}
    out = {k: v for k, v in cand_state.items() if k not in drop}
    out.update(extraction_attempts=0, extracted_data=None, extraction_confidence=None,
               conflict_detected=False, transient_error=False, transient_retries_extract=0,
               transient_retries_retry_extract=0, transient_retries_judge_verify=0, judge_pass_count=0)
    return out


def _handoff_conf(st: dict) -> str | None:
    """The classification confidence exactly as the extraction handoff prints it (``confidence=0.93``)."""
    conf = _num(st.get("classification_confidence"))
    return None if conf is None else f"{conf:.2f}"


def _pick_candidates(
    states: list[tuple[str, dict]], cap: int
) -> tuple[list[tuple[str, dict]], list[str], list[dict]]:
    """Distinct extractable classes in priority order.

    Returns ``(kept, dropped classes, collapsed)``. ``collapsed`` lists every later candidate
    that shares a class with an earlier one but would hand the specialist a different
    subclass or confidence; its extraction is NOT run (attempts are keyed by class), so it
    is reported rather than silently treated as the same extraction.
    """
    from pipeline.config import is_extractable_doc_type

    first: dict[str, tuple[str, dict]] = {}
    kept: list[tuple[str, dict]] = []
    dropped: list[str] = []
    collapsed: list[dict] = []
    for source, st in states:
        cls = st.get("doc_type")
        if not isinstance(cls, str) or not is_extractable_doc_type(cls):
            continue
        if cls in first:
            base_source, base = first[cls]
            same_sub = _subclass_of(base) == _subclass_of(st)
            same_conf = _handoff_conf(base) == _handoff_conf(st)
            if not (same_sub and same_conf):
                collapsed.append({
                    "doc_type": cls, "source": source, "collapsed_onto": base_source,
                    "doc_subclass": _subclass_of(st), "handoff_confidence": _handoff_conf(st),
                    "extracted_doc_subclass": _subclass_of(base), "extracted_handoff_confidence": _handoff_conf(base),
                    "subclass_differs": not same_sub,
                })
            continue
        first[cls] = (source, st)
        if len(kept) < cap:
            kept.append((source, st))
        else:
            dropped.append(cls)
    return kept, dropped, collapsed


def sweep_document(doc: Doc, ctx: SweepContext) -> RunRecord:
    """Run the sorter, reviewer, extraction and judge branches for one document and return its
    record (never raises). Arbiter and boss branches are not run."""
    import graph.build_graph as bg
    from pipeline.config import get_agent_config, get_confidence_thresholds, get_review_mode

    started = time.perf_counter()
    if ctx.mock:
        _TLS.expect = mock_expectation(doc)
    record = RunRecord(
        run=ctx.run,
        doc_id=doc.doc_id,
        filename=doc.filename,
        content_sha256=doc.content_sha256,
        gold=Gold(
            expected_doc_type=doc.gold_class or None,
            expected_doc_subclass=doc.gold_subclass or None,
            expected_stage=doc.expected_stage or None,
            review_expected=doc.review_expected,
        ),
        thresholds_in_force=ctx.thresholds,
    )
    diag: dict[str, Any] = {
        "sweep_version": SWEEP_VERSION,
        "judge_attempts": ctx.judge_attempts,
        "field_scoring_embedding": ctx.field_scoring_embedding,
        "known_gaps": list(KNOWN_GAPS),
        "unobserved_branches": list(UNOBSERVED_BRANCHES)
        + ([] if ctx.judge_attempts == "all" else ["judge_on_retry_attempts"]),
        "max_extraction_candidates": ctx.max_candidates,
        "gold_meta": doc.gold_meta,
        "node_errors": [],
    }
    record.diagnostics = diag
    complete = True
    transient: list[str] = []  # stages whose transient-error retries were exhausted
    try:
        path = _materialise(doc, ctx)
        state = build_initial_state(path, f"SWEEP-{doc.doc_id}")

        # -- intake -------------------------------------------------------------
        step = run_node("intake_node", state)
        state = step.state
        text = state.get("doc_text") or ""
        record.content_sha256 = record.content_sha256 or state.get("file_sha256") or None
        record.text_chars = step.cap.raw_chars if step.cap.raw_chars is not None else len(text)
        record.truncated = False
        diag["intake"] = {
            "chars": len(text), "messy": bool(state.get("intake_messy")), "changed": bool(state.get("intake_changed")),
            "llm_prep": bool(state.get("intake_prep")), "prompt_tokens": step.prompt_tokens,
            "completion_tokens": step.completion_tokens, "cost_usd": step.cost_usd,
            "latency_s": round(step.latency_s, 4),
        }
        if state.get("error_message"):
            diag["node_errors"].append(f"intake: {state['error_message']}")

        reviewer_cfg_chars = int(get_agent_config("sorter_reviewer").get("max_input_chars", 25000))
        from agents.judge import JUDGE_SOURCE_CHARS

        diag["downstream_truncation"] = {
            "reviewer": len(text) > reviewer_cfg_chars, "reviewer_max_input_chars": reviewer_cfg_chars,
            "judge": len(text) > JUDGE_SOURCE_CHARS, "judge_source_chars": JUDGE_SOURCE_CHARS,
        }

        if not bg._has_readable_text(state):
            # The live graph parks this immediately; there is nothing to calibrate on.
            step = run_node("classify_node", state)
            record.sorter.append(_sorter_attempt(1, step))
            record.error = "empty_or_unreadable_text"
            record.observed = Observed(sorter_attempts=1)
            return record

        # -- sorter attempts 1 and 2 -------------------------------------------
        step1 = run_node("classify_node", state)
        record.sorter.append(_sorter_attempt(1, step1))
        state1 = step1.state
        if step1.cap.sorter:
            diag["sorter_windows"] = step1.cap.sorter[0].get("windows")
        step2 = run_node("retry_classify_node", state1)
        record.sorter.append(_sorter_attempt(2, step2))
        state2 = step2.state
        for n, s in ((1, step1), (2, step2)):
            for key in ("error_message",):
                if s.update.get(key):
                    diag["node_errors"].append(f"sorter{n}: {s.update[key]}")
        complete &= not (step1.transient_error or step2.transient_error)
        transient += [f"sorter#{n}" for n, s in ((1, step1), (2, step2)) if s.transient_error]

        # -- reviewer (always) -------------------------------------------------
        mode = get_review_mode(state2.get("doc_type"))
        rstep = run_node("review_classify_node", state2)
        record.reviewer = _reviewer_observation(state2, rstep, mode)
        if rstep.cap.reviewer:
            diag["reviewer_confidence_raw"] = rstep.cap.reviewer[-1]["confidence"]
        if rstep.update.get("escalation_reason") and not record.reviewer.ran:
            diag["node_errors"].append(f"reviewer: {str(rstep.update['escalation_reason'])[:200]}")
        complete &= record.reviewer.ran
        if rstep.transient_error:
            transient.append("reviewer")

        # -- extraction candidates ---------------------------------------------
        cand_states: list[tuple[str, dict]] = []
        if not step1.transient_error:
            cand_states.append(("sorter_1", state1))
        if record.reviewer.ran and record.reviewer.reviewer_doc_type:
            cand_states.append(("reviewer", {
                **state2,
                "doc_type": rstep.update.get("reviewer_doc_type"),
                "contract_subtype": rstep.update.get("reviewer_contract_subtype"),
                "doc_subclass": rstep.update.get("reviewer_doc_subclass"),
                "classification_confidence": rstep.update.get("reviewer_confidence"),
            }))
        if not step2.transient_error:
            cand_states.append(("sorter_2", state2))
        kept, dropped, collapsed = _pick_candidates(cand_states, ctx.max_candidates)
        diag["extraction_candidates"] = [
            {"source": src, "doc_type": st["doc_type"], "doc_subclass": _subclass_of(st),
             "handoff_confidence": _num(st.get("classification_confidence"))}
            for src, st in kept
        ]
        if dropped:
            diag["extraction_candidates_dropped"] = dropped
            complete = False
        if collapsed:
            diag["extraction_candidates_collapsed"] = collapsed
            # A different subclass changes the handoff hint the specialist sees, so that extraction
            # was not observed. A confidence-only difference (printed to 2 decimals) is listed above
            # but does not demote the record: it is below the run-to-run noise of the model itself.
            if any(c["subclass_differs"] for c in collapsed):
                complete = False

        min_attempts: int | None = None
        all_judged = True
        first_judge = True
        for _src, cand in kept:
            cls = cand["doc_type"]
            n_ext = 1 + int(get_confidence_thresholds(cls).get("retry_max", 2))
            ext_state = _fresh_extraction_state(cand)
            done = 0
            for n in range(1, n_ext + 1):
                estep = run_node("extract_node" if n == 1 else "retry_extract_node", ext_state)
                record.extraction.append(_extraction_attempt(doc, n, cls, estep, text, diag))
                if estep.update.get("error_message") and not estep.transient_error:
                    diag["node_errors"].append(f"extract[{cls}#{n}]: {estep.update['error_message']}"[:240])
                complete &= not estep.transient_error
                if estep.transient_error:
                    transient.append(f"extract[{cls}#{n}]")
                    break  # the chain needs a real extraction to build on
                ext_state = estep.state
                done = n
                if n == 1 or ctx.judge_attempts == "all":
                    jstep = run_node("judge_verify_node", {**ext_state})
                    obs = _judge_observation(jstep, doc_type=cls, attempt=n)
                    if first_judge:
                        record.judge, first_judge = obs, False
                    else:
                        record.judges.append(obs)
                    all_judged &= obs.ran
                    if jstep.transient_error:
                        transient.append(f"judge[{cls}#{n}]")
            min_attempts = done if min_attempts is None else min(min_attempts, done)
            complete &= done == n_ext
        if kept:
            complete &= all_judged
        record.observed = Observed(
            sorter_attempts=len(record.sorter),
            reviewer=record.reviewer.ran,
            extraction_attempts=min_attempts or 0,
            judge=bool(kept) and all_judged,
            full_information=bool(complete and len(record.sorter) == SORTER_ATTEMPTS),
        )
        if transient:
            # Keep every usable observation, but make the document retryable (--resume
            # --retry-errors) and countable in the summary like any other errored record.
            diag["transient_exhausted"] = transient
            record.error = "transient_error_exhausted: " + ", ".join(transient)
    except Exception as exc:  # one bad document must not stop the sweep
        record.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        record.observed = Observed(
            sorter_attempts=len(record.sorter), reviewer=record.reviewer.ran,
            extraction_attempts=len(record.extraction), judge=record.judge.ran, full_information=False,
        )
    finally:
        diag["wall_s"] = round(time.perf_counter() - started, 3)
        diag["cost_usd_total"] = round(_record_cost(record) + float((diag.get("intake") or {}).get("cost_usd") or 0), 6)
    return record


def _record_cost(record: RunRecord) -> float:
    parts = [a.cost_usd for a in record.sorter] + [a.cost_usd for a in record.extraction]
    parts += [record.reviewer.cost_usd, record.judge.cost_usd] + [j.cost_usd for j in record.judges]
    return sum(parts)


def _safe_name(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name or "doc.txt"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return base if Path(base).suffix else base + ".txt"


def _materialise(doc: Doc, ctx: SweepContext) -> Path:
    """The file the intake node reads. Text docs are written UNTRUNCATED."""
    if doc.path is not None:
        if not doc.path.exists():
            raise FileNotFoundError(f"document file not found: {doc.path}")
        return doc.path
    if doc.text is None:
        raise ValueError(f"{doc.doc_id}: neither a path nor text")
    inputs = ctx.base_dir / "sweep_inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(doc.doc_id.encode()).hexdigest()[:8]
    target = inputs / f"{digest}-{_safe_name(doc.filename)}"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(doc.text)
    return target


# --------------------------------------------------------------------------------------
# Dataset loaders
# --------------------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Parquet cell -> plain Python: NaN / pandas NA -> None, numpy arrays and scalars -> lists / scalars.

    ``parse_hf_row`` tests cells with ``in (None, "")``, which raises on a numpy array.
    """
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.api.types.is_scalar(value) and pd.isna(value):
            return None
    except Exception:
        pass
    return value


def load_fixture_docs() -> tuple[list[Doc], dict]:
    from scripts import run_experiment as rx

    rows = rx.fixture_rows(None)
    items = rx.build_items(rows)
    docs = []
    for row, item in zip(rows, items):
        want = item["expected_output"]
        docs.append(Doc(
            doc_id=row["id"], filename=row["filename"], path=Path(row["_path"]),
            gold_class=want.get("expected_doc_class") or None,
            expected_stage=want.get("expected_stage") or None,
            expected_fields=want.get("expected_fields"),
        ))
    return docs, {"dataset_id": FIXTURES_DATASET, "dataset_revision": None, "split": "unsplit"}


def load_manifest_docs(manifest: Path, samples_dir: Path | None) -> tuple[list[Doc], dict]:
    from scripts import run_experiment as rx

    with manifest.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    items = rx.build_items(rows)
    root = samples_dir or manifest.parent
    docs = []
    for row, item in zip(rows, items):
        want = item["expected_output"]
        candidates = [root / (row.get("subdir") or "") / row["filename"], root / row["filename"]]
        path = next((c for c in candidates if c.exists()), candidates[0])
        docs.append(Doc(
            doc_id=row["id"], filename=row["filename"], path=path,
            gold_class=want.get("expected_doc_class") or None,
            expected_stage=want.get("expected_stage") or None,
            expected_fields=want.get("expected_fields"),
        ))
    return docs, {"dataset_id": f"manifest:{manifest.name}", "dataset_revision": None, "split": "unsplit"}


def load_hf_docs(split: str, per_class: int, seed: int) -> tuple[list[Doc], dict]:
    """Seeded per-class sample of the pinned corpus via ``pipeline.hf_corpus_loader.load_corpus``.

    Full document text, never truncated. Network only through the loader.
    """
    from pipeline import hf_corpus_loader as loader
    from scripts import run_hf_pilot as hp

    frame, prov = loader.load_corpus(split=split)
    by_class: dict[str, list[dict]] = {}
    for rec in frame.to_dict("records"):
        rec = {k: _clean(v) for k, v in rec.items()}
        sample = hp.parse_hf_row(rec)
        if not sample or len(sample["text"]) < 200:
            continue
        by_class.setdefault(sample["expected_hf_class"], []).append({"rec": rec, "sample": sample})
    rng = random.Random(seed)
    docs: list[Doc] = []
    for cls in sorted(by_class):
        pool = sorted(by_class[cls], key=lambda x: x["sample"]["filename"])
        for item in rng.sample(pool, min(per_class, len(pool))):
            rec, sample = item["rec"], item["sample"]
            fields = hp.expected_fields_for_sample(sample)
            meta = hp.expected_fields_meta(sample)
            review_expected = rec.get("review_expected")
            docs.append(Doc(
                doc_id=sample["filename"], filename=sample["filename"], text=sample["text"],
                gold_class=sample["expected_hf_class"], gold_subclass=sample.get("expected_subclass") or None,
                expected_stage=_clean(rec.get("expected_stage")) or None,
                review_expected=bool(review_expected) if review_expected is not None else None,
                expected_fields=fields or None,
                content_sha256=_clean(rec.get("content_sha256")),
                gold_meta={k: meta.get(k) for k in ("sources", "n_fields", "n_posthoc", "coverage", "specialist")},
            ))
    revision = (prov.get("revision_requested") or prov.get("hub_sha_tip")) if isinstance(prov, dict) else None
    return docs, {"dataset_id": loader.FULL_CORPUS_ID, "dataset_revision": revision, "split": split}


# --------------------------------------------------------------------------------------
# Provenance, resume, output
# --------------------------------------------------------------------------------------


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _taxonomy_sha256() -> str | None:
    try:
        from pipeline.config import CONFIG_PATH

        return hashlib.sha256(Path(CONFIG_PATH).read_bytes()).hexdigest()
    except Exception:
        return None


def thresholds_snapshot() -> dict:
    """Every threshold the router reads, as configured now (JSON-safe copy)."""
    from pipeline.config import load_config

    cfg = load_config()
    snap = {"confidence": cfg.get("confidence") or {}, "reviewer": cfg.get("reviewer") or {},
            "judge_verify": os.environ.get("MAILROOM_JUDGE_VERIFY", "on")}
    return json.loads(json.dumps(snap, default=str))


def read_existing_lines(out: Path) -> tuple[list[tuple[str, dict]], int]:
    """``(raw line, parsed row)`` for every usable line of an existing output; unusable ones are counted."""
    rows: list[tuple[str, dict]] = []
    bad = 0
    if not out.exists():
        return rows, bad
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict) and row.get("doc_id"):
            rows.append((line, row))
        else:
            bad += 1
    return rows, bad


def read_existing(out: Path) -> tuple[list[dict], int]:
    """Parsed lines of an existing output file; unparsable lines (a torn last write) are counted."""
    rows, bad = read_existing_lines(out)
    return [row for _line, row in rows], bad


def atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Replace ``path`` with ``lines`` (one JSON object each) or leave it exactly as it was.

    Written to a temporary file in the same directory (same filesystem, so ``os.replace`` is
    atomic), flushed and fsynced first: a crash or error mid-rewrite never costs the records
    already on disk.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(line + "\n" for line in lines))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _ends_with_newline(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def _file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def resume_mismatch(existing: list[dict], docs: list[Doc], current: dict) -> str | None:
    """Why ``existing`` records cannot be continued by this invocation, or ``None`` when they can.

    A resume skips documents by ``doc_id`` alone, so it is only sound when the saved records
    came from the same inputs and settings. ``current`` holds this run's ``mode``,
    ``dataset_id``, ``dataset_revision``, ``split``, ``taxonomy_sha256``, ``judge_attempts``,
    ``max_extraction_candidates`` and ``thresholds``. A saved record that lacks a recorded value
    cannot be validated and is refused too: start a new output file.
    """
    run_keys = ("mode", "dataset_id", "dataset_revision", "split", "taxonomy_sha256")
    diffs: dict[str, str] = {}
    for row in existing:
        run = row.get("run") or {}
        diag = row.get("diagnostics") or {}
        if run.get("gt_in_state"):
            diffs["gt_in_state"] = "saved records have ground truth in state"
        for key in run_keys:
            if key not in run:
                diffs.setdefault(key, "not recorded in the saved records")
            elif run[key] != current[key]:
                diffs.setdefault(key, f"saved {run[key]!r}, now {current[key]!r}")
        if "judge_attempts" not in diag:
            diffs.setdefault("judge_attempts", "not recorded in the saved records")
        elif diag["judge_attempts"] != current["judge_attempts"]:
            diffs.setdefault("judge_attempts", f"saved {diag['judge_attempts']!r}, now {current['judge_attempts']!r}")
        if "max_extraction_candidates" in diag and diag["max_extraction_candidates"] != current["max_extraction_candidates"]:
            diffs.setdefault("max_extraction_candidates",
                             f"saved {diag['max_extraction_candidates']!r}, now {current['max_extraction_candidates']!r}")
        if "thresholds_in_force" not in row:
            diffs.setdefault("thresholds_in_force", "not recorded in the saved records")
        elif row["thresholds_in_force"] != current["thresholds"]:
            diffs.setdefault("thresholds_in_force", "the confidence/reviewer thresholds differ")
    by_id = {d.doc_id: d for d in docs}
    changed = []
    for row in existing:
        doc = by_id.get(row.get("doc_id"))
        saved = row.get("content_sha256")
        if doc is None or not saved:
            continue
        now = doc.content_sha256 or (_file_sha256(doc.path) if doc.path is not None else None)
        if now and now != saved:
            changed.append(doc.doc_id)
    if changed:
        diffs["content_sha256"] = f"{len(changed)} document(s) changed since they were swept, e.g. {changed[:3]}"
    if not diffs:
        return None
    return "; ".join(f"{k}: {v}" for k, v in diffs.items())


class _Writer:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: RunRecord) -> None:
        line = record.model_dump_json()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


@contextlib.contextmanager
def sweep_environment(mock: bool, base_dir: Path | None) -> Iterator[Path]:
    """Hermetic env for the sweep, restored on exit. Creates a scratch base dir if none is set."""
    overrides = {
        "OBSERVABILITY_PROVIDER": "none",  # no hosted trace/score writes, in any mode
        "OBSERVABILITY_ENVIRONMENT": "mock" if mock else "calibration",
        "MAILROOM_LLM_CACHE": "0",
        "MAILROOM_RELATIONS": "0",
        "MAILROOM_RELATIONS_EMBEDDINGS": "0",
        "MAILROOM_WATCHER_STATUS": "0",
        "MAILROOM_WATCHDOG": "0",
        "MAILROOM_GMAIL_ENABLED": "0",
    }
    if mock:
        overrides.update({"MAILROOM_BERT_INTAKE": "0", "MAILROOM_LLM_INTAKE": "0", "MAILROOM_PROVIDER_HEALTH": "0",
                          "MAILROOM_VISION_ENABLED": "0"})
    created = None
    if base_dir is None:
        env_base = os.environ.get("MAILROOM_BASE_DIR")
        if env_base:
            base_dir = Path(env_base)
        else:
            created = Path(tempfile.mkdtemp(prefix="mailroom-sweep-"))
            base_dir = created
    overrides["MAILROOM_BASE_DIR"] = str(base_dir)
    overrides.setdefault("MAILROOM_FIELD_SCORING_EMBEDDING", os.environ.get("MAILROOM_FIELD_SCORING_EMBEDDING", "0"))
    # The sweep's intake writes catalog rows and audit entries: they must land in the scratch
    # base dir's SQLite file, never in a live database a DATABASE_URL points at.
    saved = {k: os.environ.get(k) for k in (*overrides, *ISOLATED_UNSET)}
    for key in ISOLATED_UNSET:
        os.environ.pop(key, None)
    os.environ.update(overrides)
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        yield base_dir
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)


ISOLATED_UNSET = ("DATABASE_URL",)


def estimate_calls(n_docs: int, retry_max: int = 2, candidates: int = 2, judge_attempts: str = "first") -> dict:
    """Lower-bound LLM calls: sliding windows / extraction chunks / LLM intake only add to it."""
    judge_passes = (1 + retry_max) if judge_attempts == "all" else 1
    per_doc = SORTER_ATTEMPTS + 1 + candidates * ((1 + retry_max) + 2 * judge_passes)
    return {"per_doc": per_doc, "docs": n_docs, "total": per_doc * n_docs}


def real_guard(env: dict | None = None) -> str | None:
    """None when a real run is allowed; otherwise the refusal reason."""
    env = os.environ if env is None else env
    if str(env.get(ALLOW_REAL_ENV, "")).strip() != "1":
        return f"refusing --real: set {ALLOW_REAL_ENV}=1 to confirm this run spends real LLM money"
    key = str(env.get("OPENROUTER_API_KEY", "")).strip()
    if not key or key in ("mock-key", "test-key-not-real"):
        return "refusing --real: OPENROUTER_API_KEY is not a real key"
    return None


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset", choices=[FIXTURES_DATASET], help="Committed plain-text test fixtures.")
    src.add_argument("--hf-split", choices=["train", "test"], help="Pinned HF corpus split (needs network).")
    src.add_argument("--manifest", type=Path, help="Pilot-style manifest CSV (id, filename, expected_doc_class, ...).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="Deterministic fake LLM (no API key, no network).")
    mode.add_argument("--real", action="store_true", help=f"Real LLM; refused unless {ALLOW_REAL_ENV}=1.")
    p.add_argument("--out", type=Path, required=True, help="Output JSONL (appended to).")
    p.add_argument("--per-class", type=int, default=5, help="HF: documents per class.")
    p.add_argument("--seed", type=int, default=0, help="HF: sampling seed.")
    p.add_argument("--samples-dir", type=Path, default=None, help="Manifest: root for sample files.")
    p.add_argument("--limit", type=int, default=None, help="Sweep only the first N documents.")
    p.add_argument("--resume", action="store_true", help="Skip doc_ids already in --out.")
    p.add_argument("--retry-errors", action="store_true", help="With --resume: drop errored records and re-run them.")
    p.add_argument("--concurrency", type=int, default=1, help="Documents in flight (thread pool).")
    p.add_argument("--judge-attempts", choices=["first", "all"], default="first")
    p.add_argument("--max-extraction-candidates", type=int, default=2)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mock = bool(args.mock)
    from pipeline.env import load_env

    explicit_base = os.environ.get("MAILROOM_BASE_DIR")
    load_env()  # before the real-run guard reads OPENROUTER_API_KEY; exported vars still win
    if explicit_base is None:
        # A base dir that only .env supplied is the live data dir (.env.example: ./data); the sweep
        # writes catalog rows and audit entries there, so it takes a scratch dir unless the operator
        # exported MAILROOM_BASE_DIR for this invocation.
        os.environ.pop("MAILROOM_BASE_DIR", None)
    if args.concurrency < 1 or args.max_extraction_candidates < 1:
        print("--concurrency and --max-extraction-candidates must be >= 1", file=sys.stderr)
        return 2
    if not mock:
        reason = real_guard()
        if reason:
            est = estimate_calls(1, candidates=args.max_extraction_candidates, judge_attempts=args.judge_attempts)
            print(f"{reason}\nestimated LLM calls: >= {est['per_doc']} per document (windows, chunks and LLM "
                  "intake add more)", file=sys.stderr)
            return 2

    with sweep_environment(mock, None) as base_dir:
        from pipeline.logging import setup_logging

        setup_logging(level=os.environ.get("LOG_LEVEL") or "WARNING")
        if os.environ.get("MAILROOM_FIELD_SCORING_EMBEDDING", "0").lower() in ("0", "false", "no"):
            with contextlib.suppress(Exception):
                from llm_dojo_scoring import configure

                configure(field_scoring__embedding_enabled=False)
        if args.hf_split:
            docs, ds = load_hf_docs(args.hf_split, args.per_class, args.seed)
        elif args.manifest:
            docs, ds = load_manifest_docs(args.manifest, args.samples_dir)
        else:
            docs, ds = load_fixture_docs()
        if args.limit is not None:
            docs = docs[: args.limit]
        ids = [d.doc_id for d in docs]
        if len(set(ids)) != len(ids):
            print("duplicate doc_ids in the selected documents", file=sys.stderr)
            return 2

        raw_existing, torn = read_existing_lines(args.out)
        existing = [row for _line, row in raw_existing]
        thresholds = thresholds_snapshot()
        run_id = existing[0].get("run", {}).get("run_id") if existing and args.resume else None
        if args.resume:
            current = {
                "mode": "mock" if mock else "real", "dataset_id": ds["dataset_id"],
                "dataset_revision": ds["dataset_revision"], "split": ds["split"],
                "taxonomy_sha256": _taxonomy_sha256(), "judge_attempts": args.judge_attempts,
                "max_extraction_candidates": args.max_extraction_candidates, "thresholds": thresholds,
            }
            why = resume_mismatch(existing, docs, current)
            if why:
                print(f"refusing --resume: {args.out} was not produced by this configuration ({why}); "
                      "use a new --out", file=sys.stderr)
                return 2
            keep = [(line, row) for line, row in raw_existing if not (args.retry_errors and row.get("error"))]
            if args.retry_errors or torn or not _ends_with_newline(args.out):
                # Drop errored (--retry-errors) and torn lines and restore the newline delimiter, so
                # the next append starts on its own line; atomic, the original survives a failure.
                atomic_write_lines(args.out, [line for line, _row in keep])
            existing = [row for _line, row in keep]
            done = {r["doc_id"] for r in existing}
            todo = [d for d in docs if d.doc_id not in done]
        elif existing:
            print(f"{args.out} already has {len(existing)} record(s); use --resume or a new path", file=sys.stderr)
            return 2
        else:
            todo = docs
        if torn:
            print(f"ignored {torn} unparsable line(s) in {args.out}", file=sys.stderr)

        run = RunInfo(
            run_id=run_id or time.strftime("sweep-%Y%m%dT%H%M%SZ", time.gmtime()),
            git_sha=_git_sha(), taxonomy_sha256=_taxonomy_sha256(), dojo_version=_dojo_version(),
            dataset_id=ds["dataset_id"], dataset_revision=ds["dataset_revision"], split=ds["split"],
            mode="mock" if mock else "real", gt_in_state=False,
        )
        ctx = SweepContext(
            run=run, base_dir=base_dir, mock=mock, judge_attempts=args.judge_attempts,
            max_candidates=args.max_extraction_candidates, concurrency=args.concurrency,
            thresholds=thresholds,
            field_scoring_embedding=os.environ.get("MAILROOM_FIELD_SCORING_EMBEDDING", "0").lower() not in ("0", "false", "no"),
        )
        if not mock:
            est = estimate_calls(len(todo), candidates=args.max_extraction_candidates, judge_attempts=args.judge_attempts)
            print(f"estimated LLM calls: >= {est['total']} ({est['per_doc']} per document x {est['docs']}); "
                  "windows, chunks and LLM intake add more", flush=True)

        print(f"sweeping {len(todo)} document(s) ({len(docs) - len(todo)} already done), mode={run.mode}, "
              f"concurrency={args.concurrency}, base_dir={base_dir}", flush=True)
        records = _run_all(todo, ctx, args.out, mock)

    return _summarise(records, len(docs) - len(todo))


def _dojo_version() -> str | None:
    try:
        from llm_dojo_scoring import __version__

        return str(__version__)
    except Exception:
        return None


def _run_all(todo: list[Doc], ctx: SweepContext, out: Path, mock: bool) -> list[RunRecord]:
    writer = _Writer(out)
    records: list[RunRecord] = []
    llm_cm = mock_llm() if mock else contextlib.nullcontext()
    try:
        with llm_cm, installed_hooks(), ThreadPoolExecutor(max_workers=ctx.concurrency) as pool:
            futures = {pool.submit(sweep_document, d, ctx): d for d in todo}
            try:
                for fut in as_completed(futures):
                    record = fut.result()
                    writer.write(record)
                    records.append(record)
                    flag = "ERR" if record.error else ("full" if record.observed.full_information else "partial")
                    print(f"[{len(records)}/{len(todo)}] {record.doc_id}: {flag}", flush=True)
            except KeyboardInterrupt:
                for fut in futures:
                    fut.cancel()
                print("interrupted: finished records are on disk; re-run with --resume", file=sys.stderr)
                raise
    finally:
        writer.close()
    return records


def _summarise(records: list[RunRecord], skipped: int) -> int:
    n = len(records)
    errors = sum(1 for r in records if r.error)
    full = sum(1 for r in records if r.observed.full_information)
    cost = sum(float(r.diagnostics.get("cost_usd_total") or 0) for r in records)
    summary = {
        "docs": n, "skipped_already_done": skipped, "errors": errors,
        "full_information": full, "full_information_share": round(full / n, 4) if n else None,
        "est_cost_usd": round(cost, 6),
    }
    print("SUMMARY " + json.dumps(summary, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
