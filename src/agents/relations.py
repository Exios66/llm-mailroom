"""Relations agent — LLM judgment pass over relation candidates (HUB-040).

The OPTIONAL second layer of the relations clerk: a closed-vocabulary LLM
judgment over the deterministic scanner's top candidates (pairs whose signals
are suggestive but sub-threshold — the ambiguous band, mirroring the
judge-verify philosophy). Code-COMPLETE but DISABLED in the pilot:
``relations.llm: false`` (taxonomy) keeps the layer deterministic-only until
production, when a paid model activates (with the MAILROOM_LLM_FREE_ONLY
guardrail unset). Every judgment is validated and clamped to the live
vocabulary exactly like ``validate_triage`` — nothing unvalidated ever
reaches the ledger (audit-integrity law).
"""

from __future__ import annotations

import json

from agents.base import BaseAgent
from llm.prompts import get_managed_prompt
from storage.relations import RELATION_TYPES

RELATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_doc_id": {"type": "string"},
                    "target_doc_id": {"type": "string"},
                    "relation_type": {"type": "string", "enum": list(RELATION_TYPES)},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["source_doc_id", "target_doc_id", "relation_type", "confidence"],
            },
        }
    },
    "required": ["judgments"],
}

RELATIONS_SYSTEM_PROMPT = """You are the relations analyst of a legal mailroom. You review CANDIDATE relations between archived documents (proposed by deterministic signals: shared keywords, shared parties, embedding similarity, same matter) and judge which are meaningful, lawyer-grade associations.

For each candidate, decide:
- relation_type: one of {types}.
- confidence: 0.0-1.0 — how confident are you this is a MEANINGFUL association for legal research (not mere coincidence)?
- rationale: ONE grounded sentence citing the evidence.

Rules:
- Ground everything in the provided evidence. Never invent parties, dates, or document contents.
- Reject (confidence < 0.3) candidates that look coincidental — coincidence is worse than silence in a matter file.
- relation_type MUST be from the list — never invent types.
Respond with a single JSON object of shape: {"judgments": [{"source_doc_id": "...", "target_doc_id": "...", "relation_type": "...", "confidence": 0.0, "rationale": "..."}]}"""


def _relations_system_prompt() -> str:
    return RELATIONS_SYSTEM_PROMPT.replace("{types}", ", ".join(RELATION_TYPES))


def validate_judgments(raw: dict, allowed_pairs: set[tuple[str, str]]) -> list[dict]:
    """Clamp the model's answer: closed type vocabulary, 0.0-1.0 confidence,
    bounded rationale, and ONLY pairs the deterministic layer actually
    proposed (the LLM can never mint a relation the scanner didn't)."""
    judgments = raw.get("judgments") if isinstance(raw, dict) else None
    if not isinstance(judgments, list):
        return []
    out = []
    for item in judgments[:50]:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source_doc_id") or "").strip()
        dst = str(item.get("target_doc_id") or "").strip()
        if not src or not dst or src == dst:
            continue
        pair = (src, dst) if src <= dst else (dst, src)
        if pair not in allowed_pairs:
            continue
        rtype = str(item.get("relation_type") or "").strip().lower()
        if rtype not in RELATION_TYPES:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        confidence = min(1.0, max(0.0, confidence))
        out.append(
            {
                "source_doc_id": pair[0],
                "target_doc_id": pair[1],
                "relation_type": rtype,
                "confidence": round(confidence, 3),
                "rationale": str(item.get("rationale") or "").strip()[:300],
            }
        )
    return out


class RelationsAgent(BaseAgent):
    agent_name = "relations"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt("relations", RELATIONS_SYSTEM_PROMPT)
        return text.replace("{types}", ", ".join(RELATION_TYPES))

    def judge(self, candidates: list[dict]) -> list[dict]:
        """Judge candidate pairs. Input: deterministic candidates ( dicts with
        source_doc_id/target_doc_id + signal evidence). Returns validated
        judgments only — invalid, out-of-vocabulary, or unproposed pairs are
        dropped, never stored."""
        if not candidates:
            return []
        allowed_pairs = set()
        lines = []
        for c in candidates[:25]:
            src, dst = sorted([str(c.get("source_doc_id")), str(c.get("target_doc_id"))])
            allowed_pairs.add((src, dst))
            signals = c.get("signals") or []
            signal_lines = "; ".join(
                f"{s.get('relation_type')}={s.get('score')}" for s in signals[:4]
            )
            evidence = c.get("evidence") or {}
            lines.append(
                f"pair {src} <-> {dst} [{signal_lines}] evidence: {json.dumps(evidence, default=str)[:400]}"
            )
        user = "CANDIDATE RELATIONS:\n" + "\n".join(lines)
        raw = self._call_structured(
            user,
            RELATIONS_SCHEMA,
            system_prompt=self.system_prompt(),
        )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return validate_judgments(raw if isinstance(raw, dict) else {}, allowed_pairs)
