# Graph throughput baseline (Phase 0): where one process stops scaling

- **Date**: 2026-11-17 (UTC)
- **Kind**: evaluations (offline load benchmark; no real model, no hosted service)
- **Status**: baseline recorded on base `f83e398` plus the benchmark script (measured at commit
  `e00ac25f84ffaf27ea6d3c867b39cf82709069f3`, clean tree). Nothing in the pipeline was changed.

> **Mock-LLM numbers.** Every LLM call is answered by the repository's fake LLM after a
> fixed sleep (default 200 ms). Absolute docs/min are therefore NOT production numbers, and a
> real provider's latency, rate limits, TLS/connection setup and queueing are absent. What
> the data supports: the shape across concurrency levels, the per-document work the pipeline
> does around the LLM, and the non-LLM overhead that grows with concurrency.

## Scope

How the real graph (`graph.build_graph.run_pipeline`, with the real bins, SQLite catalog and
hash-chained audit log) behaves as the number of documents in flight grows, and which of the
five suspected bottlenecks (below) the numbers confirm. The script is
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
  `ThreadPoolExecutor` of `c` workers, calling `run_pipeline` directly. The watcher dispatch
  cap (`watcher.max_concurrent_docs`, 4) is therefore **not** in play: levels above 4 show
  what the pipeline does when something lets more documents in.
- LLM seams: `llm.client.OpenAI` and `langchain_agents.base_agent.ChatOpenAI` are replaced by
  counting factories that still construct the **real** client (so its construction cost is
  paid) and then fake only the request. Retry backoff sleeps are shortened by mutating the
  taxonomy `llm_retry` block in the worker process (`--retry-base-delay-ms`, default 50 ms;
  both retry layers read it), so injected 429s cost little time here by construction.
- Counted without changing code: `graph.build_graph._bounded` (per-node wall time, LLM time,
  heartbeat time), `_touch_heartbeat`, `_run_coro`, `asyncio.run`,
  `storage.db.create_async_engine` and `storage.db._apply_sqlite_pragmas` (runs once per new
  raw SQLite connection). CPU is `getrusage` of the worker process.
- Latency is per document from claim to returned state; it excludes time queued behind the
  pool. "Doc overhead" is end-to-end latency minus the injected LLM sleep (it includes retry
  sleeps when 429s are injected).

**Machine**: Linux 6.18 x86_64 cloud VM, 4 vCPU, 15.7 GB RAM, Python
3.13.18, SQLite via aiosqlite, load average about 2.5 at start of the main run; other
sessions may have shared the host.

**Commands** (from the repository root, `.venv` active; JSON files sit next to this report):

```bash
# main baseline, ~3.5 min
PYTHONPATH=src python src/scripts/bench_throughput.py \
  --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline.json --md-out out.md
# 20% injected 429s, one level, ~15 s
PYTHONPATH=src python src/scripts/bench_throughput.py --levels 4 --docs 48 --rate-limit-rate 0.2 \
  --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline-rate-limit-0.2.json
# realistic provider latency, 2 levels, ~2 min
PYTHONPATH=src python src/scripts/bench_throughput.py --levels 4,16 --docs 48 --latency-ms 2000 \
  --json-out docs/reports/evaluations/2026-11-17-graph-throughput-baseline-latency-2000ms.json
```

The first command ran on a clean tree at the commit above; the other two ran a few
minutes later, at the same commit, with the first run's JSON present as an untracked file
(`tree_clean: false` in their JSON).

## Baseline (128 documents per level, 200 ms per LLM call, no injected 429s)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | doc overhead p50 ms | LLM calls | OpenAI ctor | ChatOpenAI ctor | heartbeats | heartbeat s (sum) | event loops | DB engines | DB connections | CPU cores busy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 128 / 0 | 97.1 | 79 | 616 | 723 | 216 | 346 | 62 | 188 | 733 | 4.3 | 2567 | 2567 | 3943 | 0.26 |
| 4 | 128 / 0 | 34.8 | 221 | 919 | 1251 | 519 | 346 | 62 | 188 | 733 | 10.6 | 2567 | 2567 | 3943 | 0.94 |
| 16 | 128 / 0 | 35.4 | 217 | 4011 | 6982 | 3601 | 346 | 62 | 188 | 733 | 87.0 | 2567 | 2567 | 3943 | 1.24 |
| 64 | 128 / 0 | 36.0 | 213 | 14587 | 26656 | 13981 | 346 | 62 | 188 | 733 | 480.1 | 2567 | 2567 | 3983 | 1.24 |

Per node, p50 / p95 in ms (invocations):

