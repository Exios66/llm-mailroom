#!/usr/bin/env python3
"""Scale hill-climb evaluator: hermetic volume + fault-injection harness.

Runs the REAL pipeline code (graph, bins, catalog, audit log, watcher) against
a deterministic fake LLM, under volume and under injected faults, and prints a
scale scorecard plus exactly one final ``SCORE: 0.XXXX`` line. No network, no
real LLM, no tracing backend: every scenario runs in its own subprocess with a
fresh temporary ``MAILROOM_BASE_DIR`` (``DATABASE_URL`` pinned to its scratch SQLite file), so it can
never touch ``data/`` or a hosted service. It uses only APIs that exist on
``main`` today and feature-detects the rest (``MAILROOM_MAX_CONCURRENT_DOCS``
is exported but may be ignored by older code), so the same command measures the
baseline and every later fix.

Usage::

    PYTHONPATH=src .venv/bin/python src/scripts/stress_test.py
    PYTHONPATH=src .venv/bin/python src/scripts/stress_test.py --docs 120 \
        --threads 1,8,32 --json out.json
    PYTHONPATH=src .venv/bin/python src/scripts/stress_test.py --docs 6 \
        --threads 1,2 --scenarios throughput,integrity

Scenarios (``--scenarios``, comma list, default all)
----------------------------------------------------
``throughput``
    ``--docs`` synthetic documents (5 classes, ~10% low-confidence, ~2% blank,
    ~3% long multi-window) are claimed and run through ``run_pipeline`` at each
    ``--threads`` count. The mock LLM sleeps ``--mock-latency-ms`` per call
    (default 50) to model provider I/O overlap. Reports docs/min, p50/p95/max
    per-document latency and terminal-state counts.
``integrity``
    Evaluated by every worker after its run (so after ``throughput`` and after
    every other scenario). Metrics, each summed across workers:

    * ``lost_docs``: input documents that are not accounted for. An input is
      lost if the catalog has no row with a terminal stage
      (``archived``/``review``/``failed``) for its filename, OR its file is not
      in the archive/review/failed bins (still in inbox/processing, or gone).
    * ``duplicate_catalog_rows``: extra catalog rows beyond one per input
      filename. ``duplicate_sha_rows``: extra rows sharing a ``file_sha256``
      under different filenames.
    * ``duplicate_chains``: extra distinct ``doc_id`` audit chains per input
      filename (a re-run that minted a second identity).
    * ``orphan_processing_rows``: catalog rows still ``stage=processing`` after
      the run has drained.
    * ``broken_chains``: audit chains that fail ``schemas.audit.verify_chain``
      (the same verifier ``scripts/verify_audit_chains.py`` uses) or contain a
      duplicate ``seq``.
    * ``lost_audit_events``: for every catalog row in a terminal stage, the
      required events that are absent from that ``doc_id``'s chain, summed.
      Required events by terminal stage (event names as written by
      ``graph/build_graph.py`` and ``agents/archivist.py``):

      - ``archived``: ``ingested``, ``classified``, ``extracted``, ``archived``
      - ``review``:   ``ingested``, ``routed_to_review``
      - ``failed``:   ``run_aborted``

      A terminal row with NO chain at all counts every required event.
``watcher_backpressure``
    ``--watcher-files`` files (default ``--docs``) are dropped into the inbox
    of a live ``Watcher`` whose ``run_pipeline`` is replaced by a slow stub
    (``--watcher-run-ms``). Reports peak concurrent pipelines, peak threads,
    and whether each file was run exactly once. ``MAILROOM_MAX_CONCURRENT_DOCS``
    (``--cap``, default 4) is exported; ``cap_honored`` = ``peak <= cap``
    (measured, so old code that ignores the variable reports False).
``stale_claim``
    A backlogged inbox file (mtime 2 h old) is claimed by the watcher and its
    pipeline is held in flight while the periodic stale-claim reconcile runs.
    ``inflight_hijacked`` counts claimed files moved out from under a live run
    (or re-queued to the inbox). A genuinely orphaned claim (dead worker) must
    still be recovered: ``orphans_unrecovered``.
``crash_recovery``
    The pipeline is killed (``BaseException``, like SIGKILL) after intake,
    after classify and after extract; the stale claim is reconciled and the
    file re-run through the watcher's real path. Reports duplicate catalog
    rows, duplicate chains, orphan ``processing`` rows, duplicate manifests.
``db_contention``
    Mixed catalog/audit writers (the 5-step document write pattern) at each
    ``--db-threads`` count (default 32,128). ``failed_ops`` must be 0.
``provider_faults``
    The mock LLM raises ``openai`` connection errors / 429s at rate
    ``--fault-rate`` (default 0.2, decided by hash(seed, doc, call index), so
    per-doc faults are deterministic) plus a full outage of ``--outage-calls``
    consecutive calls. Retry backoff sleeps are recorded and collapsed to ~0.
    Reports retries made, docs parked to review for transient reasons, docs
    failed and ``transient_review_share``.

SCORE (in [0, 1])
-----------------
Integrity is a hard gate. Gates (each prints ``GATE <name> PASS|FAIL``):
``lost_docs``, ``duplicate_catalog_rows``, ``duplicate_sha_rows``,
``duplicate_chains``, ``orphan_processing_rows``, ``broken_chains``,
``lost_audit_events``, ``inflight_hijacked``, ``orphans_unrecovered``,
``db_failed_ops``, ``watcher_exactly_once``, ``crash_injected`` (every
crash_recovery document actually died at its injection point),
``integrity_measured`` (the integrity scenario was selected but no worker
produced an integrity measurement), ``scenario_error`` (a worker crashed or
timed out). A gate is only evaluated when the scenario that feeds it
ran; the integrity gates are also evaluated whenever any worker measured
integrity, even if ``integrity`` was not selected. If any gate fails::

    SCORE = 0

otherwise::

    SCORE = 0.5 * min(1, docs_per_min@8 / target_dpm)
          + 0.3 * (1 - transient_review_share)
          + 0.2 * cap_honored

``docs_per_min@8`` is the throughput at 8 threads (nearest measured thread
count if 8 was not run). ``target_dpm`` is ``--target-dpm`` (default 600).
A component whose scenario did not run contributes 0 and is marked
"not measured". Exit status is 0 when every gate passes, 2 otherwise. The last
line printed is exactly ``SCORE: 0.XXXX``.

Determinism: document mix, texts, per-document fault decisions and the seed-
derived ordering are fixed by ``--seed``. NOT deterministic: wall-clock
throughput, thread interleaving, which documents the outage window hits
(call-count based), and the races the scenarios exist to expose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent

ALL_SCENARIOS = (
    "throughput",
    "integrity",
    "watcher_backpressure",
    "stale_claim",
    "crash_recovery",
    "db_contention",
    "provider_faults",
)

TERMINAL_STAGES = ("archived", "review", "failed")
REQUIRED_EVENTS = {
    "archived": ("ingested", "classified", "extracted", "archived"),
    "review": ("ingested", "routed_to_review"),
    "failed": ("run_aborted",),
}

# Classes that exist in the taxonomy and have a specialist (extractable).
DOC_CLASSES = (
    "contract",
    "correspondence",
    "insurance_claim",
    "corporate_record",
    "merger_agreement",
)

MARKER_RE = re.compile(r"\[\[stress id=(\S+) cls=(\S+) conf=([0-9.]+) kind=(\w+)\]\]")

SECRET_ENV = (
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_BASE_URL",
    "BRAINTRUST_API_KEY",
    "PHOENIX_API_KEY",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "DATABASE_URL",
    "MAILROOM_LLM_FREE_ONLY",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
)


# ---------------------------------------------------------------------------
# Small helpers (no project imports: the parent process stays light)
# ---------------------------------------------------------------------------


def _u(seed: int, *parts) -> float:
    """Deterministic uniform [0, 1) from (seed, parts)."""
    digest = hashlib.blake2b(":".join(map(str, (seed, *parts))).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def _stable_index(key: str, modulo: int) -> int:
    """Process-independent ``key -> [0, modulo)`` (``hash()`` is randomized per process)."""
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big") % modulo


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _worker_env(base_dir: str, log_level: str, extra: dict | None = None) -> dict:
    """Environment for a worker: hermetic, no tracing, no secrets, fresh base."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(
        {
            "MAILROOM_BASE_DIR": base_dir,
            # Pin the catalog to this worker's scratch SQLite file. Dropping
            # DATABASE_URL above is not enough: project modules call load_env()
            # on import, which would re-read it from the repository .env.
            "DATABASE_URL": f"sqlite+aiosqlite:///{Path(base_dir) / 'mailroom.db'}",
            "OBSERVABILITY_PROVIDER": "none",
            "OPENROUTER_API_KEY": "test-key-not-real",
            "MAILROOM_DOCCLASS_PROMPTS": "0",
            "MAILROOM_GMAIL_ENABLED": "0",
            "MAILROOM_LLM_INTAKE": "0",
            "MAILROOM_PROVIDER_HEALTH": "0",
            "MAILROOM_BERT_INTAKE": "0",
            "MAILROOM_RELATIONS": "0",
            "MAILROOM_WATCHER_STATUS": "0",
            "MAILROOM_WATCHDOG": "0",
            "MAILROOM_LLM_CACHE": "0",
            "MAILROOM_RELATIONS_EMBEDDINGS": "0",
            "MAILROOM_EMBED_WATCHER": "0",
            "LOG_LEVEL": log_level,
            "PYTHONPATH": str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------


def _class_bodies() -> dict[str, list[str]]:
    """Per-class body texts: repo fixtures when present, else a generic body."""
    bodies: dict[str, list[str]] = {}
    fx = SRC_DIR / "tests" / "fixtures"
    for cls in DOC_CLASSES:
        texts = []
        for p in sorted((fx / cls).glob("*.txt")) if (fx / cls).is_dir() else []:
            if "ambiguous" in p.name:
                continue
            texts.append(p.read_text(errors="replace"))
        if not texts:
            texts = [
                f"{cls.upper().replace('_', ' ')}\n\n"
                + "This document records terms between Acme Corporation and Zenith LLC dated "
                "January 15, 2024. " * 12
            ]
        bodies[cls] = texts
    return bodies


def make_corpus(n: int, seed: int, long_docs: int | None = None, long_chars: int = 230_000, clean: bool = False):
    """Return ``[(filename, text, meta)]`` for ``n`` documents (``clean``: all ``ok``).

    ``meta`` = {id, cls, conf, kind}; ``kind`` in ok / ambiguous / blank / long.
    The mock LLM reads the ``[[stress ...]]`` marker embedded in the text (once
    per ~2.6 KB, so every sliding window carries it).
    """
    rng = random.Random(seed)
    bodies = _class_bodies()
    n_long = long_docs if long_docs is not None else (max(1, round(n * 0.03)) if n >= 20 else 0)
    n_amb = round(n * 0.10) if n >= 10 else 0
    n_blank = round(n * 0.02) if n >= 50 else 0
    if clean:  # only confident, single-window documents: any non-archived outcome is a finding
        n_long = n_amb = n_blank = 0
    kinds = ["long"] * n_long + ["ambiguous"] * n_amb + ["blank"] * n_blank
    kinds += ["ok"] * max(0, n - len(kinds))
    kinds = kinds[:n]
    rng.shuffle(kinds)
    corpus = []
    for i, kind in enumerate(kinds):
        cls = "contract" if kind == "long" else DOC_CLASSES[i % len(DOC_CLASSES)]
        # "ok" confidences sit above every class's `high` threshold (max 0.98).
        conf = 0.40 if kind == "ambiguous" else round(0.985 + (i % 3) * 0.005, 3)
        doc_id = f"s{seed}d{i:05d}"
        marker = f"[[stress id={doc_id} cls={cls} conf={conf} kind={kind}]]"
        body = bodies[cls][i % len(bodies[cls])]
        if kind == "blank":
            text = f"   \n\n  \n{' ' * (i + 1)}\n"  # whitespace only, distinct bytes (and sha256) per file
        elif kind == "long":
            unit = f"{body}\n{marker}\nUnique clause {doc_id}.\n"
            text = unit * max(2, long_chars // max(1, len(unit)))
        else:
            text = f"{body}\n{marker}\nUnique reference {doc_id} {rng.random():.8f}.\n"
        corpus.append((f"{doc_id}_{cls}_{kind}.txt", text, {"id": doc_id, "cls": cls, "conf": conf, "kind": kind}))
    return corpus


# ---------------------------------------------------------------------------
# Worker side: everything below runs in a child process
# ---------------------------------------------------------------------------


class ScaleMock:
    """Deterministic fake LLM shared by the OpenAI-style client path and the
    vendored LangChain path.

    Responses are keyed off the ``[[stress ...]]`` marker in the document text.
    Faults are decided per (seed, doc id, per-doc call index), so they do not
    depend on thread interleaving; the outage window is call-count based.
    """

    def __init__(self, seed=0, latency_s=0.0, fault_rate=0.0, outage_start=None, outage_calls=0, real_sleep=None):
        import threading
        from collections import Counter

        self.seed = seed
        self.latency_s = latency_s
        self.fault_rate = fault_rate
        self.outage_start = outage_start
        self.outage_calls = outage_calls
        self.real_sleep = real_sleep or time.sleep
        self.lock = threading.Lock()
        self.calls_total = 0
        self.faults_injected = 0
        self.outage_faults = 0
        self.per_doc = Counter()

    # -- shared -----------------------------------------------------------
    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ""

    @staticmethod
    def _meta(text: str) -> dict:
        found = MARKER_RE.findall(text)
        if not found:
            return {"id": None, "cls": "contract", "conf": 0.97, "kind": "ok"}
        doc_id, cls, conf, kind = found[-1]
        return {"id": doc_id, "cls": cls, "conf": float(conf), "kind": kind}

    def _pre_call(self, meta: dict) -> None:
        import httpx
        import openai

        key = meta["id"] or "anon"
        with self.lock:
            self.calls_total += 1
            g = self.calls_total
            self.per_doc[key] += 1
            k = self.per_doc[key]
        in_outage = (
            self.outage_start is not None and self.outage_start <= g < self.outage_start + self.outage_calls
        )
        random_fault = self.fault_rate > 0 and _u(self.seed, "fault", key, k) < self.fault_rate
        if in_outage or random_fault:
            with self.lock:
                self.faults_injected += 1
                if in_outage:
                    self.outage_faults += 1
            req = httpx.Request("POST", "https://mock.invalid/v1/chat/completions")
            if _u(self.seed, "type", key, k) < 0.5:
                raise openai.APIConnectionError(request=req)
            raise openai.RateLimitError(
                "rate limited (injected)",
                response=httpx.Response(429, request=req, headers={"Retry-After": "1"}),
                body=None,
            )
        if self.latency_s > 0:
            self.real_sleep(self.latency_s)

    _subclass_cache: dict = {}

    @classmethod
    def _subclass(cls, doc_cls: str):
        """A valid sorter subclass (the classification guard requires one)."""
        if doc_cls not in cls._subclass_cache:
            try:
                from langchain_agents.doc_inventories import valid_sorter_subclasses

                allowed = sorted(valid_sorter_subclasses(doc_cls))
            except Exception:  # noqa: BLE001
                allowed = []
            cls._subclass_cache[doc_cls] = "other" if "other" in allowed else (allowed[0] if allowed else None)
        return cls._subclass_cache[doc_cls]

    @classmethod
    def _classification(cls, meta: dict) -> dict:
        out = {
            "doc_type": meta["cls"],
            "contract_subtype": "other" if meta["cls"] == "contract" else None,
            "confidence": meta["conf"],
            "reasoning": "mock",
        }
        out["doc_subclass"] = "other" if meta["cls"] == "contract" else cls._subclass(meta["cls"])
        return out

    @staticmethod
    def _extraction(meta: dict) -> dict:
        return {"confidence": meta["conf"], "document_name": "Mock Agreement", "parties": ["Mock Party"]}

    # -- OpenAI-style client ---------------------------------------------
    def make_client(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.base_url = "https://mock.invalid/v1"
        client.chat.completions.create.side_effect = self._create
        return client

    def _create(self, **kwargs):
        from unittest.mock import MagicMock

        msgs = kwargs.get("messages") or []
        texts = [self._text_of(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in msgs]
        meta = self._meta("\n".join(texts))
        self._pre_call(meta)
        last = texts[-1] if texts else ""
        if "Classify this legal document" in last or "RE-EVALUATION REQUESTED" in last or "CLASSIFY THIS DOCUMENT" in last:
            content = json.dumps(self._classification(meta))
        elif "ADJUDICATION REQUEST" in last:
            content = json.dumps({"decision": "approved", "reasoning": "mock", "resolution_notes": ""})
        else:
            content = json.dumps(self._extraction(meta))
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        return resp

    # -- vendored LangChain path -------------------------------------------
    def make_langchain_llm(self):
        from langchain_agents.mock import FakeLangChainLLM, is_classify_call, user_text_from_messages

        owner = self

        class _Fake(FakeLangChainLLM):
            def _run(self, messages):
                text = user_text_from_messages(messages)
                meta = owner._meta(text)
                owner._pre_call(meta)
                parsed = owner._classification(meta) if is_classify_call(text) else owner._extraction(meta)
                return self._make_message(parsed)

        return _Fake()


def _start_llm_patches(mock: ScaleMock):
    """Patch every LLM entry point to the mock (same seams as run_pilot --mock)."""
    from unittest.mock import patch

    from langchain_agents.base_agent import BaseAgent as LCBase

    client = mock.make_client()
    fake = mock.make_langchain_llm()
    patches = [
        patch("llm.client.get_llm", side_effect=lambda name: (client, "mock-model")),
        patch("agents.base.get_llm", side_effect=lambda name: (client, "mock-model")),
        patch.object(LCBase, "llm", new=lambda self: fake),
    ]
    for p in patches:
        p.start()
    return patches


def _stub_network_side_effects() -> None:
    """Keep the watcher/graph imports from starting a model download thread."""
    import llm_dojo_scoring

    llm_dojo_scoring.warm_embedding_model = lambda blocking=False: None


def _ensure_bins():
    from pipeline import bins

    bins.ensure_dirs(
        bins.inbox_dir(),
        bins.processing_dir(),
        bins.review_dir(),
        bins.failed_dir(),
        bins.archive_dir(),
        bins.manifests_dir(),
    )
    return bins


def _write_corpus(corpus) -> None:
    from pipeline import bins

    for name, text, _meta in corpus:
        (bins.inbox_dir() / name).write_text(text)


# -- integrity ---------------------------------------------------------------


def _manifest_index() -> dict[str, list[tuple[str, str | None]]]:
    """``original_filename -> [(doc_id, stage)]`` from the saved manifests.

    An unreadable or malformed manifest raises (it reaches ``scenario_error``
    and rejects the score) rather than being skipped and undercounting.
    """
    from pipeline import bins

    index: dict[str, list[tuple[str, str | None]]] = {}
    for mf in sorted(bins.manifests_dir().glob("*.json")):
        data = json.loads(mf.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"manifest root must be a JSON object: {mf.name}")
        index.setdefault(data.get("original_filename"), []).append((data.get("doc_id") or mf.stem, data.get("stage")))
    return index

_UUID_SUFFIX = re.compile(r"--[0-9a-fA-F-]{8,}(?=\.[^.]+$|$)")


def check_integrity(inputs: dict[str, dict] | None) -> dict:
    """Post-drain integrity audit. ``inputs`` maps filename -> meta, or None to
    audit catalog/audit-log consistency only (no input accounting)."""
    import asyncio
    from collections import Counter, defaultdict

    from pipeline import bins
    from schemas.audit import AuditLogEntry, verify_chain
    from storage.audit_log import get_audit_chain, list_audit_doc_ids
    from storage.catalog import list_documents

    async def _gather():
        rows = await list_documents()
        doc_ids = await list_audit_doc_ids()
        chains = {d: await get_audit_chain(d) for d in doc_ids}
        return rows, chains

    rows, chains = asyncio.run(_gather())

    by_name = defaultdict(list)
    by_sha = defaultdict(set)
    for r in rows:
        by_name[r.original_filename].append(r)
        if r.file_sha256:
            by_sha[r.file_sha256].add(r.original_filename)
    sha_counts = Counter(r.file_sha256 for r in rows if r.file_sha256)

    # files by terminal bin
    terminal_names: set[str] = set()
    for d in (bins.archive_dir(), bins.review_dir(), bins.failed_dir()):
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file():
                    terminal_names.add(p.name)
                    terminal_names.add(_UUID_SUFFIX.sub("", p.name))
    stuck_inbox = len([p for p in bins.inbox_dir().glob("*") if p.is_file() and p.suffix != ".meta"])
    proc = bins.processing_dir()
    stuck_processing = len([p for p in proc.rglob("*") if p.is_file()]) if proc.exists() else 0

    lost_catalog = lost_files = lost = 0
    duplicate_rows = duplicate_chains = 0
    manifest_ids = _manifest_index() if inputs is not None else {}
    if inputs is not None:
        for name in inputs:
            recs = by_name.get(name, [])
            terminal = [r for r in recs if r.stage in TERMINAL_STAGES]
            miss_cat = not terminal
            miss_file = name not in terminal_names
            lost_catalog += miss_cat
            lost_files += miss_file
            lost += miss_cat or miss_file
            duplicate_rows += max(0, len(recs) - 1)
            # Chains are tied to an input through the catalog row AND the saved
            # manifest, so a retry whose catalog row lacks the filename (the
            # catalog write is best-effort) still counts as a second chain.
            ids = {r.doc_id for r in recs} | {d for d, _stage in manifest_ids.get(name, [])}
            with_chain = {d for d in ids if chains.get(d)}
            duplicate_chains += max(0, len(with_chain) - 1)
    duplicate_sha = sum(
        max(0, len(rows_with_sha) - 1) for rows_with_sha in by_sha.values() if len(rows_with_sha) > 1
    )
    del sha_counts

    orphan_processing = sum(1 for r in rows if r.stage == "processing")

    broken = 0
    broken_ids = []
    for doc_id, chain in chains.items():
        entries = [
            AuditLogEntry(
                doc_id=doc_id,
                **{k: c[k] for k in ("entry_id", "matter_id", "event", "actor", "detail", "prev_hash", "entry_hash", "timestamp")},
            )
            for c in chain
        ]
        seqs = [c["seq"] for c in chain]
        if (entries and not verify_chain(entries)) or len(seqs) != len(set(seqs)):
            broken += 1
            broken_ids.append(doc_id)

    lost_events = 0
    missing_examples = []
    for r in rows:
        required = REQUIRED_EVENTS.get(r.stage)
        if not required:
            continue
        present = {c["event"] for c in chains.get(r.doc_id, [])}
        missing = [e for e in required if e not in present]
        lost_events += len(missing)
        if missing and len(missing_examples) < 5:
            missing_examples.append({"doc_id": r.doc_id, "stage": r.stage, "missing": missing})

    stage_counts = Counter(r.stage for r in rows)
    return {
        "inputs": len(inputs) if inputs is not None else None,
        "catalog_rows": len(rows),
        "catalog_stage_counts": dict(stage_counts),
        "lost_docs": lost,
        "lost_docs_catalog": lost_catalog,
        "lost_docs_files": lost_files,
        "stuck_in_inbox": stuck_inbox,
        "stuck_in_processing": stuck_processing,
        "duplicate_catalog_rows": duplicate_rows,
        "duplicate_sha_rows": duplicate_sha,
        "duplicate_chains": duplicate_chains,
        "orphan_processing_rows": orphan_processing,
        "broken_chains": broken,
        "lost_audit_events": lost_events,
        "audit_chains": len(chains),
        "audit_entries": sum(len(c) for c in chains.values()),
        "examples": {"missing_events": missing_examples, "broken_chains": broken_ids[:5]},
    }


# -- scenarios ---------------------------------------------------------------


def w_throughput(cfg: dict) -> dict:
    import collections
    import threading

    _stub_network_side_effects()
    mock = ScaleMock(seed=cfg["seed"], latency_s=cfg["latency_ms"] / 1000.0)
    _start_llm_patches(mock)
    bins = _ensure_bins()
    from graph.build_graph import reset_compiled_graph, run_pipeline

    reset_compiled_graph()
    corpus = make_corpus(cfg["docs"], cfg["seed"], cfg.get("long_docs"))
    _write_corpus(corpus)
    inputs = {name: meta for name, _t, meta in corpus}

    queue = collections.deque(name for name, _t, _m in corpus)
    qlock = threading.Lock()
    latencies: list[float] = []
    stages = collections.Counter()
    raised = collections.Counter()
    matters = [f"M-{i}" for i in range(1, 5)]

    def worker(wid: int):
        while True:
            with qlock:
                if not queue:
                    return
                name = queue.popleft()
            t0 = time.perf_counter()
            try:
                claimed = bins.claim_file(bins.inbox_dir() / name, f"w{wid}")
                result = run_pipeline(claimed, matters[_stable_index(name, len(matters))])
                stage = result.get("stage")
            except Exception as exc:  # noqa: BLE001
                stage = "raised"
                with qlock:
                    raised[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
            dt = time.perf_counter() - t0
            with qlock:
                latencies.append(dt)
                stages[stage] += 1

    threads = cfg["threads"]
    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - t0
    n = cfg["docs"]
    return {
        "metrics": {
            "threads": threads,
            "docs": n,
            "elapsed_s": round(elapsed, 3),
            "docs_per_min": round(n / elapsed * 60, 1) if elapsed > 0 else 0.0,
            "p50_s": round(_pct(latencies, 0.50), 3),
            "p95_s": round(_pct(latencies, 0.95), 3),
            "max_s": round(max(latencies) if latencies else 0.0, 3),
            "returned_stage_counts": dict(stages),
            "raised": sum(raised.values()),
            "raised_kinds": dict(raised.most_common(3)),
            "llm_calls": mock.calls_total,
            "long_docs": sum(1 for _n, _t, m in corpus if m["kind"] == "long"),
        },
        "integrity": check_integrity(inputs),
    }


def w_watcher_backpressure(cfg: dict) -> dict:
    import inspect
    import threading
    import uuid

    _stub_network_side_effects()
    bins = _ensure_bins()
    from pipeline import watcher

    supports_cap = "MAILROOM_MAX_CONCURRENT_DOCS" in inspect.getsource(watcher)
    n = cfg["files"]
    run_s = cfg["run_ms"] / 1000.0
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0, "peak_threads": 0, "total_calls": 0}
    calls: dict[str, int] = {}

    def fake_run(claimed, matter_id, **kw):
        name = Path(claimed).name
        with lock:
            state["cur"] += 1
            state["total_calls"] += 1
            calls[name] = calls.get(name, 0) + 1
            state["peak"] = max(state["peak"], state["cur"])
            state["peak_threads"] = max(state["peak_threads"], threading.active_count())
        try:
            time.sleep(run_s)
            if Path(claimed).exists():  # model a terminal move so nothing is re-picked
                bins.move_to_archive(Path(claimed), "M-1", "contract", doc_id=uuid.uuid4().hex[:8])
        finally:
            with lock:
                state["cur"] -= 1
        return {"doc_id": uuid.uuid4().hex, "stage": "archived"}

    watcher.run_pipeline = fake_run
    w = watcher.Watcher()
    w.start()
    t0 = time.perf_counter()
    names = [f"wb{i:05d}.txt" for i in range(n)]
    for name in names:
        (bins.inbox_dir() / name).write_text(f"watcher backpressure {name}")
    deadline = time.time() + cfg["max_wait_s"]
    while time.time() < deadline:
        with lock:
            done = sum(1 for nm in names if calls.get(nm, 0) >= 1) == n and state["cur"] == 0
        if done:
            break
        time.sleep(0.1)
    elapsed = time.perf_counter() - t0
    time.sleep(1.5)  # let a rescan tick expose late duplicate runs
    w.stop(drain_timeout=5)
    with lock:
        counts = [calls.get(nm, 0) for nm in names]
    exactly_once = sum(1 for c in counts if c == 1)
    cap = cfg["cap"]
    return {
        "metrics": {
            "files": n,
            "cap": cap,
            "supports_cap_env": supports_cap,
            "peak_concurrent_pipelines": state["peak"],
            "peak_threads": state["peak_threads"],
            "cap_honored": bool(state["peak"] <= cap),
            "files_exactly_once": exactly_once,
            "files_never_run": sum(1 for c in counts if c == 0),
            "duplicate_runs": sum(max(0, c - 1) for c in counts),
            "elapsed_s": round(elapsed, 2),
            "files_per_min": round(n / elapsed * 60, 1) if elapsed > 0 else 0.0,
        },
        "integrity": None,
    }


def w_stale_claim(cfg: dict) -> dict:
    import threading

    _stub_network_side_effects()
    bins = _ensure_bins()
    from pipeline import watcher

    n = cfg["files"]
    release = threading.Event()
    started: dict[str, threading.Event] = {}
    runs: dict[str, int] = {}
    lock = threading.Lock()
    claimed_paths: dict[str, Path] = {}
    outcome: dict[str, str] = {}

    def fake_run(claimed, matter_id, **kw):
        name = Path(claimed).name
        with lock:
            runs[name] = runs.get(name, 0) + 1
            claimed_paths[name] = Path(claimed)
        ev = started.get(name)
        if ev:
            ev.set()
        release.wait(timeout=30)
        p = Path(claimed)
        outcome[name] = "present" if p.exists() else "gone"
        if p.exists():
            bins.move_to_archive(p, "M-1", "contract", doc_id=name[:6])
        return {"doc_id": name, "stage": "archived"}

    watcher.run_pipeline = fake_run
    w = watcher.Watcher()
    old = time.time() - 2 * 3600
    names = [f"backlog{i:03d}.txt" for i in range(n)]
    for name in names:
        f = bins.inbox_dir() / name
        f.write_text(f"backlogged {name}")
        os.utime(f, (old, old))
        started[name] = threading.Event()
    runners = [
        threading.Thread(target=w._process_existing, args=(bins.inbox_dir() / name,), daemon=True) for name in names
    ]
    for t in runners:
        t.start()
    for name in names:
        started[name].wait(timeout=20)
    # Every pipeline is mid-run. claim_file() stamps the claim time, so the
    # claims are fresh here: age them past the stale cutoff NOW, as a run that
    # outlives STALE_CLAIM_MINUTES would, or the reconcile below has nothing
    # to reclaim and "no hijacking" would hold vacuously.
    from pipeline.bins import list_stale_processing_files

    for name in names:
        p = claimed_paths.get(name)
        if p is not None and p.exists():
            os.utime(p, (old, old))
    aged = {q.name for q in list_stale_processing_files(stale_minutes=watcher.STALE_CLAIM_MINUTES)}
    w._reconcile_stale_claims()
    hijacked = 0
    for name in names:
        p = claimed_paths.get(name)
        moved_out = p is None or not p.exists()
        requeued = (bins.inbox_dir() / name).exists() or any(
            q.name.startswith(Path(name).stem + "--stale") for q in bins.inbox_dir().glob("*")
        )
        if moved_out or requeued:
            hijacked += 1
    # A second reconcile + rescan pass while still in flight, then finish.
    w._reconcile_stale_claims()
    rerun_candidates = list(bins.list_inbox_files())
    release.set()
    for t in runners:
        t.join(timeout=30)
    rerun_threads = [
        threading.Thread(target=w._process_existing, args=(f,), daemon=True) for f in rerun_candidates
    ]
    for t in rerun_threads:
        t.start()
    for t in rerun_threads:
        t.join(timeout=30)

    # A genuine orphan (dead worker, old mtime, nothing running) must be recovered.
    orphans = [f"orphan{i:03d}.txt" for i in range(max(1, n // 2))]
    dead = bins.processing_dir("dead-worker")
    dead.mkdir(parents=True, exist_ok=True)
    for name in orphans:
        f = dead / name
        f.write_text(f"orphan {name}")
        os.utime(f, (old, old))
    w._reconcile_stale_claims()
    def _requeued_intact(name: str) -> bool:
        # Recovered = back in the inbox with its original bytes. Disappearing from
        # the dead worker's dir (deleted, moved elsewhere) is not recovery.
        try:
            return (bins.inbox_dir() / name).read_text() == f"orphan {name}"
        except OSError:
            return False

    recovered = sum(1 for name in orphans if _requeued_intact(name))
    with lock:
        double_runs = sum(max(0, c - 1) for c in runs.values())
    return {
        "metrics": {
            "inflight_files": n,
            "inflight_aged_stale": sum(1 for name in names if name in aged),
            "inflight_hijacked": hijacked,
            "double_runs": double_runs,
            "orphans_total": len(orphans),
            "orphans_unrecovered": len(orphans) - recovered,
        },
        "integrity": None,
    }


def w_crash_recovery(cfg: dict) -> dict:
    import asyncio
    from unittest.mock import patch

    _stub_network_side_effects()
    mock = ScaleMock(seed=cfg["seed"])
    _start_llm_patches(mock)
    bins = _ensure_bins()
    import graph.build_graph as bg
    from pipeline import watcher
    from storage.audit_log import get_audit_chain, list_audit_doc_ids  # noqa: F401
    from storage.catalog import list_documents

    class Crash(BaseException):
        """SIGKILL stand-in: not an Exception, so no handler can convert it."""

    def crasher(state):
        raise Crash("simulated SIGKILL")

    points = [("after_intake", "classify_node"), ("after_classify", "extract_node"), ("after_extract", "compile_report_node")]
    per_doc = cfg.get("docs_per_point", 2)
    corpus = make_corpus(len(points) * per_doc, cfg["seed"] + 101, clean=True)
    inputs = {n: m for n, _t, m in corpus}
    assignments = {}
    i = 0
    for point, node in points:
        for _ in range(per_doc):
            assignments[corpus[i][0]] = (point, node)
            i += 1
    _write_corpus(corpus)

    died = {}
    for name, (point, node) in assignments.items():
        claimed = bins.claim_file(bins.inbox_dir() / name, "w-crash")
        bg.reset_compiled_graph()
        with patch(f"graph.build_graph.{node}", crasher):
            bg.reset_compiled_graph()
            try:
                bg.run_pipeline(claimed, "M-1")
                died[name] = False
            except BaseException as exc:  # noqa: BLE001
                died[name] = type(exc).__name__ == "Crash"
        bg.reset_compiled_graph()
        old = time.time() - 2 * 3600
        if claimed.exists():
            os.utime(claimed, (old, old))

    # Recovery = what a restarted watcher does: reconcile stale claims, rescan.
    w = watcher.Watcher()
    w._reconcile_stale_claims()
    for f in list(bins.list_inbox_files()):
        w._process_existing(f)

    inter = check_integrity(inputs)

    async def _per_file():
        rows = await list_documents()
        out = {}
        for name in inputs:
            recs = [r for r in rows if r.original_filename == name]
            chain_ids = set()
            for r in recs:
                if await get_audit_chain(r.doc_id):
                    chain_ids.add(r.doc_id)
            out[name] = {
                "rows": len(recs),
                "chains": len(chain_ids),
                "stages": sorted(r.stage for r in recs),
            }
        return out

    per_file = asyncio.run(_per_file())
    manifests = _manifest_index()
    dup_manifests = sum(max(0, len(manifests.get(n, [])) - 1) for n in inputs)
    by_point = {}
    for name, (point, _node) in assignments.items():
        agg = by_point.setdefault(point, {"docs": 0, "extra_rows": 0, "extra_chains": 0, "died": 0, "final_stages": {}})
        agg["docs"] += 1
        agg["died"] += int(died.get(name, False))
        agg["extra_rows"] += max(0, per_file[name]["rows"] - 1)
        agg["extra_chains"] += max(0, per_file[name]["chains"] - 1)
        for s in per_file[name]["stages"]:
            agg["final_stages"][s] = agg["final_stages"].get(s, 0) + 1
    return {
        "metrics": {
            "crash_docs": len(inputs),
            "crash_points": [p for p, _n in points],
            "all_crashed": len(died) == len(assignments) and bool(died) and all(died.values()),
            "duplicate_manifests": dup_manifests,
            "by_point": by_point,
        },
        "integrity": inter,
    }


def w_db_contention(cfg: dict) -> dict:
    import asyncio
    import collections
    import threading

    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry
    from storage.catalog import write_document_record
    from storage.db import ensure_schema

    ensure_schema()
    n_threads, n_docs = cfg["threads"], cfg["docs_per_thread"]
    errors = collections.Counter()
    ok = [0]
    lock = threading.Lock()

    def worker(tid: int):
        async def run():
            for i in range(n_docs):
                doc = f"D{tid}-{i}"
                fname = f"{doc}.pdf"
                steps = [
                    lambda: write_audit_entry(build_audit_entry(doc, "M", "ingested", "intake", {"original_filename": fname})),
                    lambda: write_document_record({"doc_id": doc, "matter_id": "M", "original_filename": fname, "stage": "processing"}),
                    lambda: write_audit_entry(build_audit_entry(doc, "M", "classified", "sorter", {"x": 1})),
                    lambda: write_document_record(
                        {"doc_id": doc, "matter_id": "M", "original_filename": fname, "stage": "archived",
                         "doc_type": "contract", "extracted_data": {"k": "v" * 5000}}
                    ),
                    lambda: write_audit_entry(build_audit_entry(doc, "M", "archived", "archivist", {"y": 2})),
                ]
                for step in steps:
                    try:
                        await step()
                        with lock:
                            ok[0] += 1
                    except Exception as exc:  # noqa: BLE001
                        with lock:
                            errors[f"{type(exc).__name__}: {str(exc)[:70]}"] += 1

        asyncio.run(run())

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - t0
    inter = check_integrity(None)
    # no input accounting here; only chain/row consistency is meaningful
    return {
        "metrics": {
            "threads": n_threads,
            "docs": n_threads * n_docs,
            "ok_ops": ok[0],
            "failed_ops": sum(errors.values()),
            "ops_per_s": round(ok[0] / elapsed, 1) if elapsed > 0 else 0.0,
            "elapsed_s": round(elapsed, 2),
            "error_kinds": dict(errors.most_common(3)),
        },
        "integrity": {k: inter[k] for k in ("broken_chains", "orphan_processing_rows", "duplicate_sha_rows", "catalog_rows", "audit_chains")},
    }


def w_provider_faults(cfg: dict) -> dict:
    import asyncio
    import collections
    import threading

    _stub_network_side_effects()
    real_sleep = time.sleep
    requested = []
    rlock = threading.Lock()
    rname = ("llm.retry", "langchain_agents.base_agent")

    def fast_sleep(seconds):
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if caller in rname:
            with rlock:
                requested.append(float(seconds))
            real_sleep(0.0005)
        else:
            real_sleep(seconds)

    time.sleep = fast_sleep
    mock = ScaleMock(
        seed=cfg["seed"],
        latency_s=cfg["latency_ms"] / 1000.0,
        fault_rate=cfg["fault_rate"],
        outage_start=cfg["outage_start"],
        outage_calls=cfg["outage_calls"],
        real_sleep=real_sleep,
    )
    _start_llm_patches(mock)
    bins = _ensure_bins()
    from graph.build_graph import reset_compiled_graph, run_pipeline
    from storage.catalog import list_documents

    reset_compiled_graph()
    corpus = make_corpus(cfg["docs"], cfg["seed"] + 7, clean=True)  # clean: faults are the only reason to leave `archived`
    _write_corpus(corpus)
    inputs = {n: m for n, _t, m in corpus}
    queue = collections.deque(inputs)
    qlock = threading.Lock()
    stages = collections.Counter()
    raised = collections.Counter()

    def worker(wid: int):
        while True:
            with qlock:
                if not queue:
                    return
                name = queue.popleft()
            try:
                claimed = bins.claim_file(bins.inbox_dir() / name, f"w{wid}")
                stage = run_pipeline(claimed, "M-1").get("stage")
            except Exception as exc:  # noqa: BLE001
                stage = "raised"
                with qlock:
                    raised[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
            with qlock:
                stages[stage] += 1

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(cfg["threads"])]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - t0
    time.sleep = real_sleep

    rows = asyncio.run(list_documents())
    n = len(inputs)
    review = [r for r in rows if r.stage == "review"]
    failed = [r for r in rows if r.stage == "failed"]
    transient_review = [r for r in review if "transient" in (r.escalation_reason or "").lower()]
    transient_failed = [r for r in failed if "transient" in (r.escalation_reason or "").lower()]
    return {
        "metrics": {
            "docs": n,
            "threads": cfg["threads"],
            "fault_rate": cfg["fault_rate"],
            "outage_calls": cfg["outage_calls"],
            "elapsed_s": round(elapsed, 2),
            "llm_calls": mock.calls_total,
            "faults_injected": mock.faults_injected,
            "outage_faults": mock.outage_faults,
            "retries_made": len(requested),
            "backoff_requested_total_s": round(sum(requested), 1),
            "backoff_requested_max_s": round(max(requested) if requested else 0.0, 1),
            "docs_archived": sum(1 for r in rows if r.stage == "archived"),
            "docs_review": len(review),
            "docs_failed": len(failed),
            "docs_review_transient": len(transient_review),
            "docs_failed_transient": len(transient_failed),
            "transient_review_share": round((len(transient_review) + len(transient_failed)) / n, 4) if n else 0.0,
            "raised": sum(raised.values()),
            "returned_stage_counts": dict(stages),
        },
        "integrity": check_integrity(inputs),
    }


WORKERS = {
    "throughput": w_throughput,
    "watcher_backpressure": w_watcher_backpressure,
    "stale_claim": w_stale_claim,
    "crash_recovery": w_crash_recovery,
    "db_contention": w_db_contention,
    "provider_faults": w_provider_faults,
}


def worker_main(name: str, cfg: dict, out: Path) -> int:
    sys.path.insert(0, str(SRC_DIR))
    try:
        import dojo_source  # noqa: F401  (installed Dojo first, vendored fallback)
    except Exception:  # noqa: BLE001
        pass
    try:
        result = WORKERS[name](cfg)
        result["ok"] = True
    except BaseException as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]}
    out.write_text(json.dumps(result, default=str))
    sys.stdout.flush()
    os._exit(0)  # daemon threads / watcher observers must not hold the process


# ---------------------------------------------------------------------------
# Parent side: orchestration, scorecard, SCORE
# ---------------------------------------------------------------------------


def run_worker(name: str, cfg: dict, args, label: str) -> dict:
    """Run one worker in a fresh subprocess + fresh base dir; return its result."""
    root = Path(tempfile.mkdtemp(prefix=f"stress-{label}-", dir=args.workdir)).resolve()
    base = root / "base"
    base.mkdir()
    out = root / "result.json"
    log = root / "worker.log"
    env = _worker_env(
        str(base), args.log_level, {"MAILROOM_MAX_CONCURRENT_DOCS": str(args.cap), **({"MAILROOM_BASE_DIR": str(base)})}
    )
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", name, "--cfg", json.dumps(cfg), "--out", str(out)]
    t0 = time.perf_counter()
    result: dict
    try:
        with open(log, "wb") as fh:
            proc = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=args.timeout, cwd=str(REPO_ROOT))
        if out.exists():
            result = json.loads(out.read_text())
        else:
            tail = log.read_bytes()[-1500:].decode(errors="replace")
            result = {"ok": False, "error": f"worker exited {proc.returncode} without a result", "traceback": tail}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": f"worker timed out after {args.timeout}s"}
    result["label"] = label
    result["wall_s"] = round(time.perf_counter() - t0, 2)
    if args.keep:
        result["kept_dir"] = str(root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    if not result.get("ok"):
        print(f"  ! worker {label} failed: {result.get('error')}", file=sys.stderr)
    return result


def parse_int_list(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def positive_int_list(s: str) -> list[int]:
    """argparse type: comma-separated integers, every one >= 1."""
    try:
        values = parse_int_list(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {s!r}") from exc
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError(f"every count must be >= 1, got {s!r}")
    return values


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=int, default=120, help="documents per throughput run (default 120)")
    ap.add_argument("--threads", default="1,8,32", help="throughput thread counts (default 1,8,32)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--scenarios", default="all", help=f"comma list of {','.join(ALL_SCENARIOS)} or 'all'")
    ap.add_argument("--mock-latency-ms", type=float, default=50.0, help="per mock LLM call latency (default 50)")
    ap.add_argument("--target-dpm", type=float, default=600.0, help="docs/min at 8 threads that scores full marks")
    ap.add_argument("--json", dest="json_out", default=None, help="write the full scorecard to this path")
    ap.add_argument("--cap", type=int, default=4, help="MAILROOM_MAX_CONCURRENT_DOCS exported to workers (default 4)")
    ap.add_argument("--watcher-files", type=int, default=None, help="files dropped in watcher_backpressure (default --docs)")
    ap.add_argument("--watcher-run-ms", type=float, default=500.0, help="stub pipeline duration (default 500)")
    ap.add_argument("--stale-files", type=int, default=3, help="in-flight backlogged files in stale_claim")
    ap.add_argument("--db-threads", type=positive_int_list, default="32,128", help="db_contention thread counts (default 32,128)")
    ap.add_argument("--db-docs-per-thread", type=int, default=3)
    ap.add_argument("--fault-rate", type=float, default=0.2)
    ap.add_argument("--outage-calls", type=int, default=40, help="consecutive failing LLM calls (full outage)")
    ap.add_argument("--fault-docs", type=int, default=None, help="documents in provider_faults (default min(--docs, 60))")
    ap.add_argument("--fault-threads", type=int, default=8)
    ap.add_argument("--crash-docs-per-point", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=900.0, help="per-worker timeout in seconds")
    ap.add_argument("--workdir", default=None, help="parent directory for scratch dirs (default system temp)")
    ap.add_argument("--keep", action="store_true", help="keep scratch dirs (worker logs, DBs)")
    ap.add_argument("--log-level", default="CRITICAL", help="LOG_LEVEL for workers (default CRITICAL)")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--cfg", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out", default=None, help=argparse.SUPPRESS)
    return ap


def collect(args) -> dict:
    selected = set(ALL_SCENARIOS) if args.scenarios.strip() == "all" else {s.strip() for s in args.scenarios.split(",") if s.strip()}
    unknown = selected - set(ALL_SCENARIOS)
    if unknown:
        raise SystemExit(f"unknown scenarios: {sorted(unknown)}")
    do_integrity = "integrity" in selected
    runs: list[dict] = []
    scen: dict[str, dict] = {}
    cfg_common = {"seed": args.seed}

    def go(name, cfg, label):
        print(f"[{label}] running ...", flush=True)
        r = run_worker(name, {**cfg_common, **cfg}, args, label)
        runs.append(r)
        return r

    if "throughput" in selected:
        scen["throughput"] = {}
        for t in parse_int_list(args.threads):
            scen["throughput"][str(t)] = go(
                "throughput", {"docs": args.docs, "threads": t, "latency_ms": args.mock_latency_ms}, f"throughput@{t}"
            )
    if "watcher_backpressure" in selected:
        files = args.watcher_files or args.docs
        wait = max(60.0, files * args.watcher_run_ms / 1000.0 / max(1, args.cap) * 4 + 30)
        scen["watcher_backpressure"] = go(
            "watcher_backpressure",
            {"files": files, "run_ms": args.watcher_run_ms, "cap": args.cap, "max_wait_s": wait},
            "watcher_backpressure",
        )
    if "stale_claim" in selected:
        scen["stale_claim"] = go("stale_claim", {"files": args.stale_files}, "stale_claim")
    if "crash_recovery" in selected:
        scen["crash_recovery"] = go("crash_recovery", {"docs_per_point": args.crash_docs_per_point}, "crash_recovery")
    if "db_contention" in selected:
        scen["db_contention"] = {}
        for t in args.db_threads:
            scen["db_contention"][str(t)] = go(
                "db_contention", {"threads": t, "docs_per_thread": args.db_docs_per_thread}, f"db_contention@{t}"
            )
    if "provider_faults" in selected:
        scen["provider_faults"] = go(
            "provider_faults",
            {
                "docs": args.fault_docs or min(args.docs, 60),
                "threads": args.fault_threads,
                "latency_ms": 0.0,
                "fault_rate": args.fault_rate,
                "outage_start": 30,
                "outage_calls": args.outage_calls,
            },
            "provider_faults",
        )
    return {"selected": sorted(selected), "do_integrity": do_integrity, "scenarios": scen, "runs": runs}


def _flat_runs(scen: dict):
    for name, val in scen.items():
        if name in ("throughput", "db_contention"):
            for k, r in val.items():
                yield f"{name}@{k}", r
        else:
            yield name, val


def score(args, data: dict) -> dict:
    scen = data["scenarios"]
    metrics: dict[str, object] = {}
    errors = [label for label, r in _flat_runs(scen) if not r.get("ok")]

    # --- throughput
    dpm_by_threads: dict[int, float] = {}
    for k, r in scen.get("throughput", {}).items():
        if r.get("ok"):
            m = r["metrics"]
            dpm_by_threads[int(k)] = m["docs_per_min"]
            for key in ("docs_per_min", "p50_s", "p95_s", "max_s", "elapsed_s", "raised"):
                metrics[f"throughput.{key}@{k}"] = m[key]
            metrics[f"throughput.terminal_states@{k}"] = m["returned_stage_counts"]
            metrics[f"throughput.llm_calls@{k}"] = m["llm_calls"]
    dpm8 = None
    if dpm_by_threads:
        nearest = min(dpm_by_threads, key=lambda t: (abs(t - 8), t))
        dpm8 = dpm_by_threads[nearest]
        metrics["throughput.docs_per_min@8"] = dpm8
        metrics["throughput.docs_per_min@8.measured_at_threads"] = nearest

    # --- integrity (summed over all workers)
    inter_keys = (
        "lost_docs", "duplicate_catalog_rows", "duplicate_sha_rows", "duplicate_chains",
        "orphan_processing_rows", "broken_chains", "lost_audit_events",
    )
    totals = {k: 0 for k in inter_keys}
    measured = 0  # workers that audited integrity at all
    accounted = 0  # workers that audited their input documents (lost_docs is only set then)
    for label, r in _flat_runs(scen):
        inter = r.get("integrity") if r.get("ok") else None
        if inter:
            measured += 1
            accounted += "lost_docs" in inter
            for k in inter_keys:
                totals[k] += int(inter.get(k, 0) or 0)
            for k in inter_keys:
                metrics[f"integrity[{label}].{k}"] = inter.get(k, 0)
    if data["do_integrity"] or measured:
        for k in inter_keys:
            metrics[f"integrity.{k}"] = totals[k]

    # --- other scenarios
    wb = scen.get("watcher_backpressure")
    if wb and wb.get("ok"):
        for k, v in wb["metrics"].items():
            metrics[f"watcher.{k}"] = v
    sc = scen.get("stale_claim")
    if sc and sc.get("ok"):
        for k, v in sc["metrics"].items():
            metrics[f"stale_claim.{k}"] = v
    cr = scen.get("crash_recovery")
    if cr and cr.get("ok"):
        ci = cr["integrity"]
        metrics["crash_recovery.duplicate_catalog_rows"] = ci["duplicate_catalog_rows"]
        metrics["crash_recovery.duplicate_chains"] = ci["duplicate_chains"]
        metrics["crash_recovery.duplicate_manifests"] = cr["metrics"]["duplicate_manifests"]
        metrics["crash_recovery.orphan_processing_rows"] = ci["orphan_processing_rows"]
        metrics["crash_recovery.lost_docs"] = ci["lost_docs"]
        metrics["crash_recovery.by_point"] = cr["metrics"]["by_point"]
        metrics["crash_recovery.all_crashed"] = cr["metrics"]["all_crashed"]
    db_failed = 0
    for k, r in scen.get("db_contention", {}).items():
        if r.get("ok"):
            m = r["metrics"]
            db_failed += m["failed_ops"]
            metrics[f"db.failed_ops@{k}"] = m["failed_ops"]
            metrics[f"db.ops_per_s@{k}"] = m["ops_per_s"]
            if m["error_kinds"]:
                metrics[f"db.error_kinds@{k}"] = m["error_kinds"]
    pf = scen.get("provider_faults")
    if pf and pf.get("ok"):
        for k, v in pf["metrics"].items():
            metrics[f"provider_faults.{k}"] = v

    # --- gates (only for scenarios that ran)
    gates: dict[str, dict] = {}

    def gate(name, value, ok):
        gates[name] = {"value": value, "pass": bool(ok)}

    # Measured integrity failures are gated whichever scenario produced them
    # (selecting `integrity` only adds the requirement that something measured).
    if data["do_integrity"] or measured:
        for k in inter_keys:
            gate(k, totals[k], totals[k] == 0)
    if data["do_integrity"]:
        gate("integrity_measured", accounted, accounted > 0)
    if cr and cr.get("ok"):
        gate("crash_injected", int(not cr["metrics"]["all_crashed"]), cr["metrics"]["all_crashed"])
    if sc and sc.get("ok"):
        gate("inflight_hijacked", sc["metrics"]["inflight_hijacked"], sc["metrics"]["inflight_hijacked"] == 0)
        gate("orphans_unrecovered", sc["metrics"]["orphans_unrecovered"], sc["metrics"]["orphans_unrecovered"] == 0)
    if "db_contention" in scen:
        gate("db_failed_ops", db_failed, db_failed == 0 and all(r.get("ok") for r in scen["db_contention"].values()))
    if wb and wb.get("ok"):
        bad = wb["metrics"]["duplicate_runs"] + wb["metrics"]["files_never_run"]
        gate("watcher_exactly_once", bad, bad == 0)
    gate("scenario_error", len(errors), not errors)

    # --- SCORE
    cap_honored = None
    if wb and wb.get("ok"):
        cap_honored = 1.0 if wb["metrics"]["cap_honored"] else 0.0
    share = None
    if pf and pf.get("ok"):
        share = pf["metrics"]["transient_review_share"]
    c_thr = 0.5 * min(1.0, dpm8 / args.target_dpm) if dpm8 is not None and args.target_dpm > 0 else 0.0
    c_flt = 0.3 * (1.0 - share) if share is not None else 0.0
    c_cap = 0.2 * cap_honored if cap_honored is not None else 0.0
    gates_ok = all(g["pass"] for g in gates.values())
    total = (c_thr + c_flt + c_cap) if gates_ok else 0.0
    components = {
        "throughput_0.5": round(c_thr, 4) if dpm8 is not None else "not measured",
        "faults_0.3": round(c_flt, 4) if share is not None else "not measured",
        "cap_0.2": round(c_cap, 4) if cap_honored is not None else "not measured",
        "ungated_total": round(c_thr + c_flt + c_cap, 4),
    }
    return {
        "metrics": metrics,
        "gates": gates,
        "gates_ok": gates_ok,
        "components": components,
        "SCORE": round(total, 4),
        "target_dpm": args.target_dpm,
    }


def print_scorecard(sc: dict, wall_s: float) -> None:
    metrics = sc["metrics"]
    width = max((len(k) for k in metrics), default=10) + 2
    print("\n=== SCALE SCORECARD ===")
    for k in sorted(metrics):
        v = metrics[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        print(f"{k:<{width}} {v}")
    print("\n--- score components ---")
    for k, v in sc["components"].items():
        print(f"{k:<{width}} {v}")
    print("\n--- gates ---")
    for name, g in sc["gates"].items():
        print(f"GATE {name} {'PASS' if g['pass'] else 'FAIL'}  (value={g['value']})")
    print(f"\nwall time: {wall_s:.1f}s")


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.worker:
        return worker_main(args.worker, json.loads(args.cfg), Path(args.out))
    t0 = time.perf_counter()
    data = collect(args)
    sc = score(args, data)
    wall = time.perf_counter() - t0
    print_scorecard(sc, wall)
    if args.json_out:
        payload = {
            "args": {k: v for k, v in vars(args).items() if k not in ("worker", "cfg", "out")},
            "wall_s": round(wall, 1),
            **sc,
            "scenarios": data["scenarios"],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
    print(f"SCORE: {sc['SCORE']:.4f}")
    return 0 if sc["gates_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
