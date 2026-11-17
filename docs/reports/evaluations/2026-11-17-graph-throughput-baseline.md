# Graph throughput baseline (Phase 0): where one process stops scaling

- **Date**: 2026-11-17 (UTC)
- **Kind**: evaluations (offline load benchmark; no real model, no hosted service)
- **Status**: baseline recorded. **Measured at commit `df127446e0449a134e1de8c5cd9fdce18504ad7e`**
  (`df12744`, branch `claude/project-thread-lwyq6k`), with `tree_clean: true` in each JSON. That flag
  was sampled after each run finished, not before, so it does not prove the tree stayed clean during
  the run (the script now checks before and after, and fails a run whose provenance moved).
  #286 was squash-merged, so `df12744` is not reachable from `v0.30.0` or `main`; the measured code,
  the benchmark script and this report are all in the single #286 squash commit `49425a3`.
  What `git` supports as of the time of writing (checked at `v0.30.0` head, `dee8512` plus later fixes):
  - `src/scripts/bench_throughput.py` has one commit, `49425a3`, and has not changed since.
    `df12744` itself is not in this clone, so the script at `df12744` was not diffed against it.
  - #283, #284 and #288 are ancestors of `49425a3` and landed after the measurement. #283 changes
    insurance extraction enrichment (`doc_inventories.py`, `specialist_agents.py`, `posthoc_gt.py`,
    `extraction_gt.py`, `schemas/documents.py`); #284 changes only the opt-in interim ground-truth
    overlay, the HF pilot and a test hook; #288 changes only tests. None touches `src/graph/`, `src/storage/`
    or the watcher, so none changes the graph wiring or the storage path this benchmark exercises.
    Whether #283's per-insurance-document derivation moves any number was not measured.
  - #289 (decision points) came after `49425a3` and does change `src/graph/build_graph.py`,
    `graph/state.py`, `pipeline/config.py`, `pipeline/watcher.py` and `pipeline/reconsideration.py`.
    It is off by default (no wrapper is added without a configured slot), but this baseline was not
    re-run on it, so the numbers below describe the pre-#289 graph.
- **Revision note**: the first version of this report (commit `a29f69a`) was measured at
  `e00ac25`, which is not in this branch's history, and its text claimed more than its numbers
  support. Everything below was re-measured on the shipped tree and the conclusions were rewritten
  against the new JSON. The numbers moved (for example 213 to 181 docs/min at c=64), which is part
  of why the variance caveat under "Not verified" matters.

> **Mock-LLM numbers.** Every LLM call is answered by the repository's fake LLM after a
> fixed sleep (default 200 ms). Absolute docs/min are therefore NOT production numbers, and a
> real provider's latency, rate limits, TLS/connection setup and queueing are absent. What
> the data supports: the shape across concurrency levels, the per-document work the pipeline
> does around the LLM, and the non-LLM overhead that grows with concurrency. What it does not
> support: the cause of that growth (see "Not verified").

## Scope

How the real graph (`graph.build_graph.run_pipeline`, with the real bins, SQLite catalog and
hash-chained audit log) behaves as the number of documents in flight grows, and what the numbers
say about each of the five suspected bottlenecks (below). The script is
`src/scripts/bench_throughput.py`. It reuses the fake LLM of `src/scripts/stress_test.py`
(`ScaleMock`: same seams as `run_pilot.py --mock`, keyed by a marker in the document text so
it is safe under concurrency) and counts around unmodified pipeline functions.

## Method

- Each concurrency level runs in its **own subprocess** with a fresh temporary
  `MAILROOM_BASE_DIR`, the hermetic worker environment of `stress_test.py` (credentials
  dropped, tracing off, `DATABASE_URL` pinned to the scratch SQLite file) and
  `OPENROUTER_BASE_URL` pointed at a closed local port, so a missed seam can never reach a
  hosted service. One warm-up document is run and discarded before measuring.
