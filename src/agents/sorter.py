"""Sorter agent — LangChain version vendored from llm-entity-extraction.

Re-exports ``langchain_agents.sorter_agent.SorterAgent`` (the eval-validated
LangChain sorter with the contract-subtype dimension, ``sorter_v14`` prompt)
with mailroom defaults applied from ``config/taxonomy.yaml`` (model,
temperature, max_tokens, max_input_chars) and page-image vision support.

``classify`` returns the vendored 4-tuple
``(doc_type, contract_subtype, confidence, reasoning)`` — the graph unpacking
it must expect the subtype in position 2.

NO-TRUNCATION DOCTRINE (HUB-038): documents past the input budget are NEVER
truncated — they are classified in overlapping SLIDING WINDOWS (every
character read, ``agents.intake.sliding_windows``) and the per-window reads
merge deterministically: plurality vote among non-unknown classes (ties break
on window confidence), mean confidence of the agreeing windows, first
non-null subtype/subclass, joined reasoning. ``classify_json`` accepts an
advisory ``intake_prior`` block and a per-window ``prefix`` (retry preamble);
both are prepended to every window so no window classifies blind.
"""

import structlog
from langchain_agents.sorter_agent import SorterAgent as _LangChainSorterAgent
from pipeline.config import get_agent_config

logger = structlog.get_logger(__name__)

#: Overlap fraction between sorter windows — a clause crossing a cut is seen
#: on both sides (mirrors the extraction chunker + intake overlap guarantee).
SORTER_OVERLAP_FRACTION = 0.15

#: Reserve of the input budget for the prior/prefix block per window so the
#: composed message never exceeds the vendored cap (which would re-truncate).
_PRIOR_ALLOWANCE = 600


class SorterAgent(_LangChainSorterAgent):
    """Mailroom-configured sorter.

    - Model/budget defaults come from ``taxonomy.yaml`` ``agents.sorter``
      (explicit ``model=``/``api_key=`` args still win).
    - ``classify_json`` accepts page-image data-URIs; they are appended as
      multimodal content (additive, never replacing the text) when the
      configured model is vision-capable — attached to the FIRST window only.
    - Keeps the vendored ``sorter_v14`` prompt by default (V12 lineage +
      mailroom pipeline doctrine); override with ``prompt_version=``.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        prompt_version: str = "sorter_v14",
    ):
        super().__init__(model=model, api_key=api_key, prompt_version=prompt_version)
        cfg = get_agent_config(self.agent_name)
        if model is None:
            self.model = cfg.get("model", self.model)
        self._max_tokens = int(cfg.get("max_tokens", self._max_tokens))
        self._max_input_chars = int(cfg.get("max_input_chars", self._max_input_chars))
        self._temperature = float(cfg.get("temperature", self._temperature))
        if cfg.get("reasoning_effort"):
            self._reasoning_effort = cfg["reasoning_effort"]

    def classify(self, doc_text: str, pages: list[str] | None = None):
        """Classify a document, optionally with page images attached.

        Returns ``(doc_type, contract_subtype, confidence, reasoning)``.
        """
        result = self.classify_json(doc_text, pages=pages)
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return (
            result.get("doc_type") or "",
            result.get("contract_subtype"),
            confidence,
            result.get("reasoning") or "",
        )

    def classify_json(
        self,
        doc_text: str,
        subtype_focus: bool = False,
        pages: list[str] | None = None,
        intake_prior: str | None = None,
        prefix: str | None = None,
    ) -> dict:
        """Structured classify used by the graph (includes ``doc_subclass``).

        Sliding-windowed past the input budget — never truncates. ``pages``
        attach to the first window only (additive vision at bounded cost).
        ``intake_prior`` (advisory intake read) and ``prefix`` (e.g. the
        retry preamble) are prepended to EVERY window.
        """
        if pages:
            doc_text = (
                f"{doc_text}\n\n[Attached: {len(pages)} page image(s) of this "
                "document — also read them.]"
            )
        from agents.intake import sliding_windows

        prior = "\n\n".join(p for p in (prefix, intake_prior) if p)
        allowance = len(prior) + _PRIOR_ALLOWANCE if prior else 0
        effective = max(1024, self._max_input_chars - allowance)
        if len(doc_text) <= effective:
            composed = f"{prior}\n\n{doc_text}" if prior else doc_text
            result = super().classify_json(
                composed, subtype_focus=subtype_focus, pages=pages
            )
            self._last_windows = 1
            return result

        overlap = max(1, int(effective * SORTER_OVERLAP_FRACTION))
        windows = sliding_windows(doc_text, effective, overlap)
        results = []
        for index, (window, _base) in enumerate(windows, start=1):
            header = (
                f"WINDOW {index} OF {len(windows)} — this is one window of the "
                "document; the sorter reads EVERY window, so classify this "
                "window independently from what it shows."
            )
            composed = "\n\n".join(p for p in (prior, header, window) if p)
            results.append(
                super().classify_json(
                    composed, subtype_focus=subtype_focus, pages=pages if index == 1 else None
                )
            )
        merged = _merge_sorter_reads(results)
        self._last_windows = len(results)
        return merged


def _merge_sorter_reads(results: list[dict]) -> dict:
    """Merge per-window classification reads (deterministic, no truncation).

    - doc_type: plurality vote among non-unknown classes; ties break on the
      highest window confidence; all-unknown falls back to the first window.
    - confidence: MEAN of the agreeing windows' confidences (each window's
      read is real evidence; a lone outlier does not drag the class down).
    - contract_subtype / doc_subclass: first non-null among agreeing windows.
    - reasoning: per-window trace joined with window markers, bounded.
    """
    if len(results) == 1:
        return results[0]
    votes: dict[str, list[dict]] = {}
    for r in results:
        cls = str(r.get("doc_type") or "unknown")
        if cls == "unknown":
            continue
        votes.setdefault(cls, []).append(r)
    if not votes:
        return dict(results[0])
    best_class = max(
        votes,
        key=lambda c: (
            len(votes[c]),
            max(float(r.get("confidence") or 0.0) for r in votes[c]),
        ),
    )
    winners = votes[best_class]
    confidence = sum(float(r.get("confidence") or 0.0) for r in winners) / len(winners)
    merged = dict(winners[0])
    merged["doc_type"] = best_class
    merged["confidence"] = round(confidence, 3)
    merged["contract_subtype"] = next(
        (r.get("contract_subtype") for r in winners if r.get("contract_subtype")), None
    )
    merged["doc_subclass"] = next(
        (r.get("doc_subclass") for r in winners if r.get("doc_subclass")), None
    )
    merged["reasoning"] = " | ".join(
        f"window[{i}]: {str(r.get('reasoning') or '').strip()}"
        for i, r in enumerate(results, start=1)
    )[:600]
    return merged