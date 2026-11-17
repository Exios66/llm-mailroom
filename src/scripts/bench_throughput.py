#!/usr/bin/env python3
"""Offline load benchmark for the document graph (Phase 0: measure before changing anything).

Runs the REAL pipeline (``graph.build_graph.run_pipeline``: graph, bins, catalog, audit
log) on synthetic documents at several concurrency levels against the repository's
deterministic fake LLM (``scripts/stress_test.py:ScaleMock``, which patches the same seams
as ``run_pilot.py --mock``: the OpenAI-style client and the vendored LangChain agents),
with a per-call latency and an optional simulated rate-limit fraction injected at the
fake-client layer. No network, no real LLM, no tracing backend: every level runs in its
own subprocess with a fresh temporary ``MAILROOM_BASE_DIR`` and the hermetic worker
environment of ``stress_test.py`` (credentials stripped, ``OPENROUTER_BASE_URL`` pointed
at a closed local port so a missed seam could never reach a hosted service).

What it makes visible (counted around unmodified pipeline code, nothing is changed):

* ``openai_client_constructions`` / ``langchain_client_constructions``: how many HTTP
  clients were built (``llm.client.get_llm`` builds an ``OpenAI`` per agent instance;
  the vendored LangChain agents build a ``ChatOpenAI`` per agent instance). The real
  client is still constructed, so its cost is paid; only the request is faked.
* ``heartbeat_calls`` / ``heartbeat_s_total``: ``_touch_heartbeat`` runs at every node
  boundary, ``asyncio_run_calls`` counts the event loops created, ``db_engines_created``
  the SQLAlchemy engines, ``db_connections_opened`` the raw SQLite connections
  (``NullPool`` opens one per statement batch).
* per-node wall time (p50/p95), the injected LLM latency, the heartbeat time and the
  overhead (wall minus LLM time, so it still includes the heartbeat, which is also
  reported on its own); end-to-end per-document latency (p50/p95); docs/min.
* ``cpu``: process CPU seconds during the level and the average cores kept busy
  (``cores_busy`` near 1.0 with idle LLM waits means the process is CPU-bound).

Latency is per document, from the claim of the file to the returned state; it does not
include time queued behind the thread pool. All numbers are mock-LLM numbers: absolute
values are NOT production numbers, only the shape across concurrency levels and the
overhead that is independent of provider latency are meaningful.

Provenance (commit, ``tree_clean``, machine notes including the load average) is captured
before the first level and HEAD/cleanliness is re-checked after the last; the report carries
``provenance_changed`` (and ``provenance_after``) when they differ.

Retry backoff sleeps are shortened through the taxonomy ``llm_retry`` block (mutated in the
worker process only, via ``--retry-base-delay-ms``); both retry layers
(``llm/retry.py`` and the LangChain agents) read that block.

Usage::

    PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py
    PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py --levels 1,4,16,64 \\
        --docs 128 --latency-ms 200 --json-out out.json --md-out out.md
    PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py --levels 4 --docs 16 \\
        --rate-limit-rate 0.2
"""

from __future__ import annotations

import argparse
import collections
import contextvars
import functools
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

# Light module (no project imports at import time): corpus, fake LLM, hermetic env.
from scripts.stress_test import ScaleMock, _u, _worker_env, make_corpus  # noqa: E402

SCHEMA_VERSION = 1
_ERROR_TEXT_CHARS = 120  # failure text kept per document: enough to group causes, never document text
DEFAULT_LEVELS = (1, 4, 16, 64)
#: Closed local port: if a seam were ever missed, a request fails fast on this machine
#: instead of reaching a hosted provider.
DISCARD_BASE_URL = "http://127.0.0.1:9/v1"

# Per-node-invocation and per-document accumulators, visible to the fake LLM and the
# counting wrappers through the context of the thread that runs the node.
_FRAME: contextvars.ContextVar = contextvars.ContextVar("bench_frame", default=None)
_DOC: contextvars.ContextVar = contextvars.ContextVar("bench_doc", default=None)


# ---------------------------------------------------------------------------
# Pure statistics and aggregation (unit-tested; no project imports)
# ---------------------------------------------------------------------------