- **Workload** (same 128 documents at every level, seed 7): 5 classes, ~10% low-confidence
  (13 documents retried by `retry_classify`; 16 end in `human_review`), 4 long documents
  of ~230,000 characters (multi-window), 3 blank. Outcome at every level: 112 archived,
  16 routed to review, 0 failed or raised.
- Documents are claimed from the inbox (`bins.claim_file`, as the watcher does) and run by a
  `ThreadPoolExecutor` of `c` workers, calling `run_pipeline` directly. All `c` workers start at
  t=0. The watcher dispatch cap (`watcher.max_concurrent_docs`, 4) is therefore **not** in play:
  levels above 4 show what the pipeline does when something lets more documents in.
- LLM seams: `llm.client.OpenAI` and `langchain_agents.base_agent.ChatOpenAI` are replaced by
  counting factories that still construct the **real** client (so its construction cost is
  paid) and then fake only the request. Retry backoff sleeps are shortened by mutating the
  taxonomy `llm_retry` block in the worker process (`--retry-base-delay-ms`, default 50 ms;
  both retry layers read it), so injected 429s cost little time here by construction.
- Counted without changing code: `graph.build_graph._bounded` (per-node wall time, LLM time,
  heartbeat time), `_touch_heartbeat`, `_run_coro`, `asyncio.run`,
  `storage.db.create_async_engine` and `storage.db._apply_sqlite_pragmas` (runs once per new
  raw SQLite connection). CPU is `getrusage` of the worker process. "Heartbeat s" and
  "client construction s" are **sums over all worker threads**, so they can exceed the wall time.
- Latency is per document from claim to returned state; it excludes time queued behind the
  pool. "Doc overhead" is end-to-end latency minus the injected LLM sleep (it includes retry
  sleeps when 429s are injected). The per-node "overhead" is likewise wall time minus LLM time
  only, so it **includes** the heartbeat time that is reported next to it.

**Machine**: Linux 6.18 x86_64 cloud VM, 4 vCPU, 15.7 GB RAM, Python 3.13.18, SQLite via
aiosqlite, load average 1.81 (1-minute) sampled **after** the main run finished (0.56 to 1.71, also
sampled after the run, for the other two); other sessions may have shared the host. The JSON files
record that same end-of-run sample under `load_average_at_start`, as the benchmark script then
collected machine notes, the commit and `tree_clean` only after the levels had run; the values are
left as recorded and the label is wrong for these three files. The script now captures them before
the first level and flags `provenance_changed` if HEAD or the tree moved during the run.

**Commands** (from the repository root; each was run as shown, with `PYTHONPATH=src` and the
repository's `.venv/bin/python` (the JSON `command` field records it as such; the first revision of these files
said `python`); `.evidence/` is
git-ignored scratch space and its markdown tables are not committed):

```bash
# main baseline, 3 min 43 s
PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline.json --md-out .evidence/baseline.md
# 20% injected 429s, one level
PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py --levels 4 --docs 48 --rate-limit-rate 0.2 --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline-rate-limit-0.2.json --md-out .evidence/rl.md
# realistic provider latency, 2 levels
PYTHONPATH=src .venv/bin/python src/scripts/bench_throughput.py --levels 4,16 --docs 48 --latency-ms 2000 --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline-latency-2000ms.json --md-out .evidence/lat2000.md
```

The three runs went one after another with nothing else started by this session. After each run its JSON was moved aside and the
tracked JSON restored (`git checkout`), so every run saw a clean tree; the three new JSON files were
copied into place afterwards. All exited 0.

