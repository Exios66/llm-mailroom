"""Intake agent — triage + clean + prepare for the full pipeline (HUB-038).

The intake clerk is the FIRST agent to see every document in the full
pipeline. Its deterministic core (whitespace / hyphen / NBSP normalize — the
dojo clerk gold) stays the mandatory baseline and is never skipped; on top of
it an LLM-assisted pass (``IntakeAgent``) adds, in ONE fused call:

- TRIAGE — an advisory first read (primary doc class, subclass, confidence,
  gist, keywords), identical in shape to the free triage team's read
  (``agents/gmail_triage.py`` — ``validate_triage`` is shared). It rides the
  terminal manifest's ``intake.triage``, the completion echo, and is fed to
  the sorter as a labeled prior — the sorter re-classifies independently and
  intake NEVER overrules it.
- CLEAN — a structural repair pass (OCR residue, run-together lines, repeated
  artifacts), gated to messy documents and bounded to the non-windowed case.
  The model's output is re-run through the deterministic clerk so
  ``prep_invariants`` hold no matter what the model returns; the dojo scores
  this as ``method: llm`` against the clerk gold (``score_intake``).
- PREPARE — a section map (heading + role + char offsets, deterministically
  validated) that replaces the sorter's blind HEAD+TAIL truncation with a
  material-window selection (governing-law / term / termination / signatures
  survive) for documents over the sorter's input budget.

Cost + efficiency mandate (human directive 2026-09-03): the LLM call fires
ONLY for documents that need it — ``looks_messy`` or longer than the sorter's
input budget. Clean short documents keep the all-deterministic path (zero
added LLM calls); one fused call per document, never more. The model is the
cheapest paid tier (``qwen3.7-flash``); the free tier stays the Gmail triage
lane's privilege. ``MAILROOM_LLM_INTAKE=0`` disables the LLM pass entirely
(fully deterministic intake); failures always fail soft to the clerk output.
"""

from __future__ import annotations

import json

from agents.base import BaseAgent
from agents.gmail_triage import validate_triage
from llm.prompts import get_managed_prompt
from pipeline.config import get_all_doc_types
from llm_dojo_scoring.intake import (
    INTAKE_LIVE_METHOD,
    INTAKE_PREP_STEPS,
    INTAKE_SPAN,
    INTAKE_SPAN_KEYS,
    deterministic_normalize,
    intake_span_output,
    looks_messy,
)

__all__ = [
    "INTAKE_LIVE_METHOD",
    "INTAKE_PREP_STEPS",
    "INTAKE_SPAN",
    "INTAKE_SPAN_KEYS",
    "INTAKE_SCHEMA",
    "INTAKE_SECTION_ROLES",
    "IntakeAgent",
    "apply_intake",
    "build_sorter_input",
    "deterministic_normalize",
    "format_intake_prior",
    "intake_span_output",
    "llm_intake_enabled",
    "looks_messy",
    "should_llm_intake",
    "validate_intake",
]

#: Section roles in material-priority order — the order ``build_sorter_input``
#: selects sections when a document exceeds the sorter's input budget
#: (governing-law / term / termination / signatures first — the deal-critical
#: closing portions a blind HEAD+TAIL window can still lose).
INTAKE_SECTION_ROLES: tuple[str, ...] = (
    "governing_law",
    "term",
    "termination",
    "signatures",
    "obligations",
    "parties",
    "definitions",
    "recitals",
    "other",
)

INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "triage": {
            "type": "object",
            "properties": {
                "primary_doc_class": {"type": "string"},
                "doc_subclass": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "gist": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["primary_doc_class", "confidence", "gist"],
        },
        "cleaned_text": {"type": ["string", "null"]},
        "changes_applied": {"type": "array", "items": {"type": "string"}},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "role": {"type": "string"},
                    "start_offset": {"type": "integer"},
                    "end_offset": {"type": "integer"},
                },
                "required": ["heading", "role", "start_offset", "end_offset"],
            },
        },
    },
    "required": ["triage", "sections"],
}