def percentile(values, q: float) -> float | None:
    """Linearly interpolated ``q``-quantile (``q`` in [0, 1]) of ``values``; None when empty."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be within [0, 1], got {q!r}")
    pos = q * (len(data) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def summarize(values) -> dict:
    """``{n, mean, p50, p95, max}`` of ``values`` (None statistics when empty)."""
    data = [float(v) for v in values]
    if not data:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "n": len(data),
        "mean": sum(data) / len(data),
        "p50": percentile(data, 0.50),
        "p95": percentile(data, 0.95),
        "max": max(data),
    }


class Recorder:
    """Thread-safe counters, timings and per-node samples for one benchmark level."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.counters: collections.Counter = collections.Counter()
            self.seconds: dict[str, float] = collections.defaultdict(float)
            self.nodes: dict[str, list[dict]] = collections.defaultdict(list)

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def add_seconds(self, name: str, seconds: float) -> None:
        with self._lock:
            self.seconds[name] += seconds

    def node(self, name: str, wall_s: float, frame: dict | None) -> None:
        frame = frame or {}
        with self._lock:
            self.nodes[name].append(
                {
                    "wall_s": wall_s,
                    "llm_s": frame.get("llm_s", 0.0),
                    "llm_calls": frame.get("llm_calls", 0),
                    "heartbeat_s": frame.get("hb_s", 0.0),
                }
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self.counters),
                "seconds": dict(self.seconds),
                "nodes": {k: [dict(s) for s in v] for k, v in self.nodes.items()},
            }


def node_report(samples: list[dict]) -> dict:
    """Per-node statistics (seconds) from invocation samples (see ``Recorder.node``).

    ``mean_overhead_s`` is wall time minus injected LLM time only. It therefore includes
    the heartbeat time, which ``mean_heartbeat_s`` reports separately as a part of it.
    """
    wall = [s["wall_s"] for s in samples]
    n = len(samples)
    llm = [s["llm_s"] for s in samples]
    hb = [s["heartbeat_s"] for s in samples]
    overhead = [max(0.0, s["wall_s"] - s["llm_s"]) for s in samples]
    return {
        "invocations": n,
        "p50_s": percentile(wall, 0.50),
        "p95_s": percentile(wall, 0.95),
        "mean_s": sum(wall) / n if n else None,
        "mean_llm_s": sum(llm) / n if n else None,
        "mean_heartbeat_s": sum(hb) / n if n else None,
        "mean_overhead_s": sum(overhead) / n if n else None,
        "llm_calls": sum(s["llm_calls"] for s in samples),
    }