## Baseline (128 documents per level, 200 ms per LLM call, no injected 429s)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | doc overhead p50 ms | LLM calls | OpenAI ctor | ChatOpenAI ctor | heartbeats | heartbeat s (sum) | event loops | DB engines | DB connections | CPU cores busy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 128 / 0 | 97.6 | 79 | 615 | 763 | 215 | 346 | 62 | 188 | 733 | 4.5 | 2567 | 2567 | 3943 | 0.26 |
| 4 | 128 / 0 | 35.9 | 214 | 960 | 1316 | 560 | 346 | 62 | 188 | 733 | 11.9 | 2567 | 2567 | 3943 | 0.98 |
| 16 | 128 / 0 | 38.3 | 201 | 4408 | 7357 | 4008 | 346 | 62 | 188 | 733 | 105.4 | 2567 | 2567 | 3943 | 1.29 |
| 64 | 128 / 0 | 42.4 | 181 | 18365 | 30822 | 17906 | 346 | 62 | 188 | 733 | 536.6 | 2567 | 2567 | 3995 | 1.28 |

Derived from the same JSON (sums are over worker threads):

| concurrency | summed doc latency s | heartbeat s as % of it | mean ms per heartbeat call | client construction s (OpenAI + ChatOpenAI) | construction as % of wall | CPU s user / system | CPU s total |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 97.6 | 4.6% | 6.1 | 0.69 (0.60 + 0.08) | 0.7% | 20.5 / 5.1 | 25.6 |
| 4 | 142.7 | 8.3% | 16.2 | 0.74 (0.66 + 0.08) | 2.1% | 24.1 / 10.9 | 35.0 |
| 16 | 601.4 | 17.5% | 143.8 | 1.06 (0.99 + 0.08) | 2.8% | 29.3 / 20.1 | 49.4 |
| 64 | 2417.2 | 22.2% | 732.0 | 0.99 (0.90 + 0.09) | 2.3% | 32.4 / 22.1 | 54.5 |

Per node, p50 / p95 in ms (invocations):

| node | c=1 | c=4 | c=16 | c=64 |
|---|---:|---:|---:|---:|
| intake | 49 / 62 (128) | 130 / 288 (128) | 1097 / 1883 (128) | 5483 / 11542 (128) |
| classify | 226 / 234 (128) | 257 / 415 (128) | 543 / 1822 (128) | 1324 / 6961 (128) |
| retry_classify | 210 / 212 (13) | 222 / 281 (13) | 285 / 355 (13) | 522 / 3062 (13) |
| extract | 241 / 267 (112) | 276 / 458 (112) | 801 / 1637 (112) | 1950 / 7224 (112) |
| compile_report | 7 / 11 (112) | 13 / 40 (112) | 95 / 607 (112) | 263 / 3960 (112) |
| catalog_write | 24 / 31 (112) | 56 / 130 (112) | 386 / 1260 (112) | 1820 / 8964 (112) |
| archive | 51 / 63 (112) | 125 / 252 (112) | 836 / 1767 (112) | 2224 / 7734 (112) |
| human_review | 43 / 50 (16) | 132 / 254 (16) | 1159 / 1862 (16) | 3997 / 10387 (16) |

Per node, mean ms spent in injected LLM latency / everything else / the heartbeat (the
heartbeat is part of "everything else"):

| node | c=1 | c=4 | c=16 | c=64 |
|---|---:|---:|---:|---:|
| intake | 0 / 57 / 0 | 0 / 154 / 0 | 0 / 1137 / 1 | 0 / 5722 / 4 |
| classify | 333 / 27 / 7 | 333 / 73 / 21 | 333 / 486 / 179 | 333 / 2006 / 824 |
| extract | 214 / 42 / 7 | 214 / 94 / 20 | 214 / 664 / 136 | 214 / 2494 / 921 |
| compile_report | 0 / 9 / 9 | 0 / 18 / 18 | 0 / 170 / 170 | 0 / 925 / 923 |
| catalog_write | 0 / 25 / 7 | 0 / 67 / 18 | 0 / 537 / 172 | 0 / 3032 / 934 |
| archive | 0 / 51 / 7 | 0 / 138 / 20 | 0 / 960 / 217 | 0 / 3068 / 855 |
| human_review | 0 / 43 / 9 | 0 / 147 / 31 | 0 / 1203 / 223 | 0 / 4188 / 833 |

