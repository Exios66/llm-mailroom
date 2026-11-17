"""Offline load benchmark (src/scripts/bench_throughput.py): statistics, aggregation, smoke run."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import bench_throughput as bench

ROOT = Path(__file__).resolve().parents[2]


# -- statistics ---------------------------------------------------------------


def test_percentile_interpolates_and_handles_edges():
    assert bench.percentile([], 0.5) is None
    assert bench.percentile([4.0], 0.95) == 4.0
    assert bench.percentile([1, 2, 3, 4], 0.0) == 1
    assert bench.percentile([1, 2, 3, 4], 1.0) == 4
    assert bench.percentile([4, 1, 3, 2], 0.5) == pytest.approx(2.5)  # unsorted input
    assert bench.percentile(range(101), 0.95) == pytest.approx(95.0)
    with pytest.raises(ValueError):
        bench.percentile([1, 2], 1.5)


def test_summarize_empty_and_filled():
    assert bench.summarize([]) == {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    s = bench.summarize([1.0, 2.0, 3.0])
    assert s["n"] == 3 and s["mean"] == pytest.approx(2.0) and s["p50"] == 2.0 and s["max"] == 3.0


def test_recorder_counts_resets_and_snapshots_independently():
    rec = bench.Recorder()
    rec.bump("llm_calls")
    rec.bump("llm_calls", 2)
    rec.add_seconds("heartbeat_s", 0.25)
    rec.node("classify", 0.3, {"llm_s": 0.2, "llm_calls": 1, "hb_s": 0.01})
    snap = rec.snapshot()
    assert snap["counters"] == {"llm_calls": 3}
    assert snap["seconds"] == {"heartbeat_s": 0.25}
    snap["nodes"]["classify"][0]["wall_s"] = 99  # the snapshot is a copy
    assert rec.snapshot()["nodes"]["classify"][0]["wall_s"] == 0.3
    rec.reset()
    assert rec.snapshot() == {"counters": {}, "seconds": {}, "nodes": {}}


def test_node_report_splits_llm_heartbeat_and_overhead():
    samples = [
        {"wall_s": 0.30, "llm_s": 0.20, "llm_calls": 1, "heartbeat_s": 0.01},
        {"wall_s": 0.50, "llm_s": 0.20, "llm_calls": 1, "heartbeat_s": 0.03},
    ]
    rep = bench.node_report(samples)
    assert rep["invocations"] == 2 and rep["llm_calls"] == 2
    assert rep["p50_s"] == pytest.approx(0.40)
    assert rep["mean_llm_s"] == pytest.approx(0.20)
    assert rep["mean_heartbeat_s"] == pytest.approx(0.02)
    assert rep["mean_overhead_s"] == pytest.approx(0.20)  # (0.10 + 0.30) / 2


# -- aggregation --------------------------------------------------------------


def _doc(stage, latency, llm_s=0.4, **extra):
    return {"id": "d", "stage": stage, "latency_s": latency, "llm_s": llm_s, "error": None, **extra}


def test_build_level_report_counts_failures_and_rates():
    docs = [_doc("archived", 1.0), _doc("review", 2.0), _doc("failed", 3.0), _doc("raised", 4.0)]
    docs[3]["error"] = "RuntimeError: boom"
    snap = {
        "counters": {
            "llm_calls": 8, "openai_client_constructions": 4, "langchain_client_constructions": 4,
            "heartbeat_calls": 24, "db_connections_opened": 40, "asyncio_run_calls": 20,
        },
        "seconds": {"heartbeat_s": 0.5},
        "nodes": {"classify": [{"wall_s": 0.5, "llm_s": 0.4, "llm_calls": 1, "heartbeat_s": 0.0}]},
    }
    rep = bench.build_level_report(8, docs, 30.0, snap)
    assert rep["docs"] == 4 and rep["completed"] == 2 and rep["failed"] == 2
    assert rep["effective_concurrency"] == 4  # never more workers than documents
    assert rep["stages"] == {"archived": 1, "failed": 1, "raised": 1, "review": 1}
    assert rep["docs_per_min"] == pytest.approx(8.0)
    assert rep["latency_s"]["p50"] == pytest.approx(2.5)
    assert rep["overhead_s"]["mean"] == pytest.approx(2.1)  # latency minus 0.4 s of LLM time
    assert rep["counters"]["llm_calls"] == 8 and rep["counters"]["rate_limited"] == 0
    assert rep["per_doc"]["client_constructions_mean"] == pytest.approx(2.0)
    assert rep["per_doc"]["heartbeats_mean"] == pytest.approx(6.0)
    assert rep["seconds"]["heartbeat_total_s"] == pytest.approx(0.5)
    assert rep["errors"] == {"RuntimeError: boom": 1}
    assert rep["nodes"]["classify"]["invocations"] == 1


def test_build_level_report_handles_no_documents_and_zero_wall():
    rep = bench.build_level_report(4, [], 0.0, {})
    assert rep["docs"] == 0 and rep["docs_per_min"] is None and rep["latency_s"]["p50"] is None
    assert rep["per_doc"]["llm_calls_mean"] is None and rep["effective_concurrency"] == 0


def test_cpu_report_computes_busy_cores_and_tolerates_missing_data():
    rep = bench.cpu_report((1.0, 0.5), (3.0, 1.5), 4.0)
    assert rep == {"user_s": 2.0, "system_s": 1.0, "cores_busy": pytest.approx(0.75)}
    assert bench.cpu_report(None, (1.0, 1.0), 1.0) is None
    assert bench.cpu_report((0.0, 0.0), (1.0, 1.0), 0.0)["cores_busy"] is None


def test_render_markdown_has_one_row_per_level_and_node():
    levels = [
        bench.build_level_report(
            c, [_doc("archived", 1.0)], 2.0,
            {"nodes": {"extract": [{"wall_s": 0.5, "llm_s": 0.2, "llm_calls": 1, "heartbeat_s": 0.01}]}},
        )
        for c in (1, 4)
    ]
    levels[0]["cpu"] = {"user_s": 1.0, "system_s": 0.5, "cores_busy": 0.75}  # level 4 has none: "-"
    cfg = {"docs": 1, "latency_ms": 200.0, "rate_limit_rate": 0.0}
    md = bench.render_markdown({"levels": levels, "config": cfg})
    assert md.count("\n| 1 | 1 / 0 |") == 1 and md.count("\n| 4 | 1 / 0 |") == 1
    assert md.splitlines()[2].endswith("| 0.75 |") and md.splitlines()[3].endswith("| - |")
    assert "| extract | 500 / 500 (1) | 500 / 500 (1) |" in md
    assert "Not production numbers" in md


# -- argument parsing ---------------------------------------------------------


def test_parser_defaults_and_validation():
    args = bench.build_parser().parse_args([])
    assert args.levels == [1, 4, 16, 64] and args.rate_limit_rate == 0.0 and args.latency_ms == 200.0
    for bad in (["--levels", "1,0"], ["--levels", "x"], ["--rate-limit-rate", "1.5"]):
        with pytest.raises(SystemExit):
            bench.build_parser().parse_args(bad)


# -- fake-LLM layer -----------------------------------------------------------


def test_bench_mock_injects_429_and_counts_latency():
    openai = pytest.importorskip("openai")
    rec = bench.Recorder()
    always = bench.BenchMock(rec, rate_limit_rate=1.0, seed=1, latency_s=0.0)
    with pytest.raises(openai.RateLimitError):
        always._pre_call({"id": "d1", "cls": "contract", "conf": 0.97, "kind": "ok"})
    assert rec.snapshot()["counters"] == {"llm_calls": 1, "rate_limited": 1}

    rec2 = bench.Recorder()
    never = bench.BenchMock(rec2, rate_limit_rate=0.0, seed=1, latency_s=0.001)
    never._pre_call({"id": "d1", "cls": "contract", "conf": 0.97, "kind": "ok"})
    snap = rec2.snapshot()
    assert snap["counters"] == {"llm_calls": 1} and snap["seconds"]["llm_s"] == pytest.approx(0.001)


def test_rate_limit_decision_is_deterministic_per_doc_and_call():
    openai = pytest.importorskip("openai")

    def pattern(seed):
        mock = bench.BenchMock(bench.Recorder(), rate_limit_rate=0.5, seed=seed, latency_s=0.0)
        out = []
        for _ in range(20):
            try:
                mock._pre_call({"id": "doc-a", "cls": "contract", "conf": 0.97, "kind": "ok"})
                out.append(False)
            except openai.RateLimitError:
                out.append(True)
        return out

    assert pattern(3) == pattern(3)
    assert 0 < sum(pattern(3)) < 20


# -- smoke: the real graph, in a hermetic subprocess ---------------------------


def test_smoke_run_writes_json_and_markdown(tmp_path):
    json_out, md_out = tmp_path / "out.json", tmp_path / "out.md"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [
            sys.executable, str(ROOT / "src/scripts/bench_throughput.py"),
            "--levels", "1,2", "--docs", "2", "--latency-ms", "0", "--warmup-docs", "1",
            "--workdir", str(tmp_path), "--json-out", str(json_out), "--md-out", str(md_out),
        ],
        capture_output=True, text=True, env=env, timeout=240, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    report = json.loads(json_out.read_text())
    assert report["mock_llm"] is True and [lv["concurrency"] for lv in report["levels"]] == [1, 2]
    for lv in report["levels"]:
        assert lv["docs"] == 2 and lv["failed"] == 0, lv
        assert lv["stages"].get("archived", 0) + lv["stages"].get("review", 0) == 2
        c = lv["counters"]
        # The seams the benchmark exists to count are really wired: a document builds LLM clients,
        # beats the heart at node boundaries and opens database connections.
        assert c["llm_calls"] >= 2
        assert c["openai_client_constructions"] + c["langchain_client_constructions"] >= 2
        assert c["heartbeat_calls"] >= 6 and c["db_connections_opened"] > 0 and c["db_engines_created"] > 0
        assert {"intake", "classify", "extract", "archive"} <= set(lv["nodes"])
        assert lv["cpu"] is None or lv["cpu"]["cores_busy"] > 0
    assert md_out.read_text().startswith("| concurrency |")