def build_level_report(
    concurrency: int, docs: list[dict], wall_s: float, snapshot: dict
) -> dict:
    """One level's report from per-document records, the wall time and a ``Recorder`` snapshot.

    A document record is ``{id, stage, latency_s, llm_s, llm_calls, rate_limited, ...}``;
    ``stage`` is the returned pipeline stage, or ``"raised"`` when ``run_pipeline`` raised.
    """
    n = len(docs)
    stages = collections.Counter(d.get("stage") for d in docs)
    failed = stages.get("raised", 0) + stages.get("failed", 0)
    completed = n - failed
    latencies = [d["latency_s"] for d in docs]
    overheads = [max(0.0, d["latency_s"] - d.get("llm_s", 0.0)) for d in docs]
    counters = snapshot.get("counters", {})
    seconds = snapshot.get("seconds", {})
    return {
        "concurrency": concurrency,
        "effective_concurrency": min(concurrency, n) if n else 0,
        "docs": n,
        "completed": completed,
        "failed": failed,
        "stages": {str(k): v for k, v in sorted(stages.items(), key=lambda kv: str(kv[0]))},
        "wall_s": wall_s,
        "docs_per_min": (n / wall_s * 60.0) if wall_s > 0 else None,
        "latency_s": summarize(latencies),
        "overhead_s": summarize(overheads),
        "nodes": {name: node_report(s) for name, s in sorted(snapshot.get("nodes", {}).items())},
        "counters": {
            "llm_calls": counters.get("llm_calls", 0),
            "rate_limited": counters.get("rate_limited", 0),
            "openai_client_constructions": counters.get("openai_client_constructions", 0),
            "langchain_client_constructions": counters.get("langchain_client_constructions", 0),
            "heartbeat_calls": counters.get("heartbeat_calls", 0),
            "run_coro_calls": counters.get("run_coro_calls", 0),
            "asyncio_run_calls": counters.get("asyncio_run_calls", 0),
            "db_engines_created": counters.get("db_engines_created", 0),
            "db_connections_opened": counters.get("db_connections_opened", 0),
        },
        "seconds": {
            "heartbeat_total_s": seconds.get("heartbeat_s", 0.0),
            "openai_client_construct_total_s": seconds.get("openai_construct_s", 0.0),
            "langchain_client_construct_total_s": seconds.get("langchain_construct_s", 0.0),
            "llm_latency_total_s": seconds.get("llm_s", 0.0),
        },
        "per_doc": {
            "llm_calls_mean": counters.get("llm_calls", 0) / n if n else None,
            "client_constructions_mean": (
                (
                    counters.get("openai_client_constructions", 0)
                    + counters.get("langchain_client_constructions", 0)
                )
                / n
                if n
                else None
            ),
            "heartbeats_mean": counters.get("heartbeat_calls", 0) / n if n else None,
            "db_connections_mean": counters.get("db_connections_opened", 0) / n if n else None,
        },
        "errors": dict(collections.Counter(d["error"] for d in docs if d.get("error")).most_common(5)),
    }


def _ms(value) -> str:
    return "-" if value is None else f"{value * 1000:.0f}"