### Rate-limit run (c=4, 48 documents, `--rate-limit-rate 0.2`)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | LLM calls | 429s injected | heartbeats | event loops | DB connections |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 48 / 0 | 13.5 | 214 | 920 | 1446 | 143 | 23 | 278 | 973 | 1491 |

All 48 documents completed through both retry layers; 23 of 143 attempts (16%) were
rate-limited (`llm/retry.py` and the LangChain agents' `_invoke_with_retry`). With the 50 ms
backoff the throughput equals the clean c=4 baseline above (214 docs/min). That comparison is crude: the 48-document mix has
1 long document where the 128-document mix has 4.

### Provider-like latency (2000 ms per LLM call, 48 documents)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | e2e max ms | doc overhead p50 ms | LLM calls | CPU cores busy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 48 / 0 | 67.7 | 43 | 4419 | 5092 | 52547 | 419 | 120 | 0.18 |
| 16 | 48 / 0 | 53.9 | 53 | 6361 | 8496 | 53913 | 2285 | 120 | 0.30 |

The 48-document mix contains one long document. Its end-to-end time is the max column: 52.5 s at
c=4 and 53.9 s at c=16, which is the whole wall time of the c=16 run.

## Findings

1. **Throughput stops rising at about 4 documents in flight, and the LLM time is not what grows.**
   Throughput is 79, 214, 201, 181 docs/min for c = 1, 4, 16, 64. The c=4 gain is 2.7x (not 4x).
   Above c=4 it does not rise and drifts down (-6% at c=16, -15% at c=64); with one run per level
   the size of that decline is not established (see "Not verified"). Median per-document latency
   grows from 0.6 s to 1.0, 4.4 and 18.4 s, so each document spends longer inside `run_pipeline`
   while throughput does not improve. The per-document overhead that is not injected LLM time goes
   from 215 ms (c=1) to 17,906 ms (c=64), 83x. The process keeps only 1.29 and 1.28 of 4 cores
   busy at c=16 and c=64, and the CPU it burns for the same 128 documents grows from 25.6 s to
   54.5 s (+113%; of the 28.9 s added, 17.0 s is system time and 11.9 s user time), so
   contention adds work and does not just add waiting.
2. **The overhead that grows is in the storage path; its cause is not isolated.** The per-document
   counts are constant at every level: 5.7 heartbeats, about 20 `asyncio.run` event loops, 20
   SQLAlchemy engines (`NullPool`) and 31 SQLite connections (2567, 2567 and 3943 for 128
   documents). Constant counts cannot by themselves explain a cost that grows 83-fold, so the
   per-document loop / engine / connection pattern is a constant cost whose price changes with
   concurrency, not a measured cause. The piece that is measured and grows is heartbeat time: mean
   ms per heartbeat call 6.1, 16.2, 143.8, 732.0, and summed heartbeat time is 4.6%, 8.3%, 17.5%
   and 22.2% of the summed per-document latency. `compile_report` is a node whose "everything else"
   time is almost entirely the heartbeat at every level (everything else / heartbeat, mean ms: 9 / 9,
   18 / 18, 170 / 170, 925 / 923), which isolates the heartbeat cost cleanly. The other
   storage-touching nodes grow in the part that is not the heartbeat too, about as much as the
   heartbeat part: `catalog_write` has 18 ms of non-heartbeat overhead at c=1 and about 2,100 ms at
   c=64 (its heartbeat goes from 7 to 934 ms), `archive` 44 ms and about 2,200 ms (7 to 855 ms). The growth is **consistent with**
   serialization somewhere in the storage path (the SQLite single-writer lock behind a WAL
   database, whose `busy_timeout` makes a blocked writer wait, the GIL, or loop and thread churn
   from creating a loop, engine and connection per call). **This benchmark does not distinguish
   them**: no profiler or lock-wait counter was run. One weak extra signal: DB connections opened
   rise from 3,943 to 3,995 (+52, +1.3%) at c=64 only, which could be write retries under lock
   contention (a retry reconnects), but this was not checked and the same counter is exactly constant
   at c=1, 4 and 16. `intake` runs no heartbeat but writes the manifest, the catalog record and
   the `ingested` audit event, so it also touches storage; its p50 (49, 130, 1097, 5483 ms) is also
   partly a start-up convoy: all workers start at t=0, and at c=64 half of all documents (64 of 128)
   are in `intake` together at the start. Read its c=64 p50 as the start wave, not as steady state.
3. **Suspect 1 (an HTTP client per agent instance): true only for the native `OpenAI` path; no CPU
   cost was found.** The two client families differ, and the first version of this report was wrong
   to treat them as one:
   - The native `OpenAI` clients built by `llm.client.get_llm` each own a fresh httpx pool (62
     constructed for the 128 documents, about 10 ms each at c=1). Checked in the installed package:
     two `OpenAI(api_key=..., base_url=..., timeout=120)` instances have different `_client` objects.
   - The `ChatOpenAI` clients (`langchain_agents/base_agent.py`, 188 constructed) **share one httpx
     client**. In the installed `langchain-openai` 1.8.0, `chat_models/base.py` passes
     `_get_default_httpx_client(self.openai_api_base, self.request_timeout, socket_options)` (and the
     async twin) as the SDK's `http_client`. For a hashable timeout this returns
     `_cached_sync_httpx_client`, decorated with `@lru_cache` in `_client_utils.py` and keyed on
     (base_url, timeout, socket options). Our agents pass `timeout=120`, an int, so the cache applies
     (an `httpx.Timeout` object would not hash and would skip it). Checked directly: two
     `ChatOpenAI(model, api_key, base_url, timeout=120)` instances have `root_client` objects that
     differ but `root_client._client is` the same object (also the async client), with and without the
     proxy environment variables this sandbox sets. That fits the construction cost: 0.45 ms per
     `ChatOpenAI` (0.08 to 0.09 s for 188) against 9.7 ms per `OpenAI` (0.60 s for 62) at c=1.
   So only the `OpenAI` path pays for a new pool, and only at construction. Construction time summed
   over threads is 0.69 s at c=1 and 0.99 to 1.06 s at c=16 and c=64, which is 0.7%, 2.1%, 2.8% and
   2.3% of the wall time at c = 1, 4, 16, 64 (0.7% down to 0.04% of the summed document latency).
   What a fresh pool costs against a real provider (a TCP and TLS handshake per new pool, no
   keep-alive reuse for the `OpenAI` path) cannot be measured with a fake request: **not
   verified**. Whether the shared `ChatOpenAI` client behaves well under real traffic is not
   verified either.
4. **Suspect 5 (windows run one after another): the counts say yes, and the effect is larger than
   the brief assumed.** The benchmark records LLM calls per node, not per document, so the split is
   an inference from the totals. In the 128-document run `classify` made 213 calls and `extract`
   120. With 3 blank documents (no sorter call), 121 short documents at one call each and 4 long
   ones, 213 = 121 + 4 x 23 and 120 = 108 + 4 x 3; the 48-document runs give the same 23 and 3 for
   their single long document (70 = 47 + 23, 45 = 42 + 3). So a ~230,000-character document would
   take about 23 sequential sorter calls and 3 extraction calls: 26 calls, 5.7 s end to end at 200
   ms per call (the maximum latency at c=1; a short document takes about 0.6 s), and 52.5 s at 2000
   ms per call. At the provider-like latency that one document is the makespan of a 48-document run
   (53.9 s wall at c=16, equal to its max latency, against 67.7 s at c=4). This benchmark cannot say
   how common long documents are in the real corpus.
5. **Suspect 4 (reactive rate-limit backoff): exercised, cost not measured.** Both retry
   layers recover from a 20% 429 setting with no failed document. The backoff was shortened to
   50 ms, so the run says nothing about its real cost. For scale only (arithmetic, not
   measured): at the taxonomy defaults (`rate_limit_base_delay` 8 s, doubling, capped at 60 s,
   jitter 0.3) each 429 holds a worker for at least about 5.6 s (8 s less 30%), so the 23
   rate-limited calls above would idle workers for roughly 2 minutes of worker time, against
   the roughly 54 worker-seconds (4 workers, 13.5 s) the whole run used. There
   is no limiter that keeps calls below a provider's rate, so concurrency and 429s feed each
   other.
6. **Suspect 3 (the watcher cap of 4): not exercised, but the data bounds it.** Raising the
   cap above 4 would not help on this machine with a 200 ms provider, because throughput does not
   rise from c=4 (214, 201, 181 docs/min). At 2000 ms per call the picture changes (the process is
   mostly idle, 0.18 to 0.30 cores busy) and the cap would bind, but only
   two levels at 48 documents were run and the long-document tail dominates them, so the
   benefit of a higher cap at realistic latency is **not measured**.
7. **Work per document** is constant at every level (2.7 LLM calls, 5.7 heartbeats, about 20 event
   loops, 20 engines and 31 SQLite connections), so the work per document does not change; what
   changes with concurrency is what each unit of it costs.

## Recommendations (for the next phase, not done here)

- Isolate the cause first (finding 2): a profiler run (or a SQLite lock-wait counter) at c=16 would
  say whether the time is the writer lock, the GIL or loop/engine churn, and so which change can
  help. A long-lived loop or sync path per worker and a reused engine/connection for catalog, audit
  and heartbeat writes is the obvious candidate because the storage path is the only place whose
  cost is seen to grow with concurrency; it is a hypothesis until a re-run shows it. After each
  change, re-run this benchmark: the counters (`event loops`, `DB engines`, `DB connections`,
  `heartbeat s`) must fall, and `docs/min` at c=16 and c=64 must rise.
- Make the sorter and specialist windows concurrent, or bound their number, before raising
  the watcher cap (finding 4).
- Only then reuse the native `OpenAI` clients across documents (finding 3); the `ChatOpenAI` path
  already shares its httpx client. Small CPU win, unknown network win.
- Treat the cap and rate limiting (findings 5, 6) with provider-latency runs against a real
  endpoint, which needs the owner's go-ahead (cost and hosted service).

## Not verified

- Any real provider behaviour: latency distribution, 429 behaviour, connection reuse,
  streaming, token costs. The 2000 ms run is a fixed sleep, not a provider.
- The cause of the plateau and of the overhead growth (GIL against SQLite writer lock against
  loop/thread churn). No profiler was run; finding 2 is a pattern, not an attribution. The
  possible link between the +52 connections at c=64 and write retries under lock contention is a
  guess.
- Variance: each configuration was run once on a shared 4-vCPU host. The earlier run of the same
  commands on a different commit (`e00ac25`, not in this branch's history; its numbers are in the
  git history of the JSON files) gave 79 / 221 / 217 / 213 docs/min, so c=64 differs by about 15%
  between the two runs (213 against 181) while c=1 differs by under 1%. The drift down above c=4 may
  be partly noise; "does not rise above c=4" is what the two runs agree on. No confidence
  intervals.
- Steady state: all workers start at t=0, so the high-concurrency p50/p95 (intake especially) include
  a start-up convoy. A ramped or longer run was not done.
- Other storage back ends. Postgres (`DATABASE_URL`) was not benchmarked; the SQLite
  single-writer behaviour may not apply.
- The embedded watcher and API (dispatch, claim sweeps, ops monitor, relations pass) were
  not running; only `run_pipeline` was driven.
- The client-sharing check covers client construction in the installed packages; it was not
  watched under real network traffic.
- The corpus is synthetic (repository fixtures with a unique marker line), not real documents.
