"""Per-agent OUTCOME MEMORY for the vendored LangChain agents.

Every designated agent gets a persistent ledger of its own outcomes under
``data/memory/<agent_name>.jsonl`` (gitignored runtime state, like the rest of
``data/``). The ledger records what the agent decided, what the deterministic
guardrails/judges later said about that decision, and feeds the recent
learnings back into retry calls so the agent improves iteratively without
touching the eval-validated prompt text.

Design:

- ``record_outcome(...)`` — append one outcome row (agent, doc_type,
  decision, confidence, feedback, source, ts). Callers include the guardrail
  paths in ``graph/build_graph.py`` (classification/extraction guardrail
  triggers) and the judge-pilot loop.
- ``recent_context(agent, doc_type, k=5)`` — the last ``k`` outcomes for this
  agent+doc_type rendered as a prompt appendix ("what similar cases taught
  us"), bounded in size. Injected into RETRY prompts only, so the
  eval-validated first-attempt prompt stays byte-stable (issue #10).
- ``stats(agent)`` — counts by source/feedback for observability.

The memory file is append-only JSONL; corruption in a line never breaks the
agent (skipped with a warning). ``MAILROOM_MEMORY_DIR`` overrides the location
(default ``<MAILROOM_BASE_DIR>/memory``).
"""

from __future__ import annotations

import json
import os
import structlog
import time
from pathlib import Path

logger = structlog.get_logger(__name__)

_MAX_CONTEXT_CHARS = 3000


def _memory_dir() -> Path:
    override = os.environ.get("MAILROOM_MEMORY_DIR")
    if override:
        return Path(override)
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()
    return base / "memory"


def _memory_path(agent_name: str) -> Path:
    path = _memory_dir() / f"{agent_name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record_outcome(
    agent_name: str,
    *,
    doc_type: str,
    decision: str,
    confidence: float | None,
    feedback: str,
    source: str = "guardrail",
    detail: dict | None = None,
) -> bool:
    """Append one outcome row to the agent's ledger (best-effort)."""
    try:
        row = {
            "ts": time.time(),
            "agent": agent_name,
            "doc_type": doc_type,
            "decision": decision,
            "confidence": confidence,
            "feedback": feedback[:500],
            "source": source,
            "detail": detail or {},
        }
        with _memory_path(agent_name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception:
        logger.debug("memory_write_failed", agent=agent_name)
        return False


def recent_context(agent_name: str, doc_type: str, k: int = 5) -> str:
    """Render the last ``k`` outcomes for this agent+doc_type as a prompt
    appendix — the "what similar cases taught us" inner context used on
    retries for iterative improvement. Returns "" when empty."""
    path = _memory_path(agent_name)
    if not path.exists():
        return ""
    rows: list[dict] = []
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("doc_type") == doc_type:
                rows.append(row)
            if len(rows) >= k:
                break
    except OSError:
        return ""
    if not rows:
        return ""

    lines = ["## Memory: what similar cases taught us"]
    used = 0
    for row in rows:
        entry = (
            f"- ({row.get('source', 'memory')}) decision={row.get('decision')} "
            f"confidence={row.get('confidence')} — {row.get('feedback', '')}"
        )
        if used + len(entry) > _MAX_CONTEXT_CHARS:
            break
        lines.append(entry)
        used += len(entry)
    return "\n" + "\n".join(lines)


def stats(agent_name: str) -> dict:
    """Count outcomes by source and by feedback keyword (for observability)."""
    path = _memory_path(agent_name)
    counts = {"total": 0, "by_source": {}}
    if not path.exists():
        return counts
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts["total"] += 1
            src = row.get("source", "unknown")
            counts["by_source"][src] = counts["by_source"].get(src, 0) + 1
    except OSError:
        pass
    return counts
