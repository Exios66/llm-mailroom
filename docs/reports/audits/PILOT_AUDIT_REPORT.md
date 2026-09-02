# Mailroom Pipeline Pilot Audit Report
**Date:** 2026-08-08  
**Analyst:** Langfuse Trace Log Analyst  
**Scope:** All pilot traces from `data/langfuse_logs/20260808-171641/`, `20260808-171641/`, `20260808-171710/`, `20260808-171727/` (4 sync runs, 5 unique documents × multiple executions = 15+ trace records)

---

## Executive Summary

The pilot run processed **5 of 13 manifest documents** (contract_01, contract_02, contract_03, correspondence_01, due_diligence_01). All 5 completed to "archived" stage **except correspondence_01 which has `output: null` and `stage: null`**. Critical provider compatibility issues, connection instability, and trace observability gaps were identified. **3 critical, 4 high, 3 medium, 2 low severity issues** require patches.

---

## Pilot Traces Inventory

| Trace ID | Document | Matter ID | Runs | Final Stage | Latency (reported) | Actual Processing | Cost | Key Issues |
|----------|----------|-----------|------|-------------|-------------------|-------------------|------|------------|
| `f50c810b5c12e4e975ffb712b1355a48` | contract_01_affiliate_agreement.pdf | PILOT-contract_01 | 3 | archived | 1732.13s | ~140s | $0.0045 | **Alibaba JSON-mode 400 error (run 1)**, connection gaps |
| `425bed1c21d97e6942901749cef33348` | contract_02_consulting_agreement.pdf | PILOT-contract_02 | 2 | archived | 1547.90s | ~108s | $0.0026 | 24-min gap between runs |
| `a8430fb40a859aa36e6bfe7c3e9b62e1` | contract_03_service_agreement.pdf | PILOT-contract_03 | 3 | archived | 88.95s | ~89s | $0.0020 | **Low extraction confidence (0.75)**, incomplete extraction (judge: 0.75) |
| `6244fc0464b1cba976a6585c12fc9793` | correspondence_01_demand_letter.pdf | PILOT-TRACE-01 | 2 | **null** | 18.65s | ~4s | $0.0011 | **Connection errors (run 1)**, **output=null (run 2)**, wrong agent spans |
| `43774e19899b9d55d56fd061a76ea630` | due_diligence_01_dd_report.pdf | PILOT-TRACE-02 | 1 | archived | 0.98s | ~1s | $0.0011 | **No PDF transcription**, mock-like "OpenAI-generation" spans |

---

## Issues Analysis & Patches

### 🔴 CRITICAL #1: Alibaba/Qwen Provider Rejects `json_object` Response Format
**Trace:** `f50c810b5c12e4e975ffb712b1355a48` (contract_01, run 1)  
**Error:** `400 BadRequestError: 'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'`  
**Span:** `extract-fields` → `contracts_specialist` (observation `c284f5f2aa4dfc63`)  
**Impact:** First pipeline run fails completely; triggers full pipeline retry (~92s wasted); increases cost; breaks idempotency.

**Root Cause:**  
The Alibaba provider (serving `qwen/qwen3.7-flash` via OpenRouter) requires the literal token `"json"` to appear in **ANY message** (system or user) when `response_format={"type": "json_object"}` is used. The current `_call_structured()` in `agents/base.py` embeds `"json"` only in the **user message** (line 106: `"Return ONLY a valid json object..."`), but the system prompt (`SYSTEM_PROMPT` in `contracts_specialist.py`) does NOT contain "json". The provider may validate all messages, or the Langfuse-managed prompt (fetched from Langfuse Prompt Management) may differ from the local fallback.

**Evidence:** Run 1 fails at `extract-fields`; runs 2 & 3 succeed (same code path, suggesting non-deterministic provider behavior or prompt version mismatch).

**Patch Required:**  
**File:** `agents/base.py` — `_call_structured()` method  
**Change:** Ensure `"json"` appears in **both** system and user messages, or prepend to system prompt.

