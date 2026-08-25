#!/usr/bin/env python3
"""Pilot-test the pipeline against examples/samples/manifest.csv.

Feeds each sample through the real LangGraph pipeline, records per-document
outcomes (stage, doc_type, confidence, retries, LLM call count, wall time), and
scores them against the ground truth in the manifest. Two modes:

  --mock   deterministic fake LLM (returns the expected classification). No API
           key needed. Tests the pipeline *machinery* (PDF ingestion/transcribe,
           routing, retries, archiving, timing) reproducibly — not LLM accuracy.
  --real   real LLM via get_llm(). Requires OPENROUTER_API_KEY in .env.
           Measures actual classification/extraction accuracy too.

Use --baseline <report.json> to diff two runs (e.g. before/after a procedural
change) and quantify accuracy/time/call deltas.

Usage:
    python scripts/run_pilot.py --mock
    python scripts/run_pilot.py --real
    python scripts/run_pilot.py --mock --baseline data/pilot_report_baseline.json
    python scripts/run_pilot.py --mock --include contract
    python scripts/run_pilot.py --mock --source pileoflaw
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import structlog

logger = structlog.get_logger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("pilot")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

# NOTE: no `OPENROUTER_API_KEY` placeholder is ever set here. Mock runs patch
# get_llm entirely (no key needed); real runs must resolve a REAL key from the
# environment — llm/providers.py rejects the historical "mock-key" placeholder,
# so a live run can never silently execute against fake credentials.

from scripts.prepare_samples import is_real_sample, prepare_samples  # noqa: E402
from schemas.documents import get_extraction_schema  # noqa: E402

MANIFEST = REPO_ROOT / "docs" / "examples" / "samples" / "manifest.csv"

# Per-sample LLM metrics (mock mode increments calls/seconds; real mode also
# records usage/cost via the client wrapper below).
_LLM_METRICS = {"calls": 0, "seconds": 0.0, "usage": [], "cost_usd": 0.0}

# Run-wide cumulative cost (survives per-sample resets) for the cost watchdog.
_RUN_COST_USD = {"value": 0.0, "warned": False}
_COST_WARN_USD = 0.15
_COST_ABORT_USD = 0.20

# Live price snapshot fetched from OpenRouter at run start (per-token prices
# normalized to $/M); falls back to these estimates when the fetch fails.
# Values verified against the live /models API ($0.03/M in, $0.13/M out for
# qwen3.7-flash) and against Langfuse totalCost on prior real pilot traces.
_FALLBACK_PRICES = {
    "qwen/qwen3.7-flash": (0.03, 0.13),
    "deepseek/deepseek-v4-flash": (0.05, 0.25),  # judge — matches taxonomy.yaml cost_models
    "deepseek/deepseek-v4-pro": (0.435, 0.87),
}
_DEFAULT_PRICE = (0.03, 0.13)
_prices: dict = {}
_prices_fetched = False


def _fetch_openrouter_prices() -> dict:
    """Fetch live OpenRouter pricing (per-token), normalized to $/M tokens.

    The /models API reports `pricing.prompt`/`pricing.completion` in USD per
    token (e.g. 3e-08 == $0.03 per 1M tokens), so we scale by 1e6 to match the
    fallback constants below and the per-call cost formula.
    """
    try:
        import httpx

        resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=15)
        resp.raise_for_status()
        prices = {}
        for m in resp.json().get("data", []):
            model_id = m.get("id")
            pricing = m.get("pricing") or {}
            try:
                prices[model_id] = (
                    float(pricing.get("prompt") or 0) * 1_000_000,
                    float(pricing.get("completion") or 0) * 1_000_000,
                )
            except (TypeError, ValueError):
                continue
        logger.info("openrouter_prices_fetched", models=len(prices))
        return prices
    except Exception:
        logger.warning("openrouter_price_fetch_failed", exc_info=True)
        return {}


def _price_for(model: str) -> tuple[float, float]:
    global _prices_fetched
    if not _prices_fetched:
        _prices.update(_fetch_openrouter_prices())
        _prices_fetched = True
    if model in _prices:
        return _prices[model]
    for key, price in _FALLBACK_PRICES.items():
        if key in model:
            return price
    return _DEFAULT_PRICE


def _check_cost_watchdog() -> None:
    """Warn at $0.15, abort the run at $0.20 (cumulative across all samples)."""
    total = _RUN_COST_USD["value"]
    if total >= _COST_ABORT_USD:
        logger.error("cost_cap_abort", total_usd=round(total, 4), cap_usd=_COST_ABORT_USD)
        raise SystemExit(
            f"Pilot cost cap reached: ${total:.4f} >= ${_COST_ABORT_USD:.2f} — aborting."
        )
    if total >= _COST_WARN_USD and not _RUN_COST_USD["warned"]:
        _RUN_COST_USD["warned"] = True
        logger.warning(
            "cost_cap_warning",
            total_usd=round(total, 4),
            warn_at_usd=_COST_WARN_USD,
            abort_at_usd=_COST_ABORT_USD,
        )


def _wrap_client(client, model: str):
    """Record usage/latency/cost on every chat completion (real mode).

    All LLM access flows through the OpenAI client returned by get_llm
    (agents, pdf transcriber, reporter), so wrapping the client instance
    captures every call — including retries. Langfuse/Braintrust tracing
    wraps the class-level create; our instance-level wrapper shadows it and
    calls through, so tracing keeps working.
    """
    orig_create = client.chat.completions.create

    def recording_create(**kwargs):
        start = time.perf_counter()
        response = orig_create(**kwargs)
        elapsed = time.perf_counter() - start
        usage = getattr(response, "usage", None)
        pt = getattr(usage, "prompt_tokens", None) or 0
        ct = getattr(usage, "completion_tokens", None) or 0
        in_price, out_price = _price_for(model)
        cost = (pt * in_price + ct * out_price) / 1_000_000
        _LLM_METRICS["calls"] += 1
        _LLM_METRICS["seconds"] += elapsed
        _LLM_METRICS["cost_usd"] += cost
        _LLM_METRICS["usage"].append({
            "agent": kwargs.get("name") or "unknown",
            "model": model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "latency_ms": round(elapsed * 1000, 1),
            "cost_usd": round(cost, 6),
        })
        _RUN_COST_USD["value"] += cost
        _check_cost_watchdog()
        return response

    client.chat.completions.create = recording_create
    return client


def _real_get_llm(agent_name: str):
    """get_llm patch for real mode: build the real client, then instrument it
    with usage/cost recording."""
    client, model = _REAL_GET_LLM(agent_name)
    return _wrap_client(client, model), model


# Original (unpatched) get_llm, captured at import time so the real-mode
# wrapper can call through to it after llm.client.get_llm has been patched.
from llm.client import get_llm as _REAL_GET_LLM  # noqa: E402


def _fake_client(expect: dict) -> MagicMock:
    def create(**kwargs):
        start = time.perf_counter()
        _LLM_METRICS["calls"] += 1
        content = "Mock free-form output (report / transcription)."
        # `_call_structured` sends response_format={"type": "json_object"} with
        # the agent's instruction embedded in the user message, so the mock
        # keys its canned output off that instruction.
        last_msg = (kwargs.get("messages") or [{}])[-1]
        user_content = last_msg.get("content", "") if isinstance(last_msg, dict) else ""
        if "Classify this legal document" in user_content or "RE-EVALUATION REQUESTED" in user_content:
            content = json.dumps({
                "doc_type": expect["doc_type"],
                "confidence": expect["conf"],
                "reasoning": "mock",
            })
        elif "ADJUDICATION REQUEST" in user_content:
            content = json.dumps({"decision": "approved", "reasoning": "mock", "resolution_notes": ""})
        else:
            content = json.dumps({"confidence": expect["conf"], "mock_extraction": True})
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        _LLM_METRICS["seconds"] += time.perf_counter() - start
        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


# ---------------------------------------------------------------------------
# Vendored LangChain agents (sorter / contracts specialist) mocking + recording.
# They build their own ChatOpenAI and bypass llm.client.get_llm, so mock runs
# patch langchain_agents.base_agent.BaseAgent.llm and real runs wrap it with
# the same usage/cost recording as _wrap_client.
# ---------------------------------------------------------------------------

_LANGCHAIN_CLASSIFY_MARKERS = ("Classify this legal document", "RE-EVALUATION REQUESTED")


def _is_langchain_classify_call(user_text: str) -> bool:
    return any(marker in user_text for marker in _LANGCHAIN_CLASSIFY_MARKERS)


def _make_mock_langchain_llm(expect: dict):
    """Build the BaseAgent.llm replacement for mock mode: a deterministic
    FakeLangChainLLM keyed off the same per-sample expectations as
    _fake_client (calls/seconds recorded; no usage/cost, like the get_llm
    mock)."""
    from langchain_agents.mock import FakeLangChainLLM

    def _on_call(user_text: str, parsed: dict) -> None:
        _LLM_METRICS["calls"] += 1
        _LLM_METRICS["seconds"] += 0.005

    def _llm(self):
        return FakeLangChainLLM(
            classification={
                "doc_type": expect["doc_type"],
                "contract_subtype": "other" if expect["doc_type"] == "contract" else None,
                "confidence": expect["conf"],
                "reasoning": "mock",
            },
            extraction={"confidence": expect["conf"], "mock_extraction": True},
            on_call=_on_call,
        )

    return _llm


def _record_langchain_response(response, start: float, agent_name: str, model: str) -> None:
    """Mirror _wrap_client's usage/cost accounting for a LangChain response."""
    elapsed = time.perf_counter() - start
    usage = getattr(response, "usage_metadata", None) or (response.response_metadata or {}).get("usage") or {}
    pt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    ct = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    in_price, out_price = _price_for(model)
    cost = (pt * in_price + ct * out_price) / 1_000_000
    _LLM_METRICS["calls"] += 1
    _LLM_METRICS["seconds"] += elapsed
    _LLM_METRICS["cost_usd"] += cost
    _LLM_METRICS["usage"].append({
        "agent": agent_name,
        "model": model,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "latency_ms": round(elapsed * 1000, 1),
        "cost_usd": round(cost, 6),
    })
    _RUN_COST_USD["value"] += cost
    _check_cost_watchdog()