def render_markdown(report: dict) -> str:
    """Markdown tables for a full report (``main``'s JSON document)."""
    levels = report["levels"]
    cfg = report["config"]
    lines = [
        "| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | "
        "doc overhead p50 ms | LLM calls | 429s injected | OpenAI ctor | ChatOpenAI ctor | "
        "heartbeats | heartbeat s | event loops | DB engines | DB connections | CPU cores busy |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lv in levels:
        c = lv["counters"]
        dpm = lv["docs_per_min"]
        cores = (lv.get("cpu") or {}).get("cores_busy")
        lines.append(
            f"| {lv['concurrency']} | {lv['completed']} / {lv['failed']} | {lv['wall_s']:.1f} | "
            f"{'-' if dpm is None else f'{dpm:.0f}'} | {_ms(lv['latency_s']['p50'])} | "
            f"{_ms(lv['latency_s']['p95'])} | {_ms(lv['overhead_s']['p50'])} | {c['llm_calls']} | "
            f"{c['rate_limited']} | {c['openai_client_constructions']} | "
            f"{c['langchain_client_constructions']} | {c['heartbeat_calls']} | "
            f"{lv['seconds']['heartbeat_total_s']:.1f} | {c['asyncio_run_calls']} | "
            f"{c['db_engines_created']} | {c['db_connections_opened']} | "
            f"{'-' if cores is None else f'{cores:.2f}'} |"
        )
    node_names = sorted({name for lv in levels for name in lv["nodes"]})
    if node_names:
        head = " | ".join(f"c={lv['concurrency']} p50 / p95 ms (n)" for lv in levels)
        lines += ["", f"| node | {head} |", "|---|" + "---:|" * len(levels)]
        for name in node_names:
            cells = []
            for lv in levels:
                nd = lv["nodes"].get(name)
                cells.append(
                    "-" if not nd else f"{_ms(nd['p50_s'])} / {_ms(nd['p95_s'])} ({nd['invocations']})"
                )
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines += [
            "",
            "| node | " + " | ".join(f"c={lv['concurrency']} mean LLM / overhead (incl. heartbeat) / heartbeat ms" for lv in levels) + " |",
            "|---|" + "---:|" * len(levels),
        ]
        for name in node_names:
            cells = []
            for lv in levels:
                nd = lv["nodes"].get(name)
                cells.append(
                    "-"
                    if not nd
                    else f"{_ms(nd['mean_llm_s'])} / {_ms(nd['mean_overhead_s'])} / {_ms(nd['mean_heartbeat_s'])}"
                )
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"Mock LLM, {cfg['docs']} documents per level, {cfg['latency_ms']:g} ms injected per LLM call, "
        f"rate-limit fraction {cfg['rate_limit_rate']:g}. Not production numbers.",
    ]
    if report.get("provenance_changed"):
        after = report.get("provenance_after") or {}
        lines += [
            "",
            f"Provenance changed during the run: measured at {report.get('commit') or '?'} "
            f"(clean tree: {report.get('tree_clean')}), HEAD afterwards {after.get('commit') or '?'} "
            f"(clean tree: {after.get('tree_clean')}). Do not cite these numbers for one commit.",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Worker side: runs in a child process (one level, fresh base dir)
# ---------------------------------------------------------------------------


class BenchMock(ScaleMock):
    """``ScaleMock`` with benchmark accounting and pure 429 injection.

    Latency is slept once per successful call; an injected rate limit returns at once
    (like a real 429) without a ``Retry-After`` header, so the retry backoff is the one
    configured in ``llm_retry`` (shortened by ``--retry-base-delay-ms``).
    """

    def __init__(self, recorder: Recorder, *, rate_limit_rate: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        self.recorder = recorder
        self.rate_limit_rate = rate_limit_rate

    def _pre_call(self, meta: dict) -> None:
        import httpx
        import openai

        key = meta["id"] or "anon"
        with self.lock:
            self.calls_total += 1
            self.per_doc[key] += 1
            k = self.per_doc[key]
        frame, doc = _FRAME.get(), _DOC.get()
        self.recorder.bump("llm_calls")
        for acc in (frame, doc):
            if acc is not None:
                acc["llm_calls"] += 1
        if self.rate_limit_rate > 0 and _u(self.seed, "rate-limit", key, k) < self.rate_limit_rate:
            self.recorder.bump("rate_limited")
            if doc is not None:
                doc["rate_limited"] += 1
            req = httpx.Request("POST", "https://mock.invalid/v1/chat/completions")
            raise openai.RateLimitError(
                "rate limited (injected)", response=httpx.Response(429, request=req), body=None
            )
        if self.latency_s > 0:
            self.real_sleep(self.latency_s)
            self.recorder.add_seconds("llm_s", self.latency_s)
            for acc in (frame, doc):
                if acc is not None:
                    acc["llm_s"] += self.latency_s


def _counting_openai(real_cls, mock: BenchMock, recorder: Recorder):
    """Factory for ``llm.client.OpenAI``: count, build the REAL client, fake only its request."""

    def factory(*args, **kwargs):
        recorder.bump("openai_client_constructions")
        doc = _DOC.get()
        if doc is not None:
            doc["client_ctors"] += 1
        t0 = time.perf_counter()
        client = real_cls(*args, **kwargs)
        recorder.add_seconds("openai_construct_s", time.perf_counter() - t0)
        client.chat.completions.create = mock._create  # same instance-level seam as run_pilot --real
        return client

    return factory


def _counting_chat_openai(real_cls, mock: BenchMock, recorder: Recorder):
    """Factory for ``langchain_agents.base_agent.ChatOpenAI``: count, build the real one, return the fake."""

    def factory(*args, **kwargs):
        recorder.bump("langchain_client_constructions")
        doc = _DOC.get()
        if doc is not None:
            doc["client_ctors"] += 1
        t0 = time.perf_counter()
        real_cls(*args, **kwargs)  # pays the construction cost (HTTP clients); the instance is dropped
        recorder.add_seconds("langchain_construct_s", time.perf_counter() - t0)
        return mock.make_langchain_llm()

    return factory


def _install_patches(stack, mock: BenchMock, recorder: Recorder) -> None:
    """Counting wrappers around unmodified pipeline functions, plus the LLM seams."""
    from unittest.mock import patch

    import asyncio

    import graph.build_graph as bg
    import storage.db as sdb
    from langchain_agents import base_agent as lc_base
    from llm import client as llm_client

    orig_bounded = bg._bounded
    orig_heartbeat = bg._touch_heartbeat
    orig_run_coro = bg._run_coro
    orig_asyncio_run = asyncio.run
    orig_engine = sdb.create_async_engine
    orig_pragmas = sdb._apply_sqlite_pragmas

    def timed_bounded(fn):
        inner = orig_bounded(fn)
        name = getattr(fn, "__name__", "node").removesuffix("_node")

        @functools.wraps(inner)
        def wrapper(state):
            frame = {"llm_s": 0.0, "llm_calls": 0, "hb_s": 0.0}
            token = _FRAME.set(frame)
            t0 = time.perf_counter()
            try:
                return inner(state)
            finally:
                wall = time.perf_counter() - t0
                _FRAME.reset(token)
                recorder.node(name, wall, frame)

        return wrapper

    def timed_heartbeat(state):
        recorder.bump("heartbeat_calls")
        doc, frame = _DOC.get(), _FRAME.get()
        t0 = time.perf_counter()
        try:
            return orig_heartbeat(state)
        finally:
            dt = time.perf_counter() - t0
            recorder.add_seconds("heartbeat_s", dt)
            if frame is not None:
                frame["hb_s"] += dt
            if doc is not None:
                doc["heartbeats"] += 1
                doc["heartbeat_s"] += dt

    def counted_run_coro(coro):
        recorder.bump("run_coro_calls")
        return orig_run_coro(coro)

    def counted_asyncio_run(*args, **kwargs):
        recorder.bump("asyncio_run_calls")
        return orig_asyncio_run(*args, **kwargs)

    def counted_engine(*args, **kwargs):
        recorder.bump("db_engines_created")
        return orig_engine(*args, **kwargs)

    def counted_pragmas(*args, **kwargs):
        recorder.bump("db_connections_opened")
        doc = _DOC.get()
        if doc is not None:
            doc["db_connections"] += 1
        return orig_pragmas(*args, **kwargs)

    stack.enter_context(patch.object(bg, "_bounded", timed_bounded))
    stack.enter_context(patch.object(bg, "_touch_heartbeat", timed_heartbeat))
    stack.enter_context(patch.object(bg, "_run_coro", counted_run_coro))
    stack.enter_context(patch.object(asyncio, "run", counted_asyncio_run))
    stack.enter_context(patch.object(sdb, "create_async_engine", counted_engine))
    stack.enter_context(patch.object(sdb, "_apply_sqlite_pragmas", counted_pragmas))
    stack.enter_context(
        patch.object(llm_client, "OpenAI", _counting_openai(llm_client.OpenAI, mock, recorder))
    )
    stack.enter_context(
        patch.object(lc_base, "ChatOpenAI", _counting_chat_openai(lc_base.ChatOpenAI, mock, recorder))
    )


def _cpu_times() -> tuple[float, float] | None:
    """``(user_s, system_s)`` consumed by this process so far; None where ``resource`` is missing."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime, usage.ru_stime
    except (ImportError, OSError):
        return None


def cpu_report(before, after, wall_s: float) -> dict | None:
    """Process CPU seconds spent during a level and the average number of cores kept busy."""
    if before is None or after is None:
        return None
    user, system = after[0] - before[0], after[1] - before[1]
    return {
        "user_s": user,
        "system_s": system,
        "cores_busy": (user + system) / wall_s if wall_s > 0 else None,
    }


def _shorten_retry_delays(base_delay_s: float) -> None:
    """Shrink the taxonomy ``llm_retry`` backoff in this process (both retry layers read it)."""
    from pipeline.config import load_config

    block = load_config().setdefault("llm_retry", {})
    block["base_delay"] = base_delay_s
    block["rate_limit_base_delay"] = base_delay_s
    block["max_delay"] = max(base_delay_s * 8, base_delay_s)


def _run_documents(bins, corpus, concurrency: int, run_pipeline, recorder: Recorder):
    """Run ``corpus`` through ``run_pipeline`` with ``concurrency`` workers; return (records, wall_s)."""
    from concurrent.futures import ThreadPoolExecutor

    matters = [f"BENCH-M{i}" for i in range(1, 5)]
    records: list[dict] = []
    lock = threading.Lock()

    def one(index_name: tuple[int, str]) -> None:
        index, name = index_name
        doc = {
            "llm_s": 0.0, "llm_calls": 0, "rate_limited": 0, "client_ctors": 0,
            "heartbeats": 0, "heartbeat_s": 0.0, "db_connections": 0,
        }
        token = _DOC.set(doc)
        t0 = time.perf_counter()
        stage, error = None, None
        try:
            claimed = bins.claim_file(bins.inbox_dir() / name, f"bench{index}")
            result = run_pipeline(claimed, matters[index % len(matters)])
            stage = result.get("stage")
            if stage == "failed":  # run_pipeline reports an aborted run by returning it, not raising
                message = result.get("error_message")
                error = str(message)[:_ERROR_TEXT_CHARS] if message else None
        except Exception as exc:  # noqa: BLE001 - a raising document is a result, not a crash
            stage, error = "raised", f"{type(exc).__name__}: {str(exc)[:_ERROR_TEXT_CHARS]}"
        finally:
            latency = time.perf_counter() - t0
            _DOC.reset(token)
        with lock:
            records.append({"id": name, "stage": stage, "latency_s": latency, "error": error, **doc})

    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bench") as pool:
        list(pool.map(one, enumerate(n for n, _t, _m in corpus)))
    return records, time.perf_counter() - t_start


def worker_level(cfg: dict) -> dict:
    """Benchmark one concurrency level in this process. Returns the level report."""
    from contextlib import ExitStack

    from scripts.stress_test import _ensure_bins, _stub_network_side_effects, _write_corpus

    _stub_network_side_effects()
    recorder = Recorder()
    mock = BenchMock(
        recorder,
        rate_limit_rate=cfg["rate_limit_rate"],
        seed=cfg["seed"],
        latency_s=cfg["latency_ms"] / 1000.0,
    )
    _shorten_retry_delays(cfg["retry_base_delay_ms"] / 1000.0)
    bins = _ensure_bins()
    from graph.build_graph import get_compiled_graph, reset_compiled_graph, run_pipeline

    with ExitStack() as stack:
        _install_patches(stack, mock, recorder)
        reset_compiled_graph()  # the graph must be built with the timing wrappers in place
        get_compiled_graph()
        if cfg["warmup_docs"] > 0:  # imports, schema creation, first-use caches stay out of the numbers
            warm = make_corpus(cfg["warmup_docs"], cfg["seed"] + 10_000, long_docs=0, clean=True)
            _write_corpus(warm)
            _run_documents(bins, warm, 1, run_pipeline, recorder)
            recorder.reset()
        corpus = make_corpus(cfg["docs"], cfg["seed"], cfg.get("long_docs"), clean=cfg["clean"])
        _write_corpus(corpus)
        cpu_before = _cpu_times()
        records, wall = _run_documents(bins, corpus, cfg["concurrency"], run_pipeline, recorder)
        cpu_after = _cpu_times()
        snapshot = recorder.snapshot()
    reset_compiled_graph()
    report = build_level_report(cfg["concurrency"], records, wall, snapshot)
    report["long_docs"] = sum(1 for _n, _t, m in corpus if m["kind"] == "long")
    report["cpu"] = cpu_report(cpu_before, cpu_after, wall)
    return report


def worker_main(cfg: dict, out: Path) -> int:
    try:
        import dojo_source  # noqa: F401  (installed Dojo first, vendored fallback)
    except Exception:  # noqa: BLE001
        pass
    try:
        result = {"ok": True, "level": worker_level(cfg)}
    except BaseException as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]}
    out.write_text(json.dumps(result, default=str))
    sys.stdout.flush()
    os._exit(0)  # aiosqlite and watcher threads must not hold the process open


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------


# Variables that tell git which repository to read; inherited from a hook or wrapper they
# would make the provenance describe some other repo than ``REPO_ROOT``.
_GIT_REPO_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def _git(*args: str) -> str | None:
    """Run git in the repo; ``None`` when the command fails (unverifiable, not empty)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_ENV}
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30
        )
    except Exception:  # noqa: BLE001
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _untracked_digest(listing: str) -> str | None:
    """Hash the names and bytes of untracked files; ``None`` if one cannot be read."""
    h = hashlib.sha256()
    for name in sorted(n for n in listing.split("\0") if n):
        try:
            data = (REPO_ROOT / name).read_bytes()
        except OSError:
            return None
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def git_state() -> dict:
    """The commit and working-tree content the benchmark is measuring.

    Any failed git call makes the whole observation unverifiable (``commit`` is
    empty), which ``provenance_changed`` treats as changed.
    """
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    diff = _git("diff", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    digest = _untracked_digest(untracked) if untracked is not None else None
    if None in (commit, status, diff, digest):
        return {"commit": "", "tree_clean": None, "tree_fingerprint": None}
    # Fingerprint tracked changes and untracked file contents, so a dirty tree that
    # changes while the levels run (and stays dirty) is still caught.
    fingerprint = hashlib.sha256(f"{status}\n{diff}\n{digest}".encode()).hexdigest()
    return {"commit": commit, "tree_clean": status == "", "tree_fingerprint": fingerprint}


def provenance_changed(before: dict, after: dict) -> bool:
    """True when HEAD or the working tree moved while the levels ran, or either side is unknown."""
    keys = ("commit", "tree_clean", "tree_fingerprint")
    if not before.get("commit") or not after.get("commit"):
        return True  # unverifiable provenance counts as changed
    return any(before.get(k) != after.get(k) for k in keys)


def machine_notes() -> dict:
    load = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    mem_gb = None
    try:
        mem_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, ValueError, OSError):
        pass
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_gb": mem_gb,
        "load_average_at_start": load,
    }