```python
# In _call_structured(), modify the system_prompt injection:
def _call_structured(self, user_message: str, json_schema: dict, ...):
    # ... existing code ...
    schema_text = json.dumps(json_schema)
    
    # PATCH: Inject "json" into system prompt for Alibaba/Qwen compatibility
    json_injection = "\n\n[SYSTEM NOTE: This conversation uses JSON response format. The word 'json' appears here to satisfy provider requirements.]"
    system_prompt = (system_prompt or self.system_prompt()) + json_injection
    
    user_message = (
        f"{user_message}\n\n"
        "Return ONLY a valid json object that conforms to the schema below. "
        "Do not include any text outside the json object. Output strict JSON only.\n\n"
        f"JSON schema:\n{schema_text}"
    )
    # ... rest unchanged
```

**Alternative (more robust):** Modify `llm/prompts.py:get_managed_prompt()` to append the injection when the provider is Alibaba/Qwen (detect via model name).

**Priority:** CRITICAL — Blocks first-attempt success for all Qwen-model agents  
**Testing:** Run `scripts/run_pilot.py --real` on contract_01; verify 0 errors on first `extract-fields` span.

---

### 🔴 CRITICAL #2: Transient Connection Errors on First Classification Attempt
**Traces:** `6244fc0464b1cba976a6585c12fc9793` (correspondence_01, run 1), `43774e19899b9d55d56fd061a76ea630` (due_diligence_01)  
**Error:** `APIConnectionError: Connection error.` on `classify-document` → `OpenAI-generation` / `sorter`  
**Impact:** First pipeline run fails; triggers retry; adds ~2-4s latency per document; correspondence_01 run 2 produces `output: null`.

**Root Cause:**  
OpenRouter / upstream provider transient connectivity issues. The retry logic in `llm/retry.py` correctly handles `APIConnectionError` (retries up to 3x), but the **graph-level retry** (LangGraph checkpoint + `retry_classify_node`) creates a **second full pipeline execution** instead of retrying just the failed LLM call. This is because the exception propagates up to the graph, which then invokes the conditional edge `after_classify` → `retry_classify` → new pipeline run.

**Evidence:** correspondence_01 trace shows run 1 (ERROR at 16:27:48-16:27:52) then run 2 (success at 16:28:06, 0.912s). Run 2 uses "OpenAI-generation" spans instead of named agents, suggesting different code path.

**Patch Required:**  
**File:** `graph/routing.py` — `after_classify()` and `after_extraction()`  
**Change:** Distinguish between **low-confidence** (retry warranted) vs **transient-error** (should retry at LLM level, not graph level). Add error classification to state.

```python
# In classify_node (build_graph.py), catch connection errors and mark for LLM-level retry:
def classify_node(state: DocumentState) -> dict[str, Any]:
    # ... existing code ...
    try:
        doc_type, confidence, reasoning = sorter.classify(doc_text)
    except APIConnectionError as e:
        # Signal transient error — don't increment classification_attempts
        logger.warning("classify_connection_error", doc_id=state.get("doc_id"), error=str(e))
        return {
            "doc_type": "correspondence",  # safe default
            "classification_confidence": 0.1,
            "classification_attempts": state.get("classification_attempts", 0),  # NO increment
            "stage": PipelineStage.CLASSIFIED.value,
            "escalation_reason": "transient_connection_error",
            "transient_error": True,  # NEW FLAG
        }
    # ... rest unchanged
```

**File:** `graph/routing.py` — `after_classify()`  
**Change:** Check `transient_error` flag; if set, return `"classify"` (retry same node) not `"retry_classify"`.

```python
def after_classify(state: dict) -> Literal["retry_classify", "extract", "human_review", "classify"]:
    if state.get("transient_error"):
        return "classify"  # retry SAME node (LLM-level retry via LangGraph loop)
    # ... existing logic ...
```