def _make_real_langchain_llm():
    """Wrap the vendored base's ChatOpenAI with per-invoke recording (real
    mode). Handles both the plain and the with_structured_output paths."""
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent

    _original_llm = _LangChainBaseAgent.llm

    class _RecordingLangChainLLM:
        def __init__(self, llm, agent_name, model):
            self._llm = llm
            self._agent_name = agent_name
            self._model = model

        def bind(self, **kwargs):
            return _RecordingLangChainLLM(self._llm.bind(**kwargs), self._agent_name, self._model)

        def with_structured_output(self, schema, **kwargs):
            inner = self._llm.with_structured_output(schema, **kwargs)
            return _RecordingStructuredOutput(inner, self._agent_name, self._model)

        def invoke(self, messages, **kwargs):
            start = time.perf_counter()
            response = self._llm.invoke(messages, **kwargs)
            _record_langchain_response(response, start, self._agent_name, self._model)
            return response

    class _RecordingStructuredOutput:
        def __init__(self, inner, agent_name, model):
            self._inner = inner
            self._agent_name = agent_name
            self._model = model

        def invoke(self, messages, **kwargs):
            start = time.perf_counter()
            raw_out = self._inner.invoke(messages, **kwargs)
            message = raw_out.get("raw") if isinstance(raw_out, dict) else getattr(raw_out, "raw", None)
            if message is not None:
                _record_langchain_response(message, start, self._agent_name, self._model)
            return raw_out

    def _llm(self):
        return _RecordingLangChainLLM(_original_llm(self), self.agent_name, self.model)

    return _llm


