"""Contracts specialist — LangChain version vendored from llm-entity-extraction.

Re-exports ``langchain_agents.specialist_agents.ContractsSpecialist`` (the
eval-validated LangChain contracts specialist, production frozen-v1
prompt, per-field ``reasoning`` trace, chunked-extraction support,
``normalize_extraction`` field-presence guarantee and evidence-derived
confidence) with mailroom defaults applied from ``config/taxonomy.yaml``
(model, temperature, max_tokens, max_input_chars).

``handoff_context`` (the chained-eval pattern): when the graph passes the
sorter's classification — e.g. the contract subtype — it is prefixed to the
extraction call so the specialist extracts with the expected clause set of
that agreement family in mind.
"""

import structlog
from langchain_agents.prompts import get_prompt
from langchain_agents.specialist_agents import ContractsSpecialist as _LangChainContractsSpecialist
from llm.prompts import get_managed_prompt
from pipeline.config import get_agent_config

logger = structlog.get_logger(__name__)


class ContractsSpecialist(_LangChainContractsSpecialist):
    """Mailroom-configured contracts specialist.

    - Model/budget defaults come from ``taxonomy.yaml``
      ``agents.contracts_specialist`` (explicit ``model=``/``api_key=`` args
      still win).
    - Uses the sandbox / eval-environment **frozen v1** prompt by default
      (``contracts_specialist`` alias, sha256-locked in ``llm/frozen_v1``);
      override with ``prompt_version=`` for entity-extraction eval pins
      (``contracts_specialist_v1``…``v33``). Production extract nodes serve
      it through ``get_managed_prompt``.
    - ``handoff_context`` carries the sorter's classification (doc_type +
      contract subtype) into extraction, mirroring the sister repo's chained
      eval.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        prompt_version: str = "contracts_specialist",
        handoff_context: str | None = None,
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
        if handoff_context is not None:
            self.handoff_context = handoff_context

    def system_prompt(self) -> str:
        default = get_prompt(self.prompt_version)
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, default)
        return text