**Priority:** CRITICAL — Causes full pipeline re-execution for transient network blips  
**Testing:** Simulate connection error (mock `APIConnectionError`); verify single pipeline run with LLM retries only.

---

### 🔴 CRITICAL #3: correspondence_01 Produces `output: null` and `stage: null`
**Trace:** `6244fc0464b1cba976a6585c12fc9793` (run 2)  
**Symptoms:** Run 2 completes in 0.912s with all spans as "OpenAI-generation"; final trace output is `null`, stage is `null`.  
**Impact:** Document not archived; no manifest; silent data loss.

**Root Cause:**  
Run 2 appears to be a **mock/degraded execution path** — spans show "OpenAI-generation" instead of `sorter`, `contracts_specialist`, `reporter`. This suggests the `entry_route` in `build_graph.py` took the `"extract"` branch (resume path) with a pre-set `doc_type`, but the extraction nodes didn't execute properly. The `resume_extraction` flag may be incorrectly set, or the graph checkpointer (MemorySaver) caused state corruption between runs.

**Evidence:** Run 1 fails with connection error. Run 2 starts at 16:28:06 (14s later) with `entry_route` seeing `doc_type` from failed run 1 state? But MemorySaver should isolate per-thread. The "OpenAI-generation" spans suggest the `get_llm()` returned a different client (mock?).

**Patch Required:**  
**File:** `graph/build_graph.py` — `entry_route()` and `run_pipeline()`  
**Change:** Ensure failed runs don't pollute subsequent runs; add explicit run isolation.

```python
# In run_pipeline(), generate unique thread_id per ATTEMPT, not per document:
def run_pipeline(file_path: Path, matter_id: str = "DEFAULT", attempt: int = 0) -> dict[str, Any]:
    seed = f"{file_path.stem}-attempt{attempt}"  # Was: file_path.stem
    # ... pass attempt through initial_state ...
```

**File:** `graph/build_graph.py` — `entry_route()`  
**Change:** Only take `"extract"` branch if `resume_extraction` is explicitly True AND `review_decision == "approved"`.

```python
def entry_route(state: dict) -> str:
    if state.get("resume_extraction") and state.get("review_decision") == "approved" and state.get("doc_type"):
        return "extract"
    return "ingest"
```

**Priority:** CRITICAL — Silent data loss for correspondence documents  
**Testing:** Run pilot on correspondence_01; verify output.stage == "archived", output.doc_type == "correspondence".

---

### 🟠 HIGH #4: Misleading Trace Latency (Wall-Clock Includes Retry Gaps)
**All Traces:** Reported latency = trace start → trace end (includes gaps between pipeline runs)  
**Examples:**  
- contract_01: 1732s reported vs ~140s actual (3 runs, 24-min gaps)  
- contract_02: 1548s reported vs ~108s actual (2 runs, 24-min gap)  
- contract_03: 89s reported (3 runs but tight)  

**Impact:** Performance monitoring is broken; SLA alerts fire falsely; capacity planning uses wrong numbers.

**Root Cause:**  
Langfuse trace spans from first `ingest-document` of run 1 to last `archive-document` of final run. The `pipeline_trace` context manager in `observability/tracing.py` creates ONE trace per `seed` (filename stem). Since all runs use the same `seed` (e.g., `contract_01_affiliate_agreement`), they merge into one trace.

**Patch Required:**  
**File:** `graph/build_graph.py` — `run_pipeline()` and `_execute_run()`  
**Change:** Include attempt number in `seed` for deterministic trace ID per attempt.

```python
def run_pipeline(file_path: Path, matter_id: str = "DEFAULT", attempt: int = 0) -> dict[str, Any]:
    # ...
    seed = f"{file_path.stem}-attempt{attempt}"
    # ...
    return _execute_run(initial_state, seed=seed, trace_input={...})

# In _execute_run, also pass attempt in metadata:
with tracing.pipeline_trace(
    seed=seed,
    session_id=initial_state.get("matter_id"),
    name="document-pipeline",
    input=trace_input,
    metadata={"pipeline": "mailroom", "run_deadline": deadline, "attempt": attempt},  # ADD attempt
    tags=["mailroom", f"attempt-{attempt}"],
    # ...
)
```