| node | c=1 | c=4 | c=16 | c=64 |
|---|---:|---:|---:|---:|
| intake | 49 / 62 (128) | 121 / 267 (128) | 1062 / 1908 (128) | 4294 / 10084 (128) |
| classify | 225 / 235 (128) | 260 / 392 (128) | 529 / 1670 (128) | 1422 / 5973 (128) |
| retry_classify | 209 / 210 (13) | 223 / 314 (13) | 300 / 685 (13) | 488 / 3236 (13) |
| extract | 239 / 257 (112) | 278 / 357 (112) | 635 / 1153 (112) | 1285 / 5815 (112) |
| compile_report | 7 / 9 (112) | 14 / 33 (112) | 88 / 598 (112) | 159 / 2821 (112) |
| catalog_write | 24 / 29 (112) | 52 / 128 (112) | 369 / 1350 (112) | 1583 / 6575 (112) |
| archive | 51 / 61 (112) | 126 / 243 (112) | 776 / 1974 (112) | 1788 / 8043 (112) |
| human_review | 42 / 51 (16) | 105 / 157 (16) | 602 / 903 (16) | 3615 / 10941 (16) |

Per node, mean ms spent in injected LLM latency / everything else / the heartbeat (the
heartbeat is part of "everything else"):

| node | c=1 | c=4 | c=16 | c=64 |
|---|---:|---:|---:|---:|
| intake | 0 / 56 / 0 | 0 / 140 / 0 | 0 / 1130 / 1 | 0 / 4805 / 3 |
| classify | 333 / 26 / 7 | 333 / 70 / 19 | 333 / 416 / 143 | 333 / 1825 / 913 |
| extract | 214 / 39 / 7 | 214 / 82 / 17 | 214 / 498 / 126 | 214 / 1922 / 692 |
| compile_report | 0 / 9 / 9 | 0 / 16 / 15 | 0 / 146 / 145 | 0 / 667 / 665 |
| catalog_write | 0 / 24 / 7 | 0 / 62 / 16 | 0 / 529 / 121 | 0 / 2398 / 750 |
| archive | 0 / 52 / 6 | 0 / 137 / 17 | 0 / 874 / 180 | 0 / 2798 / 948 |
| human_review | 0 / 42 / 8 | 0 / 98 / 19 | 0 / 603 / 131 | 0 / 4281 / 724 |

CPU seconds (user + system) for the same 128 documents: 25.0 s at c=1, 32.7 s at c=4,
44.0 s at c=16, 44.6 s at c=64.

### Rate-limit run (c=4, 48 documents, 20% of calls answered with an injected 429)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | LLM calls | 429s injected | heartbeats | event loops | DB connections |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 48 / 0 | 13.4 | 215 | 905 | 1358 | 143 | 23 | 278 | 973 | 1491 |

