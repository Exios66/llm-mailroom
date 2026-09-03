"""Intake agent — triage + clean + prepare for the full pipeline (HUB-038).

The intake clerk is the FIRST agent to see every document in the full
pipeline. Its deterministic core (whitespace / hyphen / NBSP normalize — the
dojo clerk gold) stays the mandatory baseline and is never skipped; on top of
it an LLM-assisted pass (``IntakeAgent``) adds, in ONE fused call per window:

- TRIAGE — an advisory first read (primary doc class, subclass, confidence,
  gist, keywords), identical in shape to the free triage team's read
  (``agents/gmail_triage.py`` — ``validate_triage`` is shared). It rides the
  terminal manifest's ``intake.triage``, the completion echo, and is fed to
  the sorter as a labeled prior — the sorter re-classifies independently and
  intake NEVER overrules it.
- CLEAN — a structural repair pass (OCR residue, run-together lines, repeated
  artifacts), gated to messy documents and bounded to the single-window case
  (a partial window must never replace the full text). The model's output is
  re-run through the deterministic clerk so ``prep_invariants`` hold no
  matter what the model returns; the dojo scores this as ``method: llm``
  against the clerk gold (``score_intake``).
- PREPARE — a section map (heading + role + char offsets, deterministically
  validated) that lets downstream consumers route by structure.

NO TRUNCATION DOCTRINE (human directive 2026-09-03): documents are never
truncated. Documents larger than an agent's input budget are processed in
overlapping SLIDING WINDOWS (paragraph-boundary, ``INTAKE_OVERLAP_FRACTION``
overlap) so every character of the document is seen — windows are merged
deterministically (triage votes, translated + deduped section offsets). This
mirrors the extraction pass's chunking guarantee: nothing is dropped, the
merge is the completeness guarantee.

Cost + efficiency mandate: the LLM pass fires ONLY for documents that need it
— ``looks_messy`` or longer than the sorter's input budget. Clean short
documents keep the all-deterministic path (zero added LLM calls). The model
is the cheapest paid tier (``qwen3.7-flash``); the free tier stays the Gmail
triage lane's privilege. ``MAILROOM_LLM_INTAKE=0`` disables the LLM pass
entirely (fully deterministic intake); failures always fail soft to the clerk
output.
"""

from __future__ import annotations

import json
import os

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
    "deterministic_normalize",
    "format_intake_prior",
    "intake_span_output",
    "llm_intake_enabled",
    "looks_messy",
    "should_llm_intake",
    "sliding_windows",
    "validate_intake",
]

#: Section roles in material-priority order.
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

#: Overlap fraction between sliding windows — a clause crossing a cut is seen
#: on both sides (mirrors the extraction chunker's overlap guarantee).
INTAKE_OVERLAP_FRACTION = 0.15

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
- When the text block you received is a partial window (a "[... truncated ...]" marker is present), set cleaned_text to null — you must never return a partial text.
- List what you changed in changes_applied (at most 10 short items); empty when unchanged.

PREPARE — build the section map for the downstream agents:
- sections: an array of objects with keys heading, role, start_offset, end_offset — one per major section of the document.
- start_offset/end_offset are CHARACTER offsets into the text block exactly as given to you (counting every character).
- Roles come from this catalog: recitals, parties, definitions, term, obligations, termination, governing_law, signatures, other. Use "other" when no catalog role fits.
- Order sections by start_offset. Include at most 40 sections.