def run_level(cfg: dict, args) -> dict:
    """Run one level in a fresh subprocess and fresh base dir; return its report (or a failure stub)."""
    root = Path(tempfile.mkdtemp(prefix=f"bench-c{cfg['concurrency']}-", dir=args.workdir)).resolve()
    base = root / "base"
    base.mkdir()
    out = root / "result.json"
    log = root / "worker.log"
    env = _worker_env(
        str(base), args.log_level, {"OPENROUTER_BASE_URL": DISCARD_BASE_URL, "MAILROOM_BASE_DIR": str(base)}
    )
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--cfg", json.dumps(cfg), "--out", str(out)]
    try:
        with open(log, "wb") as fh:
            proc = subprocess.run(
                cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=args.timeout, cwd=str(REPO_ROOT)
            )
        if out.exists():
            result = json.loads(out.read_text())
        else:
            tail = log.read_bytes()[-1500:].decode(errors="replace")
            result = {"ok": False, "error": f"worker exited {proc.returncode} without a result", "traceback": tail}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": f"worker timed out after {args.timeout}s"}
    if args.keep:
        print(f"kept {root}", file=sys.stderr)
    else:
        shutil.rmtree(root, ignore_errors=True)
    if not result.get("ok"):
        print(f"! concurrency {cfg['concurrency']} failed: {result.get('error')}", file=sys.stderr)
        if result.get("traceback"):
            print(result["traceback"], file=sys.stderr)
        return {"concurrency": cfg["concurrency"], "error": result.get("error")}
    return result["level"]