INTAKE_SYSTEM_PROMPT = """You are the intake clerk of a legal mailroom — the first agent to see every document in the full pipeline. In ONE pass you TRIAGE, CLEAN, and PREPARE the document for the classification and extraction agents that follow.

TRIAGE — a fast, grounded first read. Classify the document into ONE of these primary classes:
{classes}
Use "unknown" when the document does not clearly fit any of them — never guess.
If the class has a well-known subtype catalog and the document clearly matches one, set doc_subclass to that token; otherwise null.
- confidence is 0.0-1.0 and must reflect how certain the primary class is.
- gist: ONE grounded sentence saying what this document actually is.
- keywords: at most 6 distinctive terms from the document.

CLEAN — only when the text is messy (OCR residue, run-together lines, repeated header/footer artifacts, garbled encoding):
- Return the FULL repaired text in cleaned_text. Repairs are structural only: join run-together lines, drop repeated artifacts, fix obvious OCR mangling. NEVER add, remove, or alter facts, numbers, names, dates, or amounts.
- When the document is clean or you made no changes, set cleaned_text to null.
- When the document is truncated (a "[... truncated ...]" marker is present), set cleaned_text to null — you must never return a partial text.
- List what you changed in changes_applied (at most 10 short items); empty when unchanged.

PREPARE — build the section map for the downstream agents:
- sections: an array of {{heading, role, start_offset, end_offset}} for the document's major sections.
- start_offset/end_offset are CHARACTER offsets into the document text block exactly as given to you (counting every character, including any "[... truncated ...]" marker).
- Roles come from this catalog: recitals, parties, definitions, term, obligations, termination, governing_law, signatures, other. Use "other" when no catalog role fits.
- Order sections by start_offset. Include at most 40 sections.

Rules:
- Ground everything in the document text. Do not invent parties, dates, or amounts.
- You are advisory: the sorter re-classifies independently and never trusts your read over the document.
Respond with a single JSON object only."""

#: Share of the intake input budget kept from the document's TAIL when the
#: intake window fires (mirrors the vendored sorter's 60/40 HEAD+TAIL).
INTAKE_TAIL_FRACTION = 0.4


def _intake_system_prompt() -> str:
    classes = ", ".join(get_all_doc_types()) + ", unknown"
    return INTAKE_SYSTEM_PROMPT.replace("{classes}", classes)


def apply_intake(text: str, *, filename: str | None = None) -> tuple[str, dict]:
    """Normalize ``text`` and emit the ``normalize-intake`` span.

    Returns ``(cleaned_text, stats)`` where ``stats`` includes the span
    payload keys (``messy``, ``method``, ``chars``) plus the raw normalize
    counters. Tracing no-ops when Langfuse is not the active backend.
    """
    from observability.tracing import observation

    cleaned, stats = deterministic_normalize(text)
    messy = looks_messy(cleaned, stats)
    payload = intake_span_output(stats, messy, method=INTAKE_LIVE_METHOD)
    with observation(
        INTAKE_SPAN,
        as_type="span",
        input={"file": filename, "raw_chars": stats.get("raw_chars", 0)},
    ) as span:
        if span is not None:
            span.update(output=payload)
    return cleaned, {**stats, **payload}


def should_llm_intake(text: str, stats: dict | None = None) -> bool:
    """Whether this document needs the LLM intake pass (the gate).

    The LLM call fires ONLY for documents that need it: the deterministic
    clerk flags the text as messy, or the text exceeds the sorter's input
    budget (truncation risk — the section map + prior earn their cost there).
    Clean short documents keep the all-deterministic path — zero added calls.
    """
    if not text or not text.strip():
        return False
    if stats and stats.get("messy"):
        return True
    try:
        from pipeline.config import get_agent_config

        budget = int(get_agent_config("sorter").get("max_input_chars", 12000))
    except Exception:
        budget = 12000
    return len(text) > budget