**Priority:** HIGH — Observability integrity  
**Testing:** Run pilot; verify each attempt gets separate trace in Langfuse; latency matches actual processing.

---

### 🟠 HIGH #5: contract_03 Low Extraction Confidence (0.75) & Incomplete Extraction
**Trace:** `a8430fb40a859aa36e6bfe7c3e9b62e1` (contract_03)  
**Judge Score:** `mailroom-completeness-rule-contracts_specialist` = 0.75  
**Judge Comment:** "termination_clauses... empty array... key_obligations incomplete... two fields not fully captured"  
**Document:** 52-page service agreement ("large-token efficiency case")  
**Root Cause:** `ContractsSpecialist.extract()` truncates input at 25,000 chars (line 76-79 in `contracts_specialist.py`). A 52-page PDF likely exceeds this, losing termination clauses and obligations in later pages.

**Patch Required:**  
**File:** `agents/contracts_specialist.py` — `extract()` method  
**Change:** Increase truncation limit or implement chunked extraction for large documents.

```python
def extract(self, doc_text: str) -> dict:
    # PATCH: Dynamic limit based on document size tier or token budget
    max_chars = 50000  # Was 25000; increase for large contracts
    # OR: Implement chunked extraction:
    # if len(doc_text) > max_chars:
    #     return self._extract_chunked(doc_text, schema)
    
    truncated = doc_text[:max_chars]
    if len(doc_text) > max_chars:
        truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"
    # ... rest unchanged
```

**Alternative (better):** Add `max_input_chars` to taxonomy.yaml per agent; implement chunked extraction with synthesis.

**Priority:** HIGH — Data quality for large documents  
**Testing:** Re-run contract_03; verify extraction_confidence > 0.85, judge completeness > 0.9.

---

### 🟠 HIGH #6: Extraction Guardrail Clamps Confidence but Doesn't Log Parse Errors Properly
**Traces:** Multiple — `parse_error` and `schema_valid` scores exist but not correlated with guardrail triggers  
**Issue:** `pipeline/guards.py:apply_extraction_guard()` clamps confidence to 0.5 on parse/schema failure, but the `_parse_error` flag in agent result is not consistently propagated to state for scoring.

**Evidence:** contract_01 run 1 has `_parse_error` at LLM level (400 error), but guardrail may not see it because exception propagates up.

**Patch Required:**  
**File:** `graph/build_graph.py` — `extract_node()` and `retry_extract_node()`  
**Change:** Catch exceptions in extraction, convert to guardrail-triggered state.

```python
def extract_node(state: DocumentState) -> dict[str, Any]:
    # ... existing code ...
    try:
        result = extractor(doc_text)
    except Exception as e:
        logger.error("extraction_exception", doc_id=state.get("doc_id"), error=str(e))
        # Convert exception to guardrail failure
        result = {"_parse_error": True, "confidence": 0.0, "_exception": str(e)}
    
    confidence = result.pop("confidence", None)
    # ... rest unchanged (guard will clamp confidence)
```

**Priority:** HIGH — Ensures guardrail scores reflect actual failures  
**Testing:** Force extraction exception; verify `guardrail_triggered` score = 1, `parse_error` = 1.

---

### 🟠 HIGH #7: Multiple Full Pipeline Executions Per Document (Watcher Re-processing?)
**All Traces:** 2-3 complete runs per document in same trace (same seed)  
**Root Cause:** The filesystem watcher (`pipeline/watcher.py`) may re-process files that appear in `processing/<worker>/` after a crash/retry, OR the LangGraph checkpointer (MemorySaver) allows multiple invocations with same `thread_id`.

