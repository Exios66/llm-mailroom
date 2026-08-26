"""Sorter agent — LangChain version vendored from llm-entity-extraction.

Re-exports ``langchain_agents.sorter_agent.SorterAgent`` (the eval-validated
LangChain sorter with the contract-subtype dimension, ``sorter_v14`` prompt)
with mailroom defaults applied from ``config/taxonomy.yaml`` (model,
temperature, max_tokens, max_input_chars) and page-image vision support.

``classify`` returns the vendored 4-tuple
``(doc_type, contract_subtype, confidence, reasoning)`` — the graph unpacking
it must expect the subtype in position 2.
"""

import structlog
from langchain_agents.sorter_agent import SorterAgent as _LangChainSorterAgent
from pipeline.config import get_agent_config

logger = structlog.get_logger(__name__)


class SorterAgent(_LangChainSorterAgent):
    """Mailroom-configured sorter.

    - Model/budget defaults come from ``taxonomy.yaml`` ``agents.sorter``
      (explicit ``model=``/``api_key=`` args still win).
    - ``classify`` accepts page-image data-URIs; they are appended as
      multimodal content (additive, never replacing the text) when the
      configured model is vision-capable.
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
    ) -> dict:
        """Structured classify used by the graph (includes ``doc_subclass``)."""
        if pages:
            doc_text = (
                f"{doc_text}\n\n[Attached: {len(pages)} page image(s) of this "
                "document — also read them.]"
            )
        return super().classify_json(
            doc_text, subtype_focus=subtype_focus, pages=pages
        )
