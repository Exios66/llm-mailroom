# LangGraph + Langfuse best-practices audit of the processing pipeline

- **Date**: 2026-08-12
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: complete

## Scope

The full processing pipeline of the mailroom — `pipeline/watcher.py` intake,
the LangGraph state machine in `graph/build_graph.py` (11 nodes, conditional
routing, checkpointing, deadline/budget guards), the agent layer
(`agents/`, `langchain_agents/`), and the Langfuse tracing scaffold
(`observability/`) — audited against the LangGraph and Langfuse best
practices from the installed skills (`.opencode/skills/langgraph-*`,
`langchain-*`, `langfuse`), including the Langfuse LangChain & LangGraph
integration docs. The audit's goal: confirm every agent follows best
practice when running the full pipeline, and verify we are not reimplementing
features that are native to the LangGraph architecture or Langfuse SDK.

## Method

- Skill-grounded checklist: LangGraph state management (reducers, partial
  updates), persistence (checkpointer selection, thread_id), human-in-the-
  loop (interrupt/Command), error handling (RetryPolicy table), invoke
  config (RunnableConfig tags/metadata); Langfuse observability best
  practices (trace naming, tags, environments, sessions, deterministic ids,
  score configs, flush health).
- Code review of `graph/state.py`, `graph/build_graph.py` (node wiring,
  `_bounded` wrapper, `_execute_run`/`run_pipeline` invocation), routing,
  the review-resume flow, and `observability/{tracing,langfuse_setup,
  scores}.py`.
- The Langfuse docs page "LangChain Tracing & LangGraph Integration"
  (fetched 2026-08-12) was used as the reference for what the native
  callback integration can and cannot provide.

## Findings

1. **Checkpointer + thread_id usage follows the persistence skill.**
   `_build_checkpointer()` defaults to `MemorySaver` (deliberately stateless:
   review resume re-invokes the graph from the manifest; the SqliteSaver
   on-disk option remains behind `MAILROOM_CHECKPOINTER=sqlite`). Every run
   passes an attempt-scoped `thread_id` (`{seed}-run{attempt}`), so re-runs
   never resume stale state (the skill's "ALWAYS provide thread_id" rule is
   satisfied; the per-attempt scoping is the correct interpretation for
   one-shot document runs rather than conversations).

2. **State design follows the state-management skill.** Nodes return partial
   update dicts (never mutate-and-return full state); routing is via
   `add_conditional_edges` with an explicit `END` on every branch (no
   infinite loops, START is entry-only); the `messages` channel uses the
   `add_messages` reducer. The remaining list fields (`conflict_details`,
   `classification_guardrail`, `extraction_guardrail`, `doc_pages`) are
   write-once per run by single nodes, so reducers are not required — no
   accumulation bug. The `messages` channel is currently inert (initialized
   empty, never accumulated or read) — harmless; kept to avoid churn.

3. **Error handling maps cleanly to the skill's error table — the hand-
   rolled transient-retry loop is a documented superset of native
   `RetryPolicy`, not a reinvention.** Transient LLM/provider failures are
   caught in-node and surfaced as `transient_error` state, and routing
   self-loops retry the SAME node with a per-node budget. Native
   `RetryPolicy(max_attempts=…)` can only retry exceptions that ESCAPE a
   node and cannot honor the run's wall-clock deadline, the cumulative
   output-token budget, or the audit-trail entries the state-loop writes.
   Unexpected exceptions bubble to `_execute_run`'s catch and finalize as an
   aborted run — exactly the skill's "unexpected → let bubble" guidance.
   No change needed; the mapping is documented for future reviewers.

4. **Human-in-the-loop is a deliberate durability pattern, not a
   reinvention of `interrupt()`.** The review flow (filesystem review bin +
   manifest + `POST /review/{id}/resolve` re-invoking the graph with
   `review_decision`) is a documented architectural choice: the pipeline is
   intentionally stateless so a crash at any point leaves a recoverable
   manifest (startup reconciliation + `recover_processing.py`), whereas
   LangGraph's native `interrupt()`/`Command(resume=…)` requires a
   persistent checkpointer that keeps the whole run's checkpoint alive while
   paused and strands it if the process dies. The native pattern is the
   right one when review latency must not hold a checkpoint; the current
   design wins for crash durability. Recommendation: revisit ONLY if review
   becomes latency-sensitive and a PostgresSaver-backed deployment exists.

5. **Langfuse tracing follows the observability best practices, and the
   hand-rolled scaffold is NOT a reinvention of the native integration.**
   The native LangChain & LangGraph integration
   (langfuse.com/integrations/frameworks/langchain) is a callback handler
   passed in the run config: it auto-names traces, cannot produce
   deterministic per-document trace ids, cannot set per-run sessions/tags/
   environments (handler-level attrs are process-wide), and only fires for
   Runnable components — the pipeline's nodes are plain functions, not
   Runnables. The repo's `pipeline_trace`/`traced_node` scaffold provides
   exactly the documented best practices the native handler cannot:
   deterministic trace ids seeded from the document, session grouping by
   matter/run, mandatory tag taxonomy, environments, curated input/output,
   verb-first span names, and `langfuse_prompt=` linking. Native generation
   auto-tracing (`langfuse.openai` monkeypatch) is used for the LLM calls
   themselves. Score configs are auto-created idempotently; flush health and
   `on_dropped` are wired; `flush()` runs in `finally` and at exit.

6. **NEW (implemented in this audit): the graph run now carries native
   LangGraph `tags` + `metadata` in the invoke `RunnableConfig`.** The same
   classification dimensions sent to Langfuse (environment/run/source tags,
   pipeline/run_deadline/attempt/run_id metadata) are now also propagated
   natively at the graph level, so any callback or LangGraph-native
   instrumentation attached to the run sees them without duplication.
   `graph/invoke` config: thread-scoped attempt + tags + metadata.

7. **Progress/streaming equivalent is native-shaped.** Per-node progress
   heartbeats (touch `updated_at` at every node boundary — L-5) serve the
   "custom" stream-mode role for a batch pipeline; no token streaming is
   needed. The `_bounded` wrapper enforces deadline/budget per node — the
   run-level guard rails the skill expects.

## Recommendations

- **Adopted now**: native RunnableConfig tags/metadata on the graph invoke
  (finding 6) — free, future-proofs any native callback integration.
- **Do not adopt `RetryPolicy`**: the deadline/budget/audit-aware state-loop
  is strictly stronger for this pipeline (finding 3).
- **Do not adopt `interrupt()` for review today**: the manifest-based flow
  survives process death by design (finding 4); revisit with PostgresSaver.
- **Keep the hand-rolled tracing scaffold**: the native LangChain/LangGraph
  callback handler cannot provide deterministic ids, per-run sessions, or
  span plain-function nodes (finding 5); generation auto-tracing is already
  native.
- **Future**: remove the inert `messages` channel when the graph is next
  touched for a real change (finding 2).

## Method

<!-- How: corpus, samples, commands run (scripts/…), thresholds, config. -->

## Findings

<!-- Numbered findings with evidence. -->

## Recommendations

<!-- Actionable next steps. -->