Rules:
- Ground everything in the document text. Do not invent parties, dates, or amounts.
- You are advisory: the sorter re-classifies independently and never trusts your read over the document.
Respond with a single JSON object only."""


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
    budget (the triage prior + section map earn their cost there). Clean
    short documents keep the all-deterministic path — zero added calls.
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

    Set ``MAILROOM_LLM_INTAKE=0`` to disable (fully deterministic intake);
    failures always fail soft to the clerk output.
    """
    return str(os.environ.get("MAILROOM_LLM_INTAKE", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def sliding_windows(text: str, budget: int, overlap_chars: int) -> list[tuple[str, int]]:
    """Split ``text`` into overlapping windows, preserving EVERY character.

    Paragraph-aware (``\\n\\n`` paragraphs stay intact; a single pathological
    paragraph larger than the budget is hard-split on ``budget`` boundaries so
    no window ever exceeds it — nothing is truncated, the SAME text is
    re-sent). Every window after the first is prepended with the previous
    window's tail (``overlap_chars``, paragraph-aligned) so content crossing a
    cut is visible on both sides — the caller's merge dedupes.

    Returns ``[(window, base_offset)]`` where ``base_offset`` is the window's
    start position in ``text`` (the overlap prefix belongs to the previous
    window and is NOT counted toward ``base_offset``; it is re-sent for
    context only). Mirrors the extraction chunker's overlap guarantee.
    """
    if not text:
        return []
    if len(text) <= budget:
        return [(text, 0)]
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    bases: list[int] = []
    current: list[str] = []
    current_len = 0
    current_start = 0
    cursor = 0
    for para in paragraphs:
        para_start = text.find(para, cursor)
        if para_start == -1:
            para_start = cursor
        while len(para) > budget:  # pathological single paragraph
            if current:
                chunks.append("\n\n".join(current))
                bases.append(current_start)
                current, current_len = [], 0
            chunks.append(para[:budget])
            bases.append(para_start)
            para = para[budget:]
            para_start += budget
            cursor = para_start
        if current and current_len + len(para) + 2 > budget:
            chunks.append("\n\n".join(current))
            bases.append(current_start)
            current, current_len = [], 0
        if not current:
            current_start = para_start
        current.append(para)
        current_len += len(para) + 2
        cursor = para_start + len(para)
    if current:
        chunks.append("\n\n".join(current))
        bases.append(current_start)
    if len(chunks) > 1 and overlap_chars > 0:
        overlapped: list[tuple[str, int]] = [(chunks[0], bases[0])]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap_chars:]
            if "\n\n" in tail:
                tail = tail[tail.find("\n\n") + 2:]
            overlapped.append((f"{tail}\n\n{chunks[i]}", bases[i]))
        return overlapped
    return list(zip(chunks, bases))


def validate_intake(result: dict, text: str) -> dict:
    """Clamp one window's LLM intake answer to the live contracts.

    - ``triage`` passes through ``validate_triage`` (vocabulary-clamped).
    - ``cleaned_text`` / ``changes_applied`` are bounded strings.
    - ``sections`` are validated deterministically: integer in-bounds offsets
      (relative to the window ``text``), monotonic non-overlapping, catalog
      roles, at most 40 entries. Invalid sections are dropped — a bad section
      map must never poison downstream routing.
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


def _merge_triage(reads: list[dict]) -> dict:
    """Merge per-window triage reads: plurality vote among non-unknown
    classes; ties break on the highest window confidence; all-unknown falls
    back to the first window's read (document order)."""
    votes: dict[str, list[dict]] = {}
    for r in reads:
        cls = str(r.get("primary_doc_class") or "unknown")
        if cls == "unknown":
            continue
        votes.setdefault(cls, []).append(r)
    if not votes:
        return reads[0] if reads else {}
    best_class = max(
        votes,
        key=lambda c: (len(votes[c]), max(float(r.get("confidence") or 0.0) for r in votes[c])),
    )
    return max(votes[best_class], key=lambda r: float(r.get("confidence") or 0.0))


def _merge_sections(window_sections: list[tuple[int, dict]]) -> list[dict]:
    """Merge per-window section maps into document-absolute offsets.

    ``window_sections`` is ``[(base_offset, section)]``; offsets are
    translated by ``base_offset``, sorted, and overlap-deduped (the overlap
    re-sends the same clause on both sides). At most 40 sections survive.
    """
    translated = [
        {
            **s,
            "start_offset": int(s["start_offset"]) + base,
            "end_offset": int(s["end_offset"]) + base,
        }
        for base, s in window_sections
    ]
    translated.sort(key=lambda s: (s["start_offset"], s["end_offset"]))
    deduped: list[dict] = []
    for s in translated:
        if not deduped or s["start_offset"] >= deduped[-1]["end_offset"]:
            deduped.append(s)
            continue
        prev = deduped[-1]
        prev_size = prev["end_offset"] - prev["start_offset"]
        size = s["end_offset"] - s["start_offset"]
        if s["heading"] == prev["heading"] and size > prev_size:
            deduped[-1] = s
    return deduped[:40]


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


class IntakeAgent(BaseAgent):
    """LLM-assisted intake: TRIAGE + CLEAN + PREPARE, sliding-windowed.

    The deterministic clerk (``apply_intake``) is ALWAYS run first by the
    pipeline; this agent refines its output when the gate fires. The returned
    ``prep`` dict carries: ``triage`` (advisory read, merged across windows),
    ``cleaned`` / ``clean_stats`` (only when a structural repair was applied
    AND the whole document fit in a single window), ``sections`` (validated
    section map with document-absolute offsets), ``windows`` (window count),
    and ``changes_applied``. No text is ever truncated: over-budget documents
    slide through overlapping windows and the reads merge.
    """

    agent_name = "intake"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, INTAKE_SYSTEM_PROMPT)
        return text.replace("{classes}", ", ".join(get_all_doc_types()) + ", unknown")

    def _call_one(self, window: str, label: str) -> dict:
        user = f"File: {label}\n\nDocument text:\n{window}"
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
        return validate_intake(raw, window)

    def intake_run(self, doc_text: str, filename: str | None = None) -> dict:
        """One fused triage + clean + prepare pass over the FULL ``doc_text``.

        Fails soft by construction: any provider/parse failure yields an
        empty ``prep`` (no triage, no cleaning, no sections) and the caller
        keeps the deterministic clerk output — intake never blocks a run.
        """
        budget = self._configured_max_input_chars()
        overlap = max(1, int(budget * INTAKE_OVERLAP_FRACTION))
        windows = sliding_windows(doc_text or "", budget, overlap)
        if not windows:
            return {
                "triage": {},
                "cleaned_text": None,
                "changes_applied": [],
                "sections": [],
                "windows": 0,
                "window_chars": 0,
            }
        if len(windows) == 1:
            window, _base = windows[0]
            prep = self._call_one(window, filename or "unnamed")
            prep["windows"] = 1
            prep["window_chars"] = len(window)
            cleaned_text = prep.get("cleaned_text")
            if cleaned_text:
                cleaned, clean_stats = deterministic_normalize(cleaned_text)
                if cleaned and cleaned != doc_text:
                    prep["cleaned"] = cleaned
                    prep["clean_stats"] = clean_stats
                    prep["changed"] = True
            return prep
        reads: list[dict] = []
        window_sections: list[tuple[int, dict]] = []
        total = len(windows)
        for index, (window, base) in enumerate(windows, start=1):
            prep_w = self._call_one(
                window,
                f"{filename or 'unnamed'} [window {index} of {total}]",
            )
            reads.append(prep_w["triage"])
            for s in prep_w["sections"]:
                window_sections.append((base, s))
        return {
            "triage": _merge_triage(reads),
            "cleaned_text": None,  # never splice a partial window into the full text
            "changes_applied": [],
            "sections": _merge_sections(window_sections),
            "windows": total,
            "window_chars": len(doc_text or ""),
        }