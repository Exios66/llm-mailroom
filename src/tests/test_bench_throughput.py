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


@pytest.mark.parametrize("flag", ["--latency-ms", "--retry-base-delay-ms"])
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "-0.5", "x"])
def test_parser_rejects_non_finite_or_negative_delays(flag, bad, capsys):
    with pytest.raises(SystemExit) as exc:
        bench.build_parser().parse_args([flag, bad])
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_parser_accepts_zero_and_fractional_delays():
    args = bench.build_parser().parse_args(["--latency-ms", "0", "--retry-base-delay-ms", "0.5"])
    assert args.latency_ms == 0.0 and args.retry_base_delay_ms == 0.5


@pytest.mark.parametrize("bad", ["nan", "inf", "0", "-1", "x"])
def test_parser_requires_finite_positive_timeout(bad, capsys):
    with pytest.raises(SystemExit) as exc:
        bench.build_parser().parse_args(["--timeout", bad])
    assert exc.value.code == 2
    assert "--timeout" in capsys.readouterr().err
    assert bench.build_parser().parse_args(["--timeout", "0.5"]).timeout == 0.5


def test_main_rejects_bad_latency_before_starting_any_worker(monkeypatch):
    started = []
    monkeypatch.setattr(bench, "run_level", lambda cfg, args: started.append(cfg))
    with pytest.raises(SystemExit) as exc:
        bench.main(["--latency-ms", "nan", "--docs", "1"])
    assert exc.value.code == 2 and started == []


# -- documents that fail without raising ----------------------------------------


class _FakeBins:
    def __init__(self, tmp):
        self._inbox = tmp

    def inbox_dir(self):
        return self._inbox

    def claim_file(self, path, worker):
        return path


def test_run_documents_records_error_text_of_returned_failures(tmp_path):
    def fake_run_pipeline(path, matter):
        if path.name == "a.txt":
            return {"stage": "failed", "error_message": "run aborted [timeout]: " + "x" * 500}
        if path.name == "b.txt":
            return {"stage": "failed"}  # no message: nothing to record
        if path.name == "c.txt":
            raise RuntimeError("boom")
        return {"stage": "archived", "error_message": "ignored on success"}

    corpus = [(n, "text", {}) for n in ("a.txt", "b.txt", "c.txt", "d.txt")]
    records, _wall = bench._run_documents(_FakeBins(tmp_path), corpus, 1, fake_run_pipeline, bench.Recorder())
    by_id = {r["id"]: r for r in records}
    assert by_id["a.txt"]["stage"] == "failed"
    assert by_id["a.txt"]["error"].startswith("run aborted [timeout]: ")
    assert len(by_id["a.txt"]["error"]) <= 130  # truncated, not the whole message
    assert by_id["b.txt"]["error"] is None
    assert by_id["c.txt"]["error"] == "RuntimeError: boom"
    assert by_id["d.txt"]["error"] is None
    rep = bench.build_level_report(1, records, 1.0, {})
    assert rep["failed"] == 3 and len(rep["errors"]) == 2  # a, b, c fail; only a and c carry text


# -- provenance ------------------------------------------------------------------


def test_provenance_changed_compares_commit_and_cleanliness():
    before = {"commit": "abc", "tree_clean": True}
    assert bench.provenance_changed(before, {"commit": "abc", "tree_clean": True}) is False
    assert bench.provenance_changed(before, {"commit": "def", "tree_clean": True}) is True
    assert bench.provenance_changed(before, {"commit": "abc", "tree_clean": False}) is True


def _fake_main_env(monkeypatch, snapshots):
    """Make main() run no workers; ``snapshots`` feeds successive git observations."""
    calls = []
    monkeypatch.setattr(bench, "run_level", lambda cfg, args: calls.append("level") or {
        **bench.build_level_report(cfg["concurrency"], [_doc("archived", 1.0)], 1.0, {}), "long_docs": 0, "cpu": None,
    })
    seq = iter(snapshots)

    def fake_git_state():
        calls.append("git")
        return next(seq)

    monkeypatch.setattr(bench, "git_state", fake_git_state)
    monkeypatch.setattr(bench, "machine_notes", lambda: calls.append("machine") or {"load_average_at_start": [0.1, 0.1, 0.1]})
    return calls


def test_main_captures_provenance_before_levels_and_flags_a_change(monkeypatch, tmp_path):
    calls = _fake_main_env(
        monkeypatch,
        [{"commit": "aaa", "tree_clean": True}, {"commit": "bbb", "tree_clean": False}],
    )
    out = tmp_path / "r.json"
    assert bench.main(["--levels", "1,2", "--docs", "1", "--json-out", str(out)]) == 0
    assert calls == ["git", "machine", "level", "level", "git"]  # provenance first, re-check last
    report = json.loads(out.read_text())
    assert report["commit"] == "aaa" and report["tree_clean"] is True  # as measured at the start
    assert report["provenance_changed"] is True
    assert report["provenance_after"] == {"commit": "bbb", "tree_clean": False}


def test_main_marks_unchanged_provenance(monkeypatch, tmp_path, capsys):
    snap = {"commit": "aaa", "tree_clean": True}
    _fake_main_env(monkeypatch, [snap, dict(snap)])
    out = tmp_path / "r.json"
    assert bench.main(["--levels", "1", "--docs", "1", "--json-out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["provenance_changed"] is False and "provenance_after" not in report
    assert "provenance changed" not in capsys.readouterr().out.lower()


def test_markdown_warns_when_provenance_changed(monkeypatch, tmp_path, capsys):
    _fake_main_env(monkeypatch, [{"commit": "a", "tree_clean": True}, {"commit": "b", "tree_clean": True}])
    bench.main(["--levels", "1", "--docs", "1"])
    assert "provenance changed" in capsys.readouterr().out.lower()


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