All 48 documents completed through both retry layers; 23 of 143 attempts were rate-limited
(`llm/retry.py` and the LangChain agents' `_invoke_with_retry`). With the 50 ms backoff the
wall time matches a clean run (13.1 s for 48 documents, scaled from the c=4 baseline).

### Provider-like latency (2000 ms per LLM call, 48 documents)

| concurrency | docs ok / failed | wall s | docs/min | e2e p50 ms | e2e p95 ms | doc overhead p50 ms | LLM calls | CPU cores busy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 48 / 0 | 66.9 | 43 | 4363 | 4912 | 363 | 120 | 0.16 |
| 16 | 48 / 0 | 53.6 | 54 | 5724 | 8273 | 1662 | 120 | 0.26 |

The 48-document mix contains one long document. It alone took 52.5 s end to end (26
sequential LLM calls), which is the whole wall time of the c=16 run.

## Findings

1. **One process stops scaling at about 4 documents in flight, and it is not the LLM.**
   Throughput goes 79, 221, 217, 213 docs/min for c = 1, 4, 16, 64. The c=4 gain is 2.8x (not
   4x). Beyond that, median per-document latency grows linearly (0.9 s, 4.0 s, 14.6 s) while
   throughput stays flat: documents queue inside the process. The overhead per document that
   is not LLM time goes from 216 ms (c=1) to 13,981 ms (c=64), 65x. The process keeps only
   1.24 of 4 cores busy at c=16 and c=64, and the CPU it burns for the same 128 documents
   grows from 25.0 s to 44.6 s (+78%, mostly system time), i.e. contention adds work.
   Where exactly it serializes (the GIL, the SQLite single-writer lock behind
   `BEGIN IMMEDIATE`, or thread and loop churn) is **not isolated** by this benchmark.
2. **Suspect 2 (event loop + engine + `NullPool` connection per storage call): confirmed, and
   it is the dominant scaling cost.** A document costs 20 `asyncio.run` event loops, 20 new
   SQLAlchemy engines and about 31 raw SQLite connections (2567, 2567 and 3943 for 128
   documents), at every concurrency level. The heartbeat is called 733 times (5.7 per
   document, but `intake` runs none because the document has no id yet, so about 605 of them
   open a loop: about 24% of the loops). Per loop-opening heartbeat the time summed over
   threads is 7 ms at c=1, 17 ms at c=4, 144 ms at c=16 and 794 ms at c=64, about 26% of
   the median document latency at c=64. But the other storage calls hurt as much: `intake`, which runs no heartbeat, has a p50 of
   4.3 s at c=64 (49 ms at c=1), and `catalog_write`, `archive`, `human_review` and
   `compile_report` degrade the same way. So the fix surface is the whole storage call
   pattern (`graph.build_graph._run_coro`, `storage.db`), not only `_touch_heartbeat`.
3. **Suspect 1 (an HTTP client per agent instance): the pattern is confirmed, a CPU cost is
   refuted.** 250 clients were built for 128 documents (62 `OpenAI` through `get_llm`, 188
   `ChatOpenAI` through the vendored LangChain agents; 1.95 per document), and none is
   reused across documents. Constructing them cost 0.69 s in total at c=1 (about 5 ms per
   document) and 0.98 s at c=64, under 1% of wall time. What a fresh client pool costs
   against a real provider (a TCP and TLS handshake per new pool, no keep-alive reuse across
   documents) cannot be measured with a fake request: **not verified**.
4. **Suspect 5 (windows run one after another): confirmed, and larger than the brief
   assumed.** A single ~230,000-character document made 23 sequential LLM calls in the
   `classify` node (the sorter reading it in windows, inferred from the call count) and 3
   in `extract`: 26 calls, 5.6 s end to end at 200 ms
   per call (a short document takes about 0.6 s), and 52.5 s at 2000 ms per call. At the
   provider-like latency that one document is the makespan of a 48-document run (53.6 s at
   c=16 against 66.9 s at c=4). The sorter's windows, not only the specialists', are the
   serial chain. This benchmark cannot say how common long documents are in the real corpus.
5. **Suspect 4 (reactive rate-limit backoff): exercised, cost not measured.** Both retry
   layers recover from a 20% 429 rate with no failed document. The backoff was shortened to
   50 ms, so the run says nothing about its real cost. For scale only (arithmetic, not
   measured): at the taxonomy defaults (`rate_limit_base_delay` 8 s, doubling, capped at 60 s)
   each 429 holds a worker for at least about 5.6 s (8 s less 30% jitter), so the 23
   rate-limited calls above would idle workers for roughly 2 minutes of worker time, against
   the roughly 54 worker-seconds (4 workers, 13 s) the whole run used. There
   is no limiter that keeps calls below a provider's rate, so concurrency and 429s feed each
   other.
6. **Suspect 3 (the watcher cap of 4): not exercised, but the data bounds it.** Raising the
   cap above 4 would not help on this machine with a 200 ms provider, because throughput is
   already flat from c=4 (221, 217, 213 docs/min). At 2000 ms per call the picture reverses
   (the process is mostly idle, 0.16 to 0.26 cores busy) and the cap would bind, but only
   two levels at 48 documents were run and the long-document tail dominates them, so the
   benefit of a higher cap at realistic latency is **not measured**.
7. **Work per document**: 2.7 LLM calls (346 for 128), 5.7 heartbeats, 20 event
   loops and 31 SQLite connections, identical at every level, so the work per document is
   constant and only its cost under contention changes.

## Recommendations (for the next phase, not done here)

- Start with the storage call pattern (finding 2): a long-lived loop or sync path per worker
  and a reused engine/connection for catalog, audit and heartbeat writes. It is the only item
  whose cost grows with concurrency in these numbers. Re-run this benchmark after each change;
  the counters (`event loops`, `DB engines`, `DB connections`, `heartbeat s`) must fall, and
  `docs/min` at c=16 and c=64 must rise.
- Make the sorter and specialist windows concurrent, or bound their number, before raising
  the watcher cap (finding 4).
- Only then reuse LLM clients across documents (finding 3): small CPU win, unknown network win.
- Treat the cap and rate limiting (findings 5, 6) with provider-latency runs against a real
  endpoint, which needs the owner's go-ahead (cost and hosted service).

## Not verified

- Any real provider behaviour: latency distribution, 429 behaviour, connection reuse,
  streaming, token costs. The 2000 ms run is a fixed sleep, not a provider.
- The cause of the plateau (GIL against SQLite writer lock against loop/thread churn). No
  profiler was run.
- Other storage back ends. Postgres (`DATABASE_URL`) was not benchmarked; the SQLite
  single-writer behaviour may not apply.
- Variance: each configuration was run once (the earlier exploratory run of the same
  configuration gave 79 / 230 / 219 / 207 docs/min, within 4% of the recorded one). No
  confidence intervals.
- The embedded watcher and API (dispatch, claim sweeps, ops monitor, relations pass) were
  not running; only `run_pipeline` was driven.
- The corpus is synthetic (repository fixtures with a unique marker line), not real documents.
