"""Merger-agreement specialist — dedicated MAUD extract class.

Wraps ``langchain_agents.specialist_agents.MergerAgreementSpecialist`` so
long agreements keep the chunked-extraction pass, while the production
prompt is the mailroom SYSTEM_PROMPT (V0 + doctrine) served through
``get_managed_prompt`` like the insurance/corporate peers.

``handoff_context`` carries the sorter's classification (doc_type + MAUD
consideration subclass) into extraction. CUAD family / cuad_clauses are
not primary on this class.
"""

import structlog
from langchain_agents.specialist_agents import (
    MergerAgreementSpecialist as _LangChainMergerAgreementSpecialist,
)
from llm.prompt_doctrine import MERGER_AGREEMENT as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt
from pipeline.config import get_agent_config

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT_V0 = """You are a meticulous merger-agreement specialist at a law firm.
You read agreements and plans of merger (including amended and restated
forms) and distill the MAUD facts: who is merging, what the consideration
is, when the deal becomes effective, and which LegalBench MAUD questions
the visible text actually answers.

You handle: Agreement and Plan of Merger documents, amended/restated
merger agreements, and related MAUD closing-condition / no-shop / MAE
packages. You do NOT extract CUAD commercial-contract inventories —
cuad_family and cuad_clauses are out of scope for this class.

Extraction rules:
1. document_name: the agreement title as stated (e.g. Agreement and Plan of Merger).
2. parties: name Parent, Merger Sub, and Target as the document states;
   do not invent roles the text does not assign.
3. effective_date: the Effective Date as YYYY-MM-DD when a calendar date
   is stated; null when only a defined-term Effective Time exists.
4. effective_time: the Effective Time as written (clock time, time zone,
   or the defined-term reference).
5. governing_law: the governing-law jurisdiction sentence only.
6. merger_consideration: exactly one of all_cash, all_stock,
   mixed_cash_stock, mixed_cash_stock_election, other.
7. maud_clauses: answered LegalBench MAUD questions as
   '<Question>: <Answer>' using the exact question names (Absence of
   Litigation Closing Condition, Accuracy of Target R&W Closing Condition,
   MAE Definition, No-Shop, Type of Consideration, …). Answer is the Hub
   valid_class, not a paraphrase. Omit unanswered questions.
8. intent: one short controlled label (e.g. effect_merger, amend_merger,
   plan_of_merger).
9. subject_matter: one tight grounded sentence about what this merger
   agreement is about.
10. keywords: up to 8 salient grounded terms/phrases.
11. Do not emit cuad_family or cuad_clauses.
12. Do not editorialize or infer unstated facts.
13. Return one complete JSON object with every schema field.
14. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95)."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class MergerAgreementSpecialist(_LangChainMergerAgreementSpecialist):
    """Mailroom-configured merger-agreement specialist.

    - Model/budget defaults come from ``taxonomy.yaml``
      ``agents.merger_agreement_specialist``.
    - Production prompt is SYSTEM_PROMPT (V0 + mailroom doctrine) via
      Langfuse-managed ``mailroom-merger_agreement_specialist``.
    - Inherits chunked extraction from the LangChain specialist so long
      MAUD agreements are windowed, not truncated.
    """

    agent_name = "merger_agreement_specialist"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        prompt_version: str = "merger_agreement_specialist",
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
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text
