---
name: langgraph
description: LangGraph pipeline topology for llm-mailroom (13 nodes, routing, HITL, checkpointer). Use when adding/changing graph nodes, conditional edges, review resume, or persistence — invoke before writing graph/agent code.
---

# LangGraph (pipeline state machine)

**When:** Editing `graph/build_graph.py`, `graph/routing.py`, node contracts,
human review, or the checkpointer.  
Do not introduce a second orchestrator.

## Contract

- One graph run per document. Node signature:
  `node(state: DocumentState) -> dict[str, Any]` (partial updates).
- 13 nodes: `intake`, `classify`, `retry_classify`, `review_classify`,
  `extract`, `retry_extract`, `judge_verify`, `arbiter`, `human_review`,
  `boss_escalation`, `compile_report`, `catalog_write`, `archive`.
- Conditional edges live in `graph/routing.py` only.
- Files move through `pipeline/bins.py` — never `os.rename` / `shutil.move`
  in node code.
- Checkpointer: **MemorySaver** default. `MAILROOM_CHECKPOINTER=sqlite` for
  on-disk `data/checkpoints.db` (debug only). Review resume re-invokes from
  the manifest (`resume_from_review`).
- Wrap nodes with `traced_node("verb-first-name")`. Adding a doc class
  touches taxonomy, schema, specialist, dispatch map, prompt template, tests.

## HITL

Park in the review bin; `POST /v1/review/{doc_id}/resolve` approved → fresh
extraction (never reuse the reviewed payload). Reconsideration causes
(`pipeline/reconsideration.py`) can send archived objective misses back to
review before catalog write.

## Depth (vendored)

`.opencode/skills/langgraph-fundamentals`, `langgraph-persistence`
(SqliteSaver), `langgraph-human-in-the-loop`, `langgraph-cli`,
`langchain-fundamentals`, `ecosystem-primer`.

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Tracing names: [langfuse](../langfuse/SKILL.md)