def run_sample(
    sample: dict, mock_mode: bool, session_id: str | None = None, run_id: str | None = None
) -> dict:
    from pipeline.bins import inbox_dir
    from graph.build_graph import run_pipeline

    matter_id = f"PILOT-{sample['id']}"
    sample_pdf = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "samples" / sample["subdir"] / sample["filename"]

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    queued = inbox / sample["filename"]
    shutil.copyfile(sample_pdf, queued)

    expect = {
        "doc_type": sample["expected_doc_class"],
        # Deterministic per-sample confidence (0.95-0.99) so the mock never
        # reports a flat 0.95 for every document; 0.40 simulates the genuinely
        # ambiguous sample. All values stay >= confidence.high so routing
        # remains deterministic (archived docs stay archived).
        "conf": 0.40
        if sample["id"] == "ambiguous_01"
        else 0.95 + (sum(sample["id"].encode()) % 5) / 100,
    }

    _LLM_METRICS["calls"] = 0
    _LLM_METRICS["seconds"] = 0.0
    _LLM_METRICS["usage"] = []
    _LLM_METRICS["cost_usd"] = 0.0

    def _mock_get_llm(agent_name):
        return _fake_client(expect), "mock-model"

    ground_truth = {
        "expected_doc_class": sample["expected_doc_class"],
        "expected_stage": sample["expected_stage"],
    }
    expected_fields = _parse_expected_fields(sample)
    if expected_fields:
        # Literal per-field expected extraction values from the manifest. When
        # present, the judge input skips the document text entirely and the
        # verdict is a field-by-field comparison (see _emit_pipeline_result).
        ground_truth["expected_fields"] = expected_fields

    started = time.perf_counter()
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent

    if mock_mode:
        with patch("llm.client.get_llm", side_effect=_mock_get_llm), \
             patch("agents.base.get_llm", side_effect=_mock_get_llm), \
             patch.object(_LangChainBaseAgent, "llm", new=_make_mock_langchain_llm(expect)):
            result = run_pipeline(
                queued, matter_id, source=sample.get("dataset"), ground_truth=ground_truth,
                session_id=session_id, run_id=run_id,
            )
    else:
        # Real mode: instrument every client with usage/latency/cost capture.
        with patch("llm.client.get_llm", side_effect=_real_get_llm), \
             patch("agents.base.get_llm", side_effect=_real_get_llm), \
             patch.object(_LangChainBaseAgent, "llm", new=_make_real_langchain_llm()):
            result = run_pipeline(
                queued, matter_id, source=sample.get("dataset"), ground_truth=ground_truth,
                session_id=session_id, run_id=run_id,
            )
    wall = time.perf_counter() - started

    total_tokens = sum(u["prompt_tokens"] + u["completion_tokens"] for u in _LLM_METRICS["usage"])

    return {
        "id": sample["id"],
        "subdir": sample["subdir"],
        "filename": sample["filename"],
        "expected_doc_class": sample["expected_doc_class"],
        "expected_stage": sample["expected_stage"],
        "size_tier": sample["size_tier"],
        "actual_doc_class": result.get("doc_type"),
        "doc_type": result.get("doc_type"),
        "contract_subtype": result.get("contract_subtype"),
        "classification_confidence": result.get("classification_confidence"),
        "extraction_confidence": result.get("extraction_confidence"),
        "extracted_data": result.get("extracted_data"),
        "stage": result.get("stage"),
        "classification_attempts": result.get("classification_attempts", 0),
        "extraction_attempts": result.get("extraction_attempts", 0),
        "retry_count": result.get("retry_count", 0),
        "wall_time_s": round(wall, 3),
        "llm_calls": _LLM_METRICS["calls"],
        "llm_time_s": round(_LLM_METRICS["seconds"], 3),
        "llm_cost_usd": round(_LLM_METRICS["cost_usd"], 6),
        "llm_tokens": total_tokens,
        "llm_usage": _LLM_METRICS["usage"],
        "class_match": result.get("doc_type") == sample["expected_doc_class"],
        "stage_expected": result.get("stage") == sample["expected_stage"],
    }