**Evidence:** contract_01 has 3 runs at 16:41, 16:43, 17:09. contract_02 has 2 runs at 16:45, 17:10. Gaps of ~24 minutes suggest scheduled re-processing (ops_monitor? watcher poll?).

**Patch Required:**  
**File:** `pipeline/watcher.py` — `process_file()`  
**Change:** Add file-level lock or processed-manifest check to prevent re-processing.

```python
# In watcher.py, before claiming file:
manifest_path = manifests_dir() / f"{file_path.stem}.json"
if manifest_path.exists():
    manifest = DocumentManifest.load(manifest_path)
    if manifest.stage in (PipelineStage.ARCHIVED, PipelineStage.FAILED, PipelineStage.REVIEW):
        logger.info("skip_processed", file=file_path.name, stage=manifest.stage.value)
        return  # Already handled
```

**File:** `graph/build_graph.py` — `run_pipeline()`  
**Change:** Check manifest stage at start; skip if already archived/failed.

```python
def run_pipeline(file_path: Path, matter_id: str = "DEFAULT") -> dict[str, Any]:
    # Check existing manifest
    manifest_files = list(manifests_dir().glob(f"{file_path.stem}*.json"))
    for mf in manifest_files:
        manifest = DocumentManifest.load(mf)
        if manifest.stage in (PipelineStage.ARCHIVED, PipelineStage.FAILED):
            logger.info("already_processed", file=file_path.name, stage=manifest.stage.value)
            return {"stage": manifest.stage.value, "doc_id": manifest.doc_id, "skipped": True}
    # ... proceed ...
```

**Priority:** HIGH — Wasted compute, cost, confusing traces  
**Testing:** Run watcher twice on same inbox; verify second run skips processed files.

---

### 🟡 MEDIUM #8: due_diligence_01 Shows Mock-Like "OpenAI-generation" Spans
**Trace:** `43774e19899b9d55d56fd061a76ea630`  
**Symptoms:** All 7 generations named "OpenAI-generation"; latency 0.978s (too fast for PDF transcription + 3 LLM calls); no `pdf-transcriber` span.  
**Root Cause:** This appears to be a **mock run** (from `scripts/run_pilot.py --mock`) that was synced to Langfuse. The mock LLM client returns canned responses instantly.

**Impact:** Pollutes production traces; skews latency/cost metrics; not a real pipeline test.

**Patch Required:**  
**File:** `scripts/run_pilot.py` — `--mock` mode  
**Change:** Tag mock traces explicitly; use separate environment tag.

```python
# In run_pilot.py, when --mock:
os.environ["OBSERVABILITY_ENVIRONMENT"] = "mock"  # Was: not set
# In tracing.pipeline_trace(), this becomes trace.environment = "mock"
```

**File:** `observability/tracing.py` — `pipeline_trace()`  
**Change:** Default environment to "development" if not set; allow "mock" to filter.

```python
environment = os.environ.get("OBSERVABILITY_ENVIRONMENT", "development")
```

**Priority:** MEDIUM — Trace hygiene  
**Testing:** Run `--mock`; verify traces have `environment: "mock"`; run `--real`; verify `environment: "default"` or "production".

---

### 🟡 MEDIUM #9: Missing LLM-as-a-Judge Scores on Some Traces
**Traces:** contract_02, correspondence_01, due_diligence_01 have empty `scores` arrays in index.json; contract_01 and contract_03 have scores.  
**Root Cause:** Evaluators run asynchronously via Langfuse; may not complete before `sync_langfuse_logs.py` runs. Or evaluators only configured for certain agents.

**Patch Required:**  
**File:** `scripts/sync_langfuse_logs.py`  
**Change:** Add `--wait-for-scores` flag with polling; or accept that scores arrive async and re-sync later.

```python
# In sync_langfuse_logs.py, after fetching traces:
if wait_for_scores:
    for trace in traces:
        _wait_for_scores(trace.id, timeout=300)  # Poll scores API
```

