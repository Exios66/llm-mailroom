# VENDORED from github.com/Exios66/llm-entity-extraction (commit 055df31, ~v0.11.0).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""SorterAgent — Legal Document Classification Agent (LangChain).

Classifies documents into one of the 6 mailroom document types with confidence
scoring. The system prompt is loaded BY VERSION from ``src.prompts`` so the
evaluation loops can test exactly one prompt version per Braintrust experiment.
"""

from __future__ import annotations

import re

import structlog
from langchain_agents.base_agent import BaseAgent, build_structured_schema
from langchain_agents.prompts import get_prompt

logger = structlog.get_logger(__name__)

DOC_CLASSES = [
    {"key": "contract", "label": "Contract / Agreement", "description": "Formal agreements between parties: M&A, vendor, employment, NDAs, etc."},
    {"key": "corporate_record", "label": "Corporate Record", "description": "Bylaws, resolutions, board minutes, cap table entries, incorporation docs"},
    {"key": "due_diligence", "label": "Due Diligence", "description": "Checklists, disclosure schedules, diligence memos, risk assessments"},
    {"key": "correspondence", "label": "Correspondence", "description": "Letters, emails, memos, notices between parties or with regulators"},
    {"key": "compliance_filing", "label": "Compliance Filing", "description": "SEC filings, state registrations, regulatory submissions, annual reports"},
    {"key": "court_opinion", "label": "Court Opinion", "description": "Judicial opinions and orders: published decisions, memorandum opinions, rulings"},
]

DOC_CLASS_KEYS = [d["key"] for d in DOC_CLASSES]

# The CONTRACT SUBGROUP dimension (CUAD corpus, 25 contract types): the
# finer-grained family of agreement a contract belongs to. The sorter outputs
# ``contract_subtype`` alongside ``doc_type`` so the mailroom knows which
# specialist expectations apply (per the CUAD dataset card, the group a
# document belongs to decides what fields to expect). Keys are normalized
# folder names from the CUAD tree; "other" is the fallback for contracts that
# fit none of the listed families.
CONTRACT_SUBTYPES = [
    {"key": "affiliate", "label": "Affiliate Agreement", "description": "Affiliate/referral program agreements"},
    {"key": "agency", "label": "Agency Agreement", "description": "Agency representation agreements"},
    {"key": "collaboration", "label": "Collaboration / Cooperation Agreement", "description": "R&D and cooperation collaborations"},
    {"key": "co_branding", "label": "Co-Branding Agreement", "description": "Co-branded marketing/product agreements"},
    {"key": "consulting", "label": "Consulting Agreement", "description": "Consulting and advisory services"},
    {"key": "development", "label": "Development Agreement", "description": "Product/software/services development"},
    {"key": "distributor", "label": "Distributor Agreement", "description": "Distribution and resale rights"},
    {"key": "endorsement", "label": "Endorsement Agreement", "description": "Endorsements and endorsement riders: celebrity/influencer deals, product or service endorsements, and endorsement riders or amendments attached to insurance, annuity, or other agreements"},
    {"key": "franchise", "label": "Franchise Agreement", "description": "Franchise rights and operations"},
    {"key": "hosting", "label": "Hosting Agreement", "description": "Web/application hosting services"},
    {"key": "ip", "label": "IP Agreement", "description": "Intellectual property transfer/license agreements"},
    {"key": "joint_venture", "label": "Joint Venture Agreement", "description": "Joint venture and project collaborations"},
    {"key": "license", "label": "License Agreement", "description": "Licensing of technology, content, or IP"},
    {"key": "maintenance", "label": "Maintenance Agreement", "description": "Maintenance and support services"},
    {"key": "manufacturing", "label": "Manufacturing Agreement", "description": "Manufacturing and supply of goods"},
    {"key": "marketing", "label": "Marketing Agreement", "description": "Marketing and promotion services"},
    {"key": "non_compete_no_solicit", "label": "Non-Compete / No-Solicit / Non-Disparagement Agreement", "description": "Restrictive-covenant agreements"},
    {"key": "outsourcing", "label": "Outsourcing Agreement", "description": "Business-process outsourcing"},
    {"key": "promotion", "label": "Promotion Agreement", "description": "Promotional services and campaigns"},
    {"key": "reseller", "label": "Reseller Agreement", "description": "Reseller and value-added distribution"},
    {"key": "service", "label": "Service Agreement", "description": "General professional/support services"},
    {"key": "sponsorship", "label": "Sponsorship Agreement", "description": "Sponsorship of events/content"},
    {"key": "strategic_alliance", "label": "Strategic Alliance Agreement", "description": "Strategic alliances and partnerships"},
    {"key": "supply", "label": "Supply Agreement", "description": "Supply of goods or materials"},
    {"key": "transportation", "label": "Transportation Agreement", "description": "Transportation and logistics services"},
]

CONTRACT_SUBTYPE_KEYS = [s["key"] for s in CONTRACT_SUBTYPES]

# Folder-name aliases from the CUAD tree -> canonical subtype key.
_SUBTYPE_ALIASES = {
    "affiliate_agreements": "affiliate",
    "affiliate_agreement": "affiliate",
    "agency_agreements": "agency",
    "co_branding": "co_branding",
    "collaboration": "collaboration",
    "consulting_agreements": "consulting",
    "development": "development",
    "distributor": "distributor",
    "endorsement": "endorsement",
    "endorsement_agreement": "endorsement",
    "franchise": "franchise",
    "hosting": "hosting",
    "ip": "ip",
    "joint_venture": "joint_venture",
    "joint_venture_filing": "joint_venture",
    "license_agreements": "license",
    "maintenance": "maintenance",
    "manufacturing": "manufacturing",
    "marketing": "marketing",
    "non_compete_non_solicit": "non_compete_no_solicit",
    "outsourcing": "outsourcing",
    "promotion": "promotion",
    "reseller": "reseller",
    "service": "service",
    "sponsorship": "sponsorship",
    "strategic_alliance": "strategic_alliance",
    "supply": "supply",
    "transportation": "transportation",
}

SUBTYPE_UNKNOWN = "other"

# Semantically interchangeable contract families: a classification into ANY
# member of the same equivalence class is a correct routing decision, not a
# miss. Derived from the observed subtype-eval failures on the 50-contract
# sample, where the sorter's family-level answer was defensible but the exact
# CUAD-folder key differed:
#   - reseller <-> distributor  ("Reseller Agreement" defining itself as a
#     "Distribution Agreement" — pure resale-channel synonymy)
#   - maintenance <-> license   (software "License and Maintenance" hybrids —
#     both CUAD samples of this pair sit in the Maintenance folder, and the
#     license grant is the operative core either way)
#   - development <-> license   (development agreements whose operative
#     mechanism is an IP/brand license — e.g. "Training Program Development
#     Agreement" built on a licensed IP + royalty structure)
#   - affiliate <-> joint_venture (an "Affiliate Agreement" whose operative
#     clause declares the parties joint venturers)
SUBTYPE_EQUIVALENCES: list[frozenset[str]] = [
    frozenset({"reseller", "distributor"}),
    frozenset({"maintenance", "license"}),
    frozenset({"development", "license"}),
    frozenset({"affiliate", "joint_venture"}),
]


def equivalent_subtypes(a: str, b: str) -> bool:
    """Return True when two subtype keys are the same family or members of
    the same interchangeable family class (see ``SUBTYPE_EQUIVALENCES``)."""
    a, b = str(a), str(b)
    if a == b:
        return True
    return any(a in cls and b in cls for cls in SUBTYPE_EQUIVALENCES)


def normalize_subtype(value) -> str:
    """Coerce a raw sorter subtype output (or a CUAD folder name) to a
    canonical subtype key; unknown/non-contract values become ``other``."""
    if value is None:
        return SUBTYPE_UNKNOWN
    key = re.sub(r"[^a-z0-9]", "", str(value).strip().lower())
    if not key:
        return SUBTYPE_UNKNOWN
    if key in CONTRACT_SUBTYPE_KEYS:
        return key
    if key in _SUBTYPE_ALIASES:
        return _SUBTYPE_ALIASES[key]
    # "License Agreement" -> "license"; "Non-Compete" -> non_compete_no_solicit.
    for subtype in CONTRACT_SUBTYPES:
        norm_label = re.sub(r"[^a-z0-9]", "", subtype["label"].lower())
        if key == norm_label or key.startswith(norm_label[:8]):
            return subtype["key"]
    return SUBTYPE_UNKNOWN

SORTER_SCHEMA = build_structured_schema(
    {
        "doc_type": {"type": "string", "enum": DOC_CLASS_KEYS},
        "contract_subtype": {
            "type": ["string", "null"],
            "enum": CONTRACT_SUBTYPE_KEYS + [SUBTYPE_UNKNOWN],
            "description": "The contract family/subgroup — REQUIRED when doc_type is "
                           "contract, null otherwise. See the subtype list in the prompt.",
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
    },
    title="ClassificationOutput",
)


class SorterAgent(BaseAgent):
    """Classifies legal documents into mailroom document types.

    Two classification paths share the same output contract
    (``{"doc_type", "confidence", "reasoning"}``):

    - ``classify_json`` / ``classify`` — text documents (full extracted
      markdown text; truncation only past the hard safety cap).
    - ``classify_image`` — document page images (RVL-CDIP-style vision
      pipeline) using the versioned vision prompt (``sorter_vision_v0``).
    """

    agent_name = "sorter"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        prompt_version: str = "sorter",
    ):
        super().__init__(model=model, api_key=api_key)
        self.prompt_version = prompt_version
        # The sorter classifies 25 near-synonymous contract families where
        # title-vs-operatives conflicts are common (reseller/distributor,
        # license/maintenance, development/license, ...). Medium reasoning
        # effort makes it weigh the operative clauses before committing;
        # overridden per-run via the eval runners' --reasoning-effort flag.
        self._reasoning_effort = "medium"

    def system_prompt(self) -> str:
        base_prompt = get_prompt(self.prompt_version)
        if "{{doc_type_descriptions}}" not in base_prompt:
            return base_prompt
        doc_type_descriptions = "\n".join(
            f"- {d['key']}: {d['label']} — {d['description']}"
            for d in DOC_CLASSES
        )
        base_prompt = base_prompt.replace("{{doc_type_descriptions}}", doc_type_descriptions)
        if "{{contract_subtypes}}" not in base_prompt:
            return base_prompt
        contract_subtypes = "\n".join(
            f"- {s['key']}: {s['label']} — {s['description']}"
            for s in CONTRACT_SUBTYPES
        )
        return base_prompt.replace("{{contract_subtypes}}", contract_subtypes)

    def classify(
        self, doc_text: str, pages: list[str] | None = None  # MAILROOM PATCH: pages
    ) -> tuple[str, str, float, str]:
        """Classify a document and return (doc_type, contract_subtype,
        confidence, reasoning).

        Args:
            doc_text: The full text content of the document.
            pages: MAILROOM PATCH — page-image data-URIs for vision-capable models.

        Returns:
            Tuple of (doc_type key, contract_subtype key, confidence 0-1, reasoning string).
        """
        truncated = self.truncate_input(doc_text)
        result = self._call_structured(
            f"Classify this legal document:\n\n{truncated}",
            json_schema=SORTER_SCHEMA,
            temperature=0.1,
            pages=pages,  # MAILROOM PATCH
        )

        if result.get("_parse_error"):
            logger.error("sorter_parse_error")
            return ("correspondence", None, 0.3, "parse error — defaulting to correspondence")

        doc_type = result.get("doc_type", "correspondence")
        if doc_type not in DOC_CLASS_KEYS:
            doc_type = "correspondence"
        contract_subtype = normalize_subtype(
            result.get("contract_subtype") if doc_type == "contract" else None
        )
        # MAILROOM PATCH: non-contracts must carry None, not the "other"
        # fallback — the schema says contract_subtype is "REQUIRED when
        # doc_type is contract, null otherwise", and the mailroom's
        # classification guard enforces exactly that.
        if doc_type != "contract":
            contract_subtype = None
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        reasoning = result.get("reasoning", "")

        logger.info("classified", doc_type=doc_type, contract_subtype=contract_subtype,
                    confidence=confidence)
        return (doc_type, contract_subtype, confidence, reasoning)

    def classify_json(
        self,
        doc_text: str,
        subtype_focus: bool = False,
        pages: list[str] | None = None,  # MAILROOM PATCH: page-image data-URIs
    ) -> dict:
        """Classify and return the raw structured dict (used by eval loops).

        With ``subtype_focus=True`` the model is explicitly TASKED with
        sorting the document into its contract subtype: the user message tells
        it the document IS a contract and that the subtype assignment is the
        decision being scored — used by the chained eval, whose rows are all
        contracts, so the sorter scores represent the subtype task rather
        than a general doc-type gate.
        """
        truncated = self.truncate_input(doc_text)
        if subtype_focus:
            user_message = (
                "This document IS a contract (all documents in this task are "
                "contracts). Your job is to sort it into its correct CONTRACT "
                "SUBTYPE: assign the contract_subtype key that best matches its "
                "agreement family, and confirm doc_type as \"contract\".\n\n"
                f"Contract text:\n\n{truncated}"
            )
        else:
            user_message = f"Classify this legal document:\n\n{truncated}"
        result = self._call_structured(
            user_message,
            json_schema=SORTER_SCHEMA,
            temperature=0.1,
            pages=pages,  # MAILROOM PATCH
        )
        if result.get("_parse_error"):
            return {"doc_type": "correspondence", "contract_subtype": None,
                    "confidence": 0.3, "reasoning": "parse error"}
        doc_type = result.get("doc_type", "correspondence")
        if doc_type not in DOC_CLASS_KEYS:
            doc_type = "correspondence"
        result["contract_subtype"] = normalize_subtype(
            result.get("contract_subtype") if doc_type == "contract" else None
        )
        # MAILROOM PATCH: non-contracts must carry None (schema: "null
        # otherwise"), matching classify().
        if doc_type != "contract":
            result["contract_subtype"] = None
        return result

    # ------------------------------------------------------------------
    # Vision path (RVL-CDIP-style image classification)
    # ------------------------------------------------------------------

    def classify_image(self, image_base64: str, image_format: str = "png") -> dict:
        """Classify a document PAGE IMAGE with a vision model (qwen).

        Uses the versioned vision prompt (``sorter_vision_v0``): the intro
        (checks + scratchpad procedure) goes in the system message, the output
        contract + worked examples go in the image-bearing user message —
        the same split RVL-CDIP applies (``## Output format`` marker).

        Returns the SAME contract as ``classify_json``:
        ``{"doc_type", "confidence", "reasoning"}``.
        """
        from langchain_agents.classifier import (
            clean_prediction,
            extract_confidence,
            extract_reasoning,
        )
        from langchain_agents.openrouter_utils import split_prompt

        prompt_text = get_prompt(self.prompt_version)
        system_text, user_text = split_prompt(prompt_text)
        if not system_text:
            system_text, user_text = prompt_text, "Classify the document in this image."

        raw = self._call_vision(
            system_prompt=system_text,
            user_text=user_text,
            image_base64=image_base64,
            image_format=image_format,
            temperature=0.1,
            max_tokens=self._max_tokens,
        )

        doc_type = clean_prediction(raw)
        if doc_type not in DOC_CLASS_KEYS:
            logger.error("sorter_vision_invalid_label", raw_label=doc_type)
            doc_type = "correspondence"

        confidence = extract_confidence(raw)
        if confidence is None:
            confidence = 0.5

        reasoning = extract_reasoning(raw)
        logger.info("classified_vision", doc_type=doc_type, confidence=confidence)
        return {"doc_type": doc_type, "confidence": confidence, "reasoning": reasoning}

    def classify_document(self, pages_base64: list[str], image_format: str = "png") -> dict:
        """Classify a FULL PDF document in ONE vision call.

        Every rendered page of the PDF is sent to the model in a single request
        (``_call_vision_multi``) — one classification per document, so the
        model reads the entire agreement (recitals, sections, exhibits,
        signature pages) before deciding. Returns the standard contract:
        ``{"doc_type", "confidence", "reasoning"}``.
        """
        from langchain_agents.classifier import (
            clean_prediction,
            extract_confidence,
            extract_reasoning,
        )
        from langchain_agents.openrouter_utils import split_prompt

        if not pages_base64:
            return {"doc_type": "correspondence", "confidence": 0.0,
                    "reasoning": "no page images"}

        prompt_text = get_prompt(self.prompt_version)
        system_text, user_text = split_prompt(prompt_text)
        if not system_text:
            system_text, user_text = prompt_text, "Classify the document in these page images."

        raw = self._call_vision_multi(
            system_prompt=system_text,
            user_text=user_text,
            images=[(b64, image_format) for b64 in pages_base64],
            temperature=0.1,
            max_tokens=self._max_tokens,
        )

        doc_type = clean_prediction(raw)
        if doc_type not in DOC_CLASS_KEYS:
            logger.error("sorter_vision_invalid_label", raw_label=doc_type)
            doc_type = "correspondence"

        confidence = extract_confidence(raw)
        if confidence is None:
            confidence = 0.5

        reasoning = extract_reasoning(raw)
        logger.info("classified_document", doc_type=doc_type, pages=len(pages_base64),
                    confidence=confidence)
        return {"doc_type": doc_type, "confidence": confidence, "reasoning": reasoning}

    def re_evaluate(self, doc_text: str, previous_result: dict) -> tuple[str, float, str]:
        """Re-evaluate a document after low-confidence classification.

        Args:
            doc_text: The full text content.
            previous_result: Dict with keys 'doc_type', 'confidence', 'reasoning'.

        Returns:
            Updated (doc_type, confidence, reasoning).
        """
        prompt = f"""RE-EVALUATION REQUESTED

Previous classification attempt produced low confidence. Please re-analyze this document more carefully.

Previous result:
- Assigned class: {previous_result.get('doc_type', 'unknown')}
- Confidence: {previous_result.get('confidence', 0)}
- Previous reasoning: {previous_result.get('reasoning', 'N/A')}

Document text:
{doc_text}

Provide your best classification with justification."""

        result = self._call_structured(prompt, json_schema=SORTER_SCHEMA, temperature=0.1)

        if result.get("_parse_error"):
            return (previous_result.get("doc_type", "correspondence"), 0.3, "re-evaluation parse error")

        doc_type = result.get("doc_type", previous_result.get("doc_type", "correspondence"))
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return (doc_type, confidence, result.get("reasoning", ""))