def _parse_expected_fields(sample: dict) -> dict | None:
    """Parse the manifest `expected_fields` JSON column (literal per-field
    expected extraction values) into a dict, or None when absent."""
    raw = (sample.get("expected_fields") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("expected_fields_invalid", filename=sample.get("filename"))
        return None


def filter_real_samples(manifest: list[dict], *, mock_mode: bool) -> list[dict]:
    """Restrict a pilot manifest to samples a given mode may process.

    Real (non-mock) runs must only process actual committed legal documents —
    the full Atticus/CUAD contract & agreement PDFs plus LegalBench MAUD.
    Repo-written synthetic .txt samples (render-to-PDF stand-ins under
    examples/sources/) are mock-only; they exist to test pipeline machinery,
    never to spend real LLM/eval tokens or pollute live traces. Mock runs
    keep the full live-manifest set.

    Returns the filtered manifest. Real-mode callers that end up with zero
    samples must refuse (see main()).
    """
    if mock_mode:
        return list(manifest)
    filtered = [m for m in manifest if is_real_sample(m)]
    synthetic = [m for m in manifest if not is_real_sample(m)]
    if synthetic:
        logger.warning(
            "real_run_blocked_synthetic_samples",
            blocked_ids=", ".join(m["id"] for m in synthetic),
            hint="synthetic .txt samples are mock-only; run them with --mock",
        )
    return filtered


def _validate_manifest_ground_truth(manifest: list[dict]) -> None:
    """Require complete, schema-compatible field truth before a scored pilot."""
    errors = []
    for sample in manifest:
        fields = _parse_expected_fields(sample)
        if fields is None or not isinstance(fields, dict):
            errors.append(f"{sample.get('id')}: expected_fields must be a JSON object")
            continue
        schema = get_extraction_schema(sample["expected_doc_class"])
        valid_keys = set(schema.model_fields) if schema is not None else set()
        unknown = sorted(set(fields) - valid_keys)
        if unknown:
            errors.append(f"{sample.get('id')}: unknown expected_fields keys: {unknown}")
    if errors:
        raise SystemExit("Invalid pilot ground truth:\n" + "\n".join(errors))


def _ground_truth_scores(row: dict, expected_fields: dict | None = None) -> dict:
    """Ground-truth scores for one pilot sample (attached to its trace).

    `expected_field_presence` is the fraction of required (non-empty) expected
    fields that the extractor surfaced with a non-empty value — a cheap
    deterministic field-coverage signal. Value-level correctness is left to
    the LLM-as-a-Judge verdict.
    """
    scores = {
        "class_correct": int(row["class_match"]),
        "stage_correct": int(row["stage_expected"]),
    }
    if expected_fields:
        extracted = row.get("extracted_data") or {}
        required = {
            key: value for key, value in expected_fields.items() if value not in (None, "")
        }
        if required:
            present = 0
            for key, expected_value in required.items():
                value = extracted.get(key)
                if isinstance(value, list):
                    ok = len(value) > 0
                else:
                    ok = bool(value)
                present += int(ok)
            scores["expected_field_presence"] = round(present / len(required), 3)
    conf = row.get("classification_confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        conf = float(conf)
        # How far the model's stated confidence is from the (binary) truth —
        # the calibration error. 0 means perfectly calibrated.
        scores["confidence_calibration_error"] = round(abs(conf - scores["class_correct"]), 3)
    return scores


def _ingest_scores(sample: dict, row: dict) -> None:
    """Attach ground-truth scores to the sample's deterministic trace id.

    Ground-truth scores (class/stage correctness, calibration error) are only
    computable against the pilot manifest, so they live here rather than in the
    pipeline. Confidence values and self-evident signals are already emitted by
    the pipeline itself. Deterministic field scores (field_scoring.py) are
    attached inside the pipeline run itself (graph/build_graph.py), not here.
    """
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client
    from observability.scores import create_trace_score, ensure_score_configs, is_enabled

    if not is_enabled():
        return
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        return
    try:
        trace_id = client.create_trace_id(seed=Path(sample["filename"]).stem)
    except Exception:
        logger.error("score_trace_id_failed", filename=sample["filename"])
        return

    ensure_score_configs()
    expected_fields = _parse_expected_fields(sample)
    for name, value in _ground_truth_scores(row, expected_fields).items():
        data_type = "BOOLEAN" if name in ("class_correct", "stage_correct") else "NUMERIC"
        create_trace_score(trace_id, name, value, data_type=data_type)
    logger.info("pilot_scores_ingested", filename=sample["filename"], trace_id=trace_id)


def _attach_field_scoring(sample: dict, row: dict) -> None:
    """Compute the deterministic field-type-aware extraction scores for a
    sample (issues #4/#5) and stash a serializable copy on the row for the
    report. Attachment to Langfuse already happened inside the pipeline run.
    """
    from observability.field_scoring import get_field_types, score_extraction

    expected = _parse_expected_fields(sample)
    extracted = row.get("extracted_data") or {}
    doc_class = row.get("doc_type") or sample.get("expected_doc_class")
    if not expected or not doc_class:
        row["field_scoring"] = None
        return
    try:
        result = score_extraction(
            doc_class, get_field_types(doc_class), extracted, expected,
            doc_text=row.get("doc_text"),
        )
        row["field_scoring"] = {
            "doc_class": doc_class,
            "field_scores": result.field_scores,
            "overall_score": result.overall_score,
            "ambiguous_fields": result.ambiguous_fields,
            "needs_judge_review": result.needs_judge_review,
            "entity_list_scores": {
                name: {
                    "precision": el.precision,
                    "recall": el.recall,
                    "f1": el.f1,
                    "matched": el.matched,
                    "unmatched_predicted": el.unmatched_predicted,
                    "unmatched_expected": el.unmatched_expected,
                }
                for name, el in result.entity_list_scores.items()
            },
            # Factuality audit (verified_precision / hallucination_rate per
            # populated field) + overall verified precision, for the report.
            "entity_list_audit": result.entity_list_audit,
            "overall_verified_precision": result.overall_verified_precision,
            "expected_fields": expected,
        }
    except Exception:
        logger.exception("field_scoring_failed", filename=sample.get("filename"))
        row["field_scoring"] = None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    archived = sum(1 for r in rows if r["stage"] == "archived")
    review = sum(1 for r in rows if r["stage"] == "review")
    failed = sum(1 for r in rows if r["stage"] == "failed")
    class_matches = sum(1 for r in rows if r["class_match"])
    per_class: dict[str, dict] = {}
    for r in rows:
        pc = per_class.setdefault(r["expected_doc_class"], {"n": 0, "match": 0, "time": 0.0, "calls": 0, "cost": 0.0})
        pc["n"] += 1
        pc["match"] += int(r["class_match"])
        pc["time"] += r["wall_time_s"]
        pc["calls"] += r["llm_calls"]
        pc["cost"] += r.get("llm_cost_usd", 0.0)

    # Mean calibration error: |stated confidence − binary correctness|.
    conf_rows = [
        (r["classification_confidence"], int(r["class_match"]))
        for r in rows
        if isinstance(r.get("classification_confidence"), (int, float))
        and not isinstance(r["classification_confidence"], bool)
    ]
    mean_calibration_error = (
        round(sum(abs(c - m) for c, m in conf_rows) / len(conf_rows), 3) if conf_rows else None
    )
    total_cost = round(sum(r.get("llm_cost_usd", 0.0) for r in rows), 6)
    total_tokens = sum(r.get("llm_tokens", 0) for r in rows)

    # Deterministic field-scoring summary (issues #4/#5): mean overall score,
    # fraction escalated to the LLM judge (ambiguous band), and judge calls
    # saved (2 evaluators skipped per unambiguous grounded run).
    scored = [r.get("field_scoring") for r in rows if r.get("field_scoring")]
    overalls = [s["overall_score"] for s in scored if isinstance(s.get("overall_score"), (int, float))]
    judge_review_n = sum(1 for s in scored if s.get("needs_judge_review"))
    judge_calls_saved = 2 * sum(1 for s in scored if s.get("needs_judge_review") is False)

    return {
        "samples": n,
        "archived": archived,
        "review": review,
        "failed": failed,
        "class_accuracy": round(class_matches / n, 3) if n else 0,
        "review_rate": round(review / n, 3) if n else 0,
        "mean_calibration_error": mean_calibration_error,
        "calibration_n": len(conf_rows),
        "avg_time_s": round(sum(r["wall_time_s"] for r in rows) / n, 3) if n else 0,
        "avg_llm_calls": round(sum(r["llm_calls"] for r in rows) / n, 1) if n else 0,
        "avg_cost_usd": round(total_cost / n, 6) if n else 0,
        "total_cost_usd": total_cost,
        "avg_tokens": round(total_tokens / n) if n else 0,
        "total_tokens": total_tokens,
        "avg_extraction_overall_score": round(sum(overalls) / len(overalls), 3) if overalls else None,
        "judge_review_rate": round(judge_review_n / len(scored), 3) if scored else None,
        "judge_calls_saved": judge_calls_saved,
        "per_class": {
            cls: {
                "n": v["n"],
                "class_accuracy": round(v["match"] / v["n"], 3),
                "avg_time_s": round(v["time"] / v["n"], 3),
                "avg_llm_calls": round(v["calls"] / v["n"], 1),
                "avg_cost_usd": round(v["cost"] / v["n"], 6),
            }
            for cls, v in sorted(per_class.items())
        },
    }


def misfile_candidates(rows: list[dict], report: dict | None = None) -> list[dict]:
    """Docs that sailed through when they shouldn't have: archived (or later
    staged) docs with a wrong class, schema-invalid extraction, or (when a
    judge pass already ran) judge completeness < 0.5. Review-bound docs are
    already caught by humans — not misfile candidates."""
    from observability.scores import validate_extraction

    judge_results: dict[str, dict] = {}
    if report:
        for r in ((report.get("evaluation") or {}).get("run", {}).get("results") or []):
            if isinstance(r, dict):
                judge_results[r.get("id")] = r

    candidates = []
    for r in rows:
        if r.get("stage") != "archived":
            continue
        reasons = []
        if not r.get("class_match"):
            reasons.append(
                f"class={r.get('doc_type')} != expected={r.get('expected_doc_class')}"
            )
        if not r.get("stage_expected"):
            reasons.append(
                f"stage={r.get('stage')} != expected={r.get('expected_stage')}"
            )
        extracted = r.get("extracted_data")
        if extracted:
            checks = validate_extraction(r.get("doc_type"), extracted)
            if checks.get("schema_valid") is False:
                reasons.append("schema_invalid")
        judge = judge_results.get(r["id"]) or {}
        if judge.get("status") == "judged":
            completeness = (judge.get("completeness") or {}).get("completeness")
            if isinstance(completeness, (int, float)) and completeness < 0.5:
                reasons.append(f"judge_completeness={completeness}")
        if reasons:
            candidates.append({
                "id": r["id"],
                "filename": r["filename"],
                "doc_type": r.get("doc_type"),
                "expected_doc_class": r.get("expected_doc_class"),
                "stage": r.get("stage"),
                "reasons": reasons,
            })
    return candidates


def diff_report(new: dict, baseline: dict) -> dict:
    keys = ["samples", "archived", "review", "failed", "class_accuracy", "avg_time_s", "avg_llm_calls"]
    overall = {k: {"baseline": baseline.get(k), "now": new.get(k)} for k in keys}
    per_class = {}
    for cls, b in (baseline.get("per_class") or {}).items():
        c = (new.get("per_class") or {}).get(cls, {})
        per_class[cls] = {
            k: {"baseline": b.get(k), "now": c.get(k)}
            for k in ("n", "class_accuracy", "avg_time_s", "avg_llm_calls")
        }
    return {"overall": overall, "per_class": per_class}


def print_rows(rows: list[dict]) -> None:
    header = f"{'id':<24}{'class':<18}{'exp_stage':<10}{'stage':<10}{'exp_class':<18}{'act_class':<18}{'conf':<5}{'calls':<6}{'time_s':<8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['id']:<24}{r['expected_doc_class']:<18}{r['expected_stage']:<10}{str(r['stage']):<10}"
            f"{r['expected_doc_class']:<18}{str(r['actual_doc_class']):<18}"
            f"{str(r['classification_confidence']):<5}{r['llm_calls']:<6}{r['wall_time_s']:<8}"
        )


def print_summary(summary: dict) -> None:
    print("\n== Summary ==")
    print(f"samples: {summary['samples']} | archived: {summary['archived']} | "
          f"review: {summary['review']} | failed: {summary['failed']}")
    print(f"class_accuracy: {summary['class_accuracy']} | review_rate: {summary['review_rate']} | "
          f"calibration_error: {summary['mean_calibration_error']} (n={summary['calibration_n']})")
    if summary.get("avg_extraction_overall_score") is not None:
        print(f"field_score_overall: {summary['avg_extraction_overall_score']} | "
              f"judge_review_rate: {summary['judge_review_rate']} | "
              f"judge_calls_saved: {summary['judge_calls_saved']}")
    print(f"avg_time_s: {summary['avg_time_s']} | avg_llm_calls: {summary['avg_llm_calls']} | "
          f"avg_cost_usd: {summary['avg_cost_usd']} | total_cost_usd: {summary['total_cost_usd']} | "
          f"avg_tokens: {summary['avg_tokens']}")
    for cls, s in summary["per_class"].items():
        print(f"  {cls:<20} n={s['n']:<2} acc={s['class_accuracy']:<6} "
              f"avg_time_s={s['avg_time_s']:<8} avg_calls={s['avg_llm_calls']:<4} avg_cost_usd={s['avg_cost_usd']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the pilot sample set through the pipeline.")
    parser.add_argument("--mock", action="store_true", help="Use a deterministic fake LLM (no API key).")
    parser.add_argument("--real", action="store_true", help="Use the real LLM (needs OPENROUTER_API_KEY).")
    parser.add_argument("--include", help="Only run samples of this expected doc class (e.g. contract).")
    parser.add_argument(
        "--source",
        help="Only run samples from this source corpus (e.g. legalbench, atticus, pileoflaw).",
    )
    parser.add_argument("--max-docs", type=int, default=None, help="Limit the run to the first N samples.")
    parser.add_argument("--baseline", help="Path to a previous pilot report JSON to diff against.")
    parser.add_argument(
        "--scores",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Attach ground-truth scores to Langfuse traces (default: on for --real, off for --mock).",
    )
    args = parser.parse_args()

    if args.mock and args.real:
        parser.error("choose --mock OR --real")
    if not args.mock and not args.real:
        parser.error(
            "choose --mock (deterministic fake LLM) or --real (real LLM). "
            "The mode is explicit so a run can never silently execute against fake LLM results."
        )
    mock_mode = args.mock
    if mock_mode:
        # Mock runs must never send traces (fake LLM, no real data). Tag the
        # environment as "mock" as belt-and-suspenders so any trace that leaks
        # through is clearly identifiable and filterable (pilot: development
        # runs polluted production traces with "OpenAI-generation" spans).
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        os.environ["OBSERVABILITY_ENVIRONMENT"] = "mock"
    else:
        # Real mode: fail fast before any document is processed if the
        # OpenRouter key is missing or is the mock placeholder. llm/providers.py
        # enforces the same check at every get_llm call.
        real_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not real_key or real_key == "mock-key":
            parser.error(
                "OPENROUTER_API_KEY is not set to a real key — refusing to run in --real mode."
            )
    scores_enabled = args.scores if args.scores is not None else (not mock_mode)

    prepare_samples()

    # One Langfuse session per pilot RUN (not per sample): every document trace
    # of this run lands in `pilot-<mode>-<timestamp>` in the Sessions view.
    # Trace ids stay deterministic per filename, so re-runs of already-traced
    # samples keep their FIRST run's session (immutable — same as tags/env).
    run_id = datetime.now(timezone.utc).isoformat()
    session_id = f"pilot-{'real' if not mock_mode else 'mock'}-{run_id}"

    with MANIFEST.open() as fh:
        manifest = list(csv.DictReader(fh))
    if args.include:
        manifest = [m for m in manifest if m["expected_doc_class"] == args.include]
    if args.source:
        manifest = [m for m in manifest if (m.get("dataset") or "original") == args.source]
        logger.info("source_filter", source=args.source, remaining=len(manifest))
    if args.max_docs:
        manifest = manifest[: args.max_docs]
        logger.info("max_docs_limit", limit=args.max_docs, remaining=len(manifest))

    # Real (non-mock) runs must only process real committed legal documents —
    # the Atticus/CUAD PDFs plus LegalBench MAUD. Repo-written synthetic .txt
    # samples are mock-only: they exist to test pipeline machinery, never to
    # spend real LLM/eval tokens or pollute live traces. Mock runs keep the
    # full live-manifest set.
    if not mock_mode:
        manifest = filter_real_samples(manifest, mock_mode=False)
        if not manifest:
            parser.error(
                "No real samples selected. --real runs only process actual "
                "committed legal documents (CUAD/Atticus PDFs, LegalBench). "
                "Synthetic .txt samples are mock-only — run with --mock."
            )
        logger.info("real_sample_filter", remaining=len(manifest))

    _validate_manifest_ground_truth(manifest)

    # L-22: one failing sample (or the cost watchdog) must not discard all
    # collected rows — per-sample try/except records {"status": "error"}, and
    # the report is written in a finally so partial runs still produce one.
    rows = []
    for m in manifest:
        try:
            rows.append(run_sample(m, mock_mode, session_id=session_id, run_id=run_id))
        except Exception as exc:
            logger.exception("sample_run_failed", sample=m.get("id"))
            rows.append({
                "id": m.get("id"),
                "filename": m.get("filename"),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    if scores_enabled:
        for m, r in zip(manifest, rows):
            if r.get("status") == "error":
                continue
            _ingest_scores(m, r)
            _attach_field_scoring(m, r)
        from observability.tracing import flush

        flush()

    summary = summarize(rows)
    print_rows(rows)
    print_summary(summary)

    report = {
        "run_id": run_id,
        "session_id": session_id,
        "mode": "mock" if mock_mode else "real",
        "scores_enabled": scores_enabled,
        "prices": {"source": "openrouter_live" if _prices else "fallback_estimates"},
        "summary": summary,
        "samples": rows,
        "misfile_candidates": misfile_candidates(rows, report=None),
        "errors": [r for r in rows if r.get("status") == "error"],
    }
    if scores_enabled:
        report["scores"] = {
            "samples": [
                {
                    "id": m["id"],
                    "scores": _ground_truth_scores(r, expected_fields=_parse_expected_fields(m)),
                    "field_scoring": r.get("field_scoring"),
                }
                for m, r in zip(manifest, rows)
                if r.get("status") != "error"
            ]
        }
    # L-24: run-scoped report path (pilot_report_<run_id>.json) so concurrent
    # runs never clobber each other, plus a dated baseline copy.
    run_tag = run_id.replace(":", "").replace("+", "").replace("-", "")[:20] or "run"
    out_path = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / f"pilot_report_{run_tag}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {out_path}")
    if not mock_mode:
        import datetime as _dt

        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        baseline_path = out_path.parent / f"pilot_report_baseline_real_{stamp}.json"
        baseline_path.write_text(json.dumps(report, indent=2))
        print(f"Real-run baseline copy written to {baseline_path}")
    if _RUN_COST_USD["value"] > 0:
        print(f"\nReal-run total LLM cost: ${_RUN_COST_USD['value']:.4f} "
              f"(watchdog: warn ${_COST_WARN_USD:.2f} / abort ${_COST_ABORT_USD:.2f})")
    if report["misfile_candidates"]:
        print("\n== Misfile candidates ==")
        for c in report["misfile_candidates"]:
            print(f"  {c['id']:<22} stage={c['stage']:<10} {', '.join(c['reasons'])}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        print("\n== Diff vs baseline ==")
        print(json.dumps(diff_report(summary, baseline["summary"]), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