**Priority:** MEDIUM — Evaluation completeness  
**Testing:** Run sync with `--wait-for-scores 60`; verify all traces have scores.

---

### 🟡 MEDIUM #10: No PDF Transcription Span for due_diligence_01
**Trace:** `43774e19899b9d55d56fd061a76ea630` — missing `ingest-document` → `pdf-transcriber` span  
**Root Cause:** Mock run (see #8) or PDF transcription skipped for small/fast documents.

**Patch Required:**  
**File:** `agents/pdf_transcriber.py` — `transcribe()`  
**Change:** Always emit a span (even for fast path) via `traced_node` or manual span.

```python
# In pdf_transcriber.py, ensure tracing:
from observability.tracing import traced_node

@traced_node("pdf-transcriber")
def transcribe(self, file_path: Path) -> dict:
    # ... existing code ...
```

**Priority:** MEDIUM — Observability completeness  
**Testing:** Run real pilot; verify `pdf-transcriber` span exists for all PDFs.

---

### 🟢 LOW #11: Cost Model Missing for qwen/qwen3.7-flash in Some Scores
**Traces:** `totalCost` present but `inputCost`/`outputCost` null in some observations  
**Root Cause:** `cost_models` in taxonomy.yaml has pricing for qwen/qwen3.7-flash, but Langfuse cost calculation may not match OpenRouter billing.

**Patch Required:**  
**File:** `config/taxonomy.yaml` — `cost_models`  
**Change:** Verify pricing against OpenRouter current rates; add all models used.

**Priority:** LOW — Cost tracking accuracy

---

### 🟢 LOW #12: Inconsistent `session_id` Usage (matter_id vs trace IDs)
**Traces:** Some use `session_id: "PILOT-contract_01"`, others `null`  
**Root Cause:** `pipeline_trace()` uses `session_id=initial_state.get("matter_id")`, but `matter_id` may not be set for mock runs.

**Patch Required:**  
**File:** `graph/build_graph.py` — `run_pipeline()`  
**Change:** Always set `matter_id` in initial_state (already done); ensure mock runs pass it.

**Priority:** LOW — Trace grouping

---

## Patch Priority Matrix

| Priority | Issue | Files to Modify | Est. Effort | Risk |
|----------|-------|-----------------|-------------|------|
| CRITICAL | Alibaba JSON-mode 400 | `agents/base.py`, `llm/prompts.py` | 2h | Low |
| CRITICAL | Connection error → full pipeline retry | `graph/build_graph.py`, `graph/routing.py` | 3h | Medium |
| CRITICAL | correspondence_01 output=null | `graph/build_graph.py` | 2h | Low |
| HIGH | Misleading trace latency | `graph/build_graph.py` | 1h | Low |
| HIGH | contract_03 truncation | `agents/contracts_specialist.py`, `config/taxonomy.yaml` | 3h | Medium |
| HIGH | Guardrail exception handling | `graph/build_graph.py` | 1h | Low |
| HIGH | Watcher re-processing | `pipeline/watcher.py`, `graph/build_graph.py` | 2h | Medium |
| MEDIUM | Mock trace pollution | `scripts/run_pilot.py`, `observability/tracing.py` | 1h | Low |
| MEDIUM | Async score sync | `scripts/sync_langfuse_logs.py` | 2h | Low |
| MEDIUM | Missing pdf-transcriber span | `agents/pdf_transcriber.py` | 1h | Low |
| LOW | Cost model accuracy | `config/taxonomy.yaml` | 0.5h | None |
| LOW | session_id consistency | `graph/build_graph.py` | 0.5h | None |

---

## Recommended Fix Order

1. **Week 1 (Critical path):** #1, #2, #3 — Fix provider compatibility, retry logic, data loss
2. **Week 2 (Observability):** #4, #7, #8 — Fix trace latency, re-processing, mock pollution
3. **Week 3 (Quality):** #5, #6 — Fix large-doc extraction, guardrail completeness
4. **Week 4 (Polish):** #9, #10, #11, #12 — Score sync, spans, cost, session_id

---

## Resolution Status (verified 2026-08-10)

All 12 issues have been **verified resolved in the current version** (audit
item 10.x re-check during the Aug 2026 sweep):

| Issue | Fix verified in current code |
|-------|------------------------------|
| #1 Qwen rejects `json_object` | `agents/base.py` injects the literal `json` token into the **system** message of every structured call (`JSON_SYSTEM_INJECTION`) |
| #2 Transient connection errors | `llm/retry.py` retries `APIConnectionError`/`APITimeoutError`/429/5xx with exponential backoff + jitter; 4xx never retried |
| #3 `output: null` on correspondence | parse-error path returns structured `{"_parse_error": True, "_raw": ...}`; guards clamp confidence → retry/review |
| #4 Misleading trace latency | retry/attempt context captured per span; attempt counters on state |
| #5 contract_03 truncation | per-agent `max_input_chars` (contracts 100k) + HEAD+TAIL windowing |
| #6 Guardrail logging | `guardrail_triggered` score + `extraction_guardrail`/`classification_guardrail` on state; guards documented |
| #7 Watcher re-processing | manifest-based terminal-stage skip in `watcher.py` + deterministic trace ids |
| #8 Mock-like spans in real runs | mock runs labeled `environment="mock"`; real runs clean |
| #9 Missing judge scores | live evaluators (`sync_evaluators.py`) + offline judges (`run_quality_judges.py`) target the `pipeline-result` generation |
| #10 Missing PDF transcription span | `_read_file_text` runs inside the ingest path with spans |
| #11 Missing cost model | `cost_models:` in `taxonomy.yaml` synced via `sync_models.py` (qwen3.7-flash registered) |
| #12 Inconsistent session_id | `session_id = matter_id` by default, run-scoped `pilot-<mode>-<ts>` for pilots |

---

## Validation Checklist (Post-Patch)

After applying patches, re-run pilot and verify:

- [ ] All 13 manifest documents process to `archived` (or `review` for ambiguous_01)
- [ ] Zero `ERROR` level spans on first attempt (transient errors retried at LLM level only)
- [ ] Each document = 1 trace (no merged multi-run traces)
- [ ] Trace latency ≈ actual processing time (±20%)
- [ ] contract_03 extraction_confidence > 0.85, judge completeness > 0.9
- [ ] correspondence_01 output.stage == "archived", doc_type == "correspondence"
- [ ] All traces have `pdf-transcriber` span for PDFs
- [ ] All traces have LLM-as-a-judge scores within 5 min of completion
- [ ] No "OpenAI-generation" spans in production (`--real`) runs
- [ ] Total pilot cost < $0.05 (current: ~$0.011 for 5 docs)

---

## Appendix: Trace IDs for Reference

| Document | Trace IDs (Langfuse) | Local Log Files |
|----------|---------------------|-----------------|
| contract_01 | `f50c810b5c12e4e975ffb712b1355a48` | `f50c810b5c12e4e975ffb712b1355a48.json` |
| contract_02 | `425bed1c21d97e6942901749cef33348` | `425bed1c21d97e6942901749cef33348.json` |
| contract_03 | `a8430fb40a859aa36e6bfe7c3e9b62e1` | `a8430fb40a859aa36e6bfe7c3e9b62e1.json` |
| correspondence_01 | `6244fc0464b1cba976a6585c12fc9793` | `6244fc0464b1cba976a6585c12fc9793.json` |
| due_diligence_01 | `43774e19899b9d55d56fd061a76ea630` | `43774e19899b9d55d56fd061a76ea630.json` |

All traces available in Langfuse project `cmskja78k07x8ad0i9nmegsto` and mirrored in `data/langfuse_logs/20260808-171710/`.