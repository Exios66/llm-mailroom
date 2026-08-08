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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

os.environ.setdefault("OPENROUTER_API_KEY", "mock-key")
# Tracing mode is set in main() from the CLI flag: --mock forces it off so
# mock runs stay hermetic; --real leaves .env's auto resolution (-> langfuse).

from scripts.prepare_samples import prepare_samples  # noqa: E402

MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"

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


def run_sample(sample: dict, mock_mode: bool) -> dict:
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
        "conf": 0.40 if sample["id"] == "ambiguous_01" else 0.95,
    }

    _LLM_METRICS["calls"] = 0
    _LLM_METRICS["seconds"] = 0.0
    _LLM_METRICS["usage"] = []
    _LLM_METRICS["cost_usd"] = 0.0

    def _mock_get_llm(agent_name):
        return _fake_client(expect), "mock-model"

    started = time.perf_counter()
    if mock_mode:
        with patch("llm.client.get_llm", side_effect=_mock_get_llm), \
             patch("agents.base.get_llm", side_effect=_mock_get_llm):
            result = run_pipeline(queued, matter_id)
    else:
        # Real mode: instrument every client with usage/latency/cost capture.
        with patch("llm.client.get_llm", side_effect=_real_get_llm), \
             patch("agents.base.get_llm", side_effect=_real_get_llm):
            result = run_pipeline(queued, matter_id)
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


def _ground_truth_scores(row: dict) -> dict:
    """Ground-truth scores for one pilot sample (attached to its trace)."""
    scores = {
        "class_correct": int(row["class_match"]),
        "stage_correct": int(row["stage_expected"]),
    }
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
    the pipeline itself.
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
    for name, value in _ground_truth_scores(row).items():
        data_type = "BOOLEAN" if name in ("class_correct", "stage_correct") else "NUMERIC"
        create_trace_score(trace_id, name, value, data_type=data_type)
    logger.info("pilot_scores_ingested", filename=sample["filename"], trace_id=trace_id)


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
    mock_mode = not args.real
    if mock_mode:
        # Mock runs must never send traces (fake LLM, no real data). Tag the
        # environment as "mock" as belt-and-suspenders so any trace that leaks
        # through is clearly identifiable and filterable (pilot: development
        # runs polluted production traces with "OpenAI-generation" spans).
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        os.environ["OBSERVABILITY_ENVIRONMENT"] = "mock"
    scores_enabled = args.scores if args.scores is not None else (not mock_mode)

    prepare_samples()

    with MANIFEST.open() as fh:
        manifest = list(csv.DictReader(fh))
    if args.include:
        manifest = [m for m in manifest if m["expected_doc_class"] == args.include]
    if args.max_docs:
        manifest = manifest[: args.max_docs]
        logger.info("max_docs_limit", limit=args.max_docs, remaining=len(manifest))

    rows = [run_sample(m, mock_mode) for m in manifest]

    if scores_enabled:
        for m, r in zip(manifest, rows):
            _ingest_scores(m, r)
        from observability.tracing import flush

        flush()

    summary = summarize(rows)
    print_rows(rows)
    print_summary(summary)

    report = {
        "run_id": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if mock_mode else "real",
        "scores_enabled": scores_enabled,
        "prices": {"source": "openrouter_live" if _prices else "fallback_estimates"},
        "summary": summary,
        "samples": rows,
        "misfile_candidates": misfile_candidates(rows, report=None),
    }
    if scores_enabled:
        report["scores"] = {
            "samples": [{"id": m["id"], "scores": _ground_truth_scores(r)} for m, r in zip(manifest, rows)]
        }
    out_path = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "pilot_report.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {out_path}")
    if not mock_mode:
        baseline_path = out_path.parent / "pilot_report_baseline_real.json"
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