def positive_int_list(text: str) -> list[int]:
    try:
        values = [int(x) for x in text.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {text!r}") from exc
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError(f"every level must be >= 1, got {text!r}")
    return values


def unit_fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number in [0, 1], got {text!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"expected a number in [0, 1], got {text!r}")
    return value


def _finite_float(text: str, flag_hint: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected {flag_hint}, got {text!r}") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"expected {flag_hint}, got {text!r}")
    return value


def non_negative_float(text: str) -> float:
    """A finite number >= 0 (latency and backoff milliseconds; 0 disables the sleep)."""
    value = _finite_float(text, "a finite number >= 0")
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a finite number >= 0, got {text!r}")
    return value


def positive_float(text: str) -> float:
    """A finite number > 0 (the per-level timeout in seconds)."""
    value = _finite_float(text, "a finite number > 0")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a finite number > 0, got {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--levels", type=positive_int_list, default=list(DEFAULT_LEVELS),
                    help="comma-separated concurrency levels (default 1,4,16,64)")
    ap.add_argument("--docs", type=int, default=128, help="documents per level (default 128)")
    ap.add_argument("--latency-ms", type=non_negative_float, default=200.0, help="injected latency per LLM call (default 200)")
    ap.add_argument("--rate-limit-rate", type=unit_fraction, default=0.0,
                    help="fraction of LLM calls answered with an injected 429 (default 0)")
    ap.add_argument("--retry-base-delay-ms", type=non_negative_float, default=50.0,
                    help="retry backoff base used instead of the taxonomy 1 s / 8 s (default 50)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--clean", action="store_true",
                    help="only confident single-window documents (default mix: ~10%% ambiguous, ~3%% long, ~2%% blank)")
    ap.add_argument("--long-docs", type=int, default=None, help="override the number of ~230k-char documents")
    ap.add_argument("--warmup-docs", type=int, default=1, help="documents run and discarded before measuring (default 1)")
    ap.add_argument("--json-out", default=None, help="write the full report as JSON")
    ap.add_argument("--md-out", default=None, help="write the markdown tables")
    ap.add_argument("--timeout", type=positive_float, default=900.0, help="per-level timeout in seconds (default 900)")
    ap.add_argument("--workdir", default=None, help="parent directory for scratch dirs (default system temp)")
    ap.add_argument("--keep", action="store_true", help="keep scratch dirs (worker logs, DBs)")
    ap.add_argument("--log-level", default="CRITICAL", help="LOG_LEVEL for workers (default CRITICAL)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--cfg", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out", default=None, help=argparse.SUPPRESS)
    return ap


def recorded_command(argv: list[str]) -> str:
    """The command line that reproduces this run, naming the interpreter that ran it.

    An interpreter inside the repository (a ``.venv``) is recorded relative to the repo root;
    any other interpreter is recorded as plain ``python`` so no local path leaks into reports.
    """
    try:
        interpreter = Path(sys.executable).relative_to(REPO_ROOT).as_posix()
    except ValueError:
        interpreter = "python"
    return f"PYTHONPATH=src {interpreter} src/scripts/bench_throughput.py " + " ".join(argv)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        return worker_main(json.loads(args.cfg), Path(args.out))
    if args.docs < 1:
        print("--docs must be >= 1", file=sys.stderr)
        return 2
    base_cfg = {
        "docs": args.docs,
        "latency_ms": args.latency_ms,
        "rate_limit_rate": args.rate_limit_rate,
        "retry_base_delay_ms": args.retry_base_delay_ms,
        "seed": args.seed,
        "clean": args.clean,
        "long_docs": args.long_docs,
        "warmup_docs": args.warmup_docs,
    }
    # Provenance is observed before the first level so it describes what is measured; it is
    # re-checked after the last level and the report says if it moved in between.
    git_before = git_state()
    machine = machine_notes()
    started = time.time()
    levels = []
    for c in args.levels:
        print(f"== concurrency {c}: {args.docs} docs ...", file=sys.stderr, flush=True)
        levels.append(run_level({**base_cfg, "concurrency": c}, args))
    git_after = git_state()
    changed = provenance_changed(git_before, git_after)
    if changed:
        print("! provenance changed while the levels ran (HEAD or working tree moved)", file=sys.stderr)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "graph-throughput-benchmark",
        "mock_llm": True,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "command": recorded_command(argv if argv is not None else sys.argv[1:]),
        **git_before,
        "provenance_changed": changed,
        **({"provenance_after": git_after} if changed else {}),
        "machine": machine,
        "config": {**base_cfg, "levels": args.levels},
        "levels": levels,
    }
    ok_levels = [lv for lv in levels if "error" not in lv]
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, default=str) + "\n")
    markdown = render_markdown({**report, "levels": ok_levels}) if ok_levels else ""
    if args.md_out:
        Path(args.md_out).write_text(markdown)
    print(markdown, end="")
    if changed:
        return 3  # results are written for inspection, but the run is not a valid measurement
    return 0 if len(ok_levels) == len(levels) else 1


if __name__ == "__main__":
    raise SystemExit(main())