def llm_intake_enabled() -> bool:
    """Whether the LLM intake pass is on (default: on).

    Set ``MAILROOM_LLM_INTAKE=0`` to disable (fully deterministic intake —
    the HUB-037-era behavior); failures always fail soft to the clerk output.
    """
    return str(os.environ.get("MAILROOM_LLM_INTAKE", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _intake_window(text: str, budget: int) -> tuple[str, bool]:
    """Bound the intake input past the agent's budget: HEAD + TAIL window.

    Returns ``(window, windowed)``. Offsets the model reports are relative to
    the WINDOW (including the truncation marker), so section selection and
    the sorter material window stay consistent.
    """
    if len(text) <= budget:
        return text, False
    head = int(budget * (1.0 - INTAKE_TAIL_FRACTION))
    return (
        text[:head]
        + f"\n\n[... document truncated, {len(text)} total chars; middle omitted ...]\n\n"
        + text[-(budget - head):],
        True,
    )


def validate_intake(result: dict, text: str) -> dict:
    """Clamp the LLM intake answer to the live contracts.

    - ``triage`` passes through ``validate_triage`` (vocabulary-clamped).
    - ``cleaned_text`` / ``changes_applied`` are bounded strings.
    - ``sections`` are validated deterministically: integer in-bounds offsets
      (relative to ``text``), monotonic non-overlapping, catalog roles, at
      most 40 entries. Invalid sections are dropped — a bad section map must
      never poison the sorter window (``build_sorter_input`` falls back to
      the blind HEAD+TAIL window when no sections survive).
    """
    text = text or ""
    raw_triage = result.get("triage")
    triage = validate_triage(raw_triage if isinstance(raw_triage, dict) else {})

    raw_clean = result.get("cleaned_text")
    cleaned_text = None
    if isinstance(raw_clean, str) and raw_clean.strip():
        cleaned_text = raw_clean[: max(len(text) * 3 + 2000, 2000)]

    raw_changes = result.get("changes_applied")
    if not isinstance(raw_changes, list):
        raw_changes = []
    changes = [str(c).strip()[:120] for c in raw_changes if str(c).strip()][:10]

    sections: list[dict] = []
    raw_sections = result.get("sections")
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if not isinstance(s, dict):
                continue
            try:
                start = int(s.get("start_offset"))
                end = int(s.get("end_offset"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start or end > len(text):
                continue
            heading = str(s.get("heading") or "").strip()[:120]
            role = str(s.get("role") or "other").strip().lower()
            if role not in INTAKE_SECTION_ROLES:
                role = "other"
            sections.append(
                {
                    "heading": heading,
                    "role": role,
                    "start_offset": start,
                    "end_offset": end,
                }
            )
    sections.sort(key=lambda s: s["start_offset"])
    deduped: list[dict] = []
    last_end = -1
    for s in sections:
        if s["start_offset"] < last_end:
            continue
        deduped.append(s)
        last_end = s["end_offset"]
    sections = deduped[:40]

    return {
        "triage": triage,
        "cleaned_text": cleaned_text,
        "changes_applied": changes,
        "sections": sections,
    }


def format_intake_prior(prep: dict | None) -> str:
    """Render the advisory intake read as the sorter's prior block.

    Labeled advisory — the sorter verifies independently (chained-eval
    pattern; the vendored ``sorter_v14`` prompt is never mutated).
    """
    triage = (prep or {}).get("triage")
    if not triage:
        return ""
    lines = [
        "[intake prior — advisory read by the intake clerk; verify it independently]",
        f"primary class: {triage.get('primary_doc_class') or 'unknown'}",
    ]
    if triage.get("doc_subclass"):
        lines.append(f"subclass: {triage['doc_subclass']}")
    confidence = triage.get("confidence")
    if confidence is not None:
        lines.append(f"confidence: {confidence}")
    gist = str(triage.get("gist") or "").strip()
    if gist:
        lines.append(f"gist: {gist}")
    keywords = triage.get("keywords") or []
    if keywords:
        lines.append(f"keywords: {', '.join(str(k) for k in keywords)}")
    return "\n".join(lines)


def build_sorter_input(doc_text: str, prep: dict | None, budget: int) -> tuple[str, bool]:
    """Compose the sorter's document input, bounded to ``budget``.

    - Within budget: the full text, untouched (``truncated=False``).
    - Over budget with a valid section map: MATERIAL-WINDOW selection — role
      priority order (governing-law / term / termination / signatures first),
      each section kept whole when it fits, sliced when it does not, plus a
      small safety tail of the closing text. Beats the blind HEAD+TAIL for
      deal-critical closing clauses.
    - Over budget without sections (no LLM pass, or the map failed
      validation): the blind HEAD+TAIL window — today's behavior.

    Returns ``(text, truncated)``.
    """
    if len(doc_text) <= budget:
        return doc_text, False
    sections = (prep or {}).get("sections") or []
    if sections:
        cap = int(budget * 0.85)
        selected: list[dict] = []
        used = 0
        for role in INTAKE_SECTION_ROLES:
            for s in [x for x in sections if x["role"] == role]:
                size = s["end_offset"] - s["start_offset"]
                if used + size > cap:
                    size = cap - used
                    if size <= 0:
                        continue
                    s = {**s, "end_offset": s["start_offset"] + size}
                selected.append(s)
                used += s["end_offset"] - s["start_offset"]
                if used >= cap:
                    break
            if used >= cap:
                break
        tail_keep = int(budget * 0.08)
        if tail_keep > 0 and used + tail_keep <= budget:
            tail_start = len(doc_text) - tail_keep
            if not any(s["end_offset"] >= tail_start for s in selected):
                selected.append(
                    {
                        "heading": "(closing portion)",
                        "role": "other",
                        "start_offset": tail_start,
                        "end_offset": len(doc_text),
                    }
                )
        parts = [doc_text[s["start_offset"]:s["end_offset"]] for s in sorted(selected, key=lambda s: s["start_offset"])]
        body = "\n\n".join(parts)
        if len(body) <= budget:
            return body, True
    head = int(budget * 0.6)
    return (
        doc_text[:head]
        + f"\n\n[... document truncated, {len(doc_text)} total chars; middle omitted — "
          "closing portion continues below (term, termination, governing law) ...]\n\n"
        + doc_text[-(budget - head):],
        True,
    )


class IntakeAgent(BaseAgent):
    """LLM-assisted intake: TRIAGE + CLEAN + PREPARE in one fused call.

    The deterministic clerk (``apply_intake``) is ALWAYS run first by the
    pipeline; this agent refines its output when the gate fires. The returned
    ``prep`` dict carries: ``triage`` (advisory read), ``cleaned`` /
    ``clean_stats`` (only when a structural repair was applied and the input
    was not windowed), ``sections`` (validated section map, offsets relative
    to the window), ``windowed`` / ``window_chars``, and ``changes_applied``.
    """

    agent_name = "intake"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, INTAKE_SYSTEM_PROMPT)
        return text.replace("{classes}", ", ".join(get_all_doc_types()) + ", unknown")

    def intake_run(self, doc_text: str, filename: str | None = None) -> dict:
        """One fused triage + clean + prepare call over ``doc_text``.

        Fails soft by construction: any provider/parse failure yields an
        empty ``prep`` (no triage, no cleaning, no sections) and the caller
        keeps the deterministic clerk output — intake never blocks a run.
        """
        window, windowed = _intake_window(doc_text, self._configured_max_input_chars())
        user = f"File: {filename or 'unnamed'}\n\nDocument text:\n{window}"
        raw = self._call_structured(
            user,
            INTAKE_SCHEMA,
            system_prompt=self.system_prompt(),
        )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        prep = validate_intake(raw, window)
        prep["windowed"] = windowed
        prep["window_chars"] = len(window)
        if not windowed:
            cleaned_text = prep.get("cleaned_text")
            if cleaned_text:
                cleaned, clean_stats = deterministic_normalize(cleaned_text)
                if cleaned and cleaned != doc_text:
                    prep["cleaned"] = cleaned
                    prep["clean_stats"] = clean_stats
                    prep["changed"] = True
        else:
            # Never splice a window back into the document — partial text must
            # not replace the full deterministic output.
            prep.pop("cleaned_text", None)
        return prep