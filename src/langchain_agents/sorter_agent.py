# VENDORED from github.com/Exios66/llm-entity-extraction (verified against
# commit 3a03d5c, 2026-08-10 — issue #10 alignment check: CONTRACT_SUBTYPES,
# _SUBTYPE_ALIASES, SUBTYPE_EQUIVALENCES, SORTER_SCHEMA, DOC_CLASSES, and the
# sorter_v5 prompt are byte-identical to upstream; the only divergences are the
# MAILROOM PATCHes below and the corrected 4-tuple return annotation, which
# matches upstream's actual return value — upstream declares a stale
# ``tuple[str, float, str]`` but returns (doc_type, subtype, confidence,
# reasoning)).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""SorterAgent — Legal Document Classification Agent (LangChain).

Classifies documents into one of the live mailroom document types with confidence
scoring. The system prompt is loaded BY VERSION from ``src.prompts`` so the
evaluation loops can test exactly one prompt version per Braintrust experiment.
"""

from __future__ import annotations

import re

import structlog
from langchain_agents.base_agent import BaseAgent, build_structured_schema
from langchain_agents.prompts import get_prompt

logger = structlog.get_logger(__name__)

# MAILROOM PATCH: canonical live production taxonomy. Retired classes are not
# sorter outputs; they resolve to ``unknown`` and therefore route to review.
DOC_CLASSES = [
    {"key": "contract", "label": "Contract / Agreement", "description": "CUAD commercial contracts and agreements (vendor, employment, NDA, license, etc.) — not MAUD merger agreements"},
    {"key": "merger_agreement", "label": "Merger Agreement", "description": "MAUD merger agreements (agreement and plan of merger) — a distinct class from CUAD commercial contracts. Subclass is consideration type (all_cash, all_stock, mixed, …)."},
    {"key": "corporate_record", "label": "Corporate Record", "description": "Bylaws, articles/certificates of incorporation, powers of attorney, stockholder rights instruments, specimen stock (including those filed as SEC exhibits)"},
    {"key": "correspondence", "label": "Correspondence", "description": "Letters, emails, memos, notices, demand letters, press releases, meeting requests"},
    {"key": "insurance_claim", "label": "Insurance Claim", "description": "Insurance claim documentation: FNOL forms, adjuster reports, demand packages, coverage determinations, denial letters, and CMS/DE-SynPUF claim tables (inpatient, outpatient, PDE, carrier)"},
]

DOC_CLASS_KEYS = [d["key"] for d in DOC_CLASSES]


def _doc_classes_for_prompt() -> list[dict]:
    """Prefer the live taxonomy catalog; fall back to the hardcoded table."""
    try:
        from pipeline.config import get_doc_class_catalog

        catalog = get_doc_class_catalog()
        if catalog:
            return catalog
    except Exception:
        pass
    return DOC_CLASSES


def _sorter_schema() -> dict:
    """Structured-output schema: live classes plus the ``unknown`` routing token.

    MAILROOM PATCH: doctrine tells the model to emit ``unknown`` for court
    opinions / DD memos. Restricting the enum to extractable classes forced
    those documents onto a specialist.
    """
    try:
        from pipeline.config import get_sorter_label_set

        labels = sorted(get_sorter_label_set())
    except Exception:
        labels = list(DOC_CLASS_KEYS) + ["unknown"]
    return build_structured_schema(
        {
            "doc_type": {"type": "string", "enum": labels},
            "contract_subtype": {
                "type": ["string", "null"],
                "enum": CONTRACT_SUBTYPE_KEYS + [SUBTYPE_UNKNOWN],
                "description": "The contract family/subgroup — REQUIRED when doc_type is "
                               "contract, null otherwise. See the subtype list in the prompt.",
            },
            "doc_subclass": {
                "type": ["string", "null"],
                "description": (
                    "Per-class subclass from the catalogs in the user message "
                    "(CUAD family for contract, MAUD consideration for "
                    "merger_agreement, Hub/dojo tokens for other live classes). "
                    "REQUIRED when the chosen class has a catalog; null for "
                    "unknown. Not an enum — SEC form types keep their hyphens."
                ),
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
        },
        title="ClassificationOutput",
    )

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
    compact = {re.sub(r"[^a-z0-9]", "", k): k for k in CONTRACT_SUBTYPE_KEYS}
    if key in compact:
        return compact[key]
    alias_norm = {re.sub(r"[^a-z0-9]", "", k): v for k, v in _SUBTYPE_ALIASES.items()}
    if key in alias_norm:
        return alias_norm[key]
    # "License Agreement" -> "license"; "Non-Compete" -> non_compete_no_solicit.
    for subtype in CONTRACT_SUBTYPES:
        norm_label = re.sub(r"[^a-z0-9]", "", subtype["label"].lower())
        if key == norm_label or key.startswith(norm_label[:8]):
            return subtype["key"]
    return SUBTYPE_UNKNOWN


def finalize_sorter_result(result: dict) -> dict:
    """Normalize sorter JSON: CUAD ``contract_subtype`` + per-class ``doc_subclass``.

    ``contract_subtype`` stays CUAD-only (null for every non-contract) so the
    existing classification guard still holds. ``doc_subclass`` carries the
    dojo per-class catalog token (CUAD family for contracts, MAUD
    consideration for merger agreements, Hub/dojo tokens otherwise).
    """
    out = dict(result or {})
    doc_type = out.get("doc_type") or ""
    raw_subclass = out.get("doc_subclass")
    raw_cuad = out.get("contract_subtype")
    candidate = raw_subclass if raw_subclass not in (None, "") else raw_cuad

    if doc_type == "contract":
        key = normalize_subtype(candidate)
        out["contract_subtype"] = key
        out["doc_subclass"] = key
        return out

    out["contract_subtype"] = None
    try:
        from langchain_agents.doc_inventories import (
            normalize_sorter_subclass,
            sorter_subclass_catalog,
            valid_sorter_subclasses,
        )
    except Exception:
        out["doc_subclass"] = None
        return out

    if not sorter_subclass_catalog(doc_type):
        out["doc_subclass"] = None
        return out
    if candidate in (None, ""):
        out["doc_subclass"] = None
        return out
    token = normalize_sorter_subclass(doc_type, candidate)
    catalog = valid_sorter_subclasses(doc_type)
    if token in catalog:
        out["doc_subclass"] = token
    else:
        out["doc_subclass"] = str(candidate).strip()
    return out


def _classification_user_message(doc_text: str, *, subtype_focus: bool = False) -> str:
    from langchain_agents.doc_inventories import format_sorter_subclass_catalogs

    catalogs = format_sorter_subclass_catalogs()
    if subtype_focus:
        body = (
            "This document IS a contract (all documents in this task are "
            "contracts). Your job is to sort it into its correct CONTRACT "
            "SUBTYPE: assign the contract_subtype key that best matches its "
            "agreement family, copy that same key into doc_subclass, and "
            "confirm doc_type as \"contract\".\n\n"
            f"Contract text:\n\n{doc_text}"
        )
    else:
        body = f"Classify this legal document:\n\n{doc_text}"
    return f"{body}\n\n{catalogs}"


SORTER_SCHEMA = _sorter_schema()


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
        callbacks: list | None = None,
    ):
        super().__init__(model=model, api_key=api_key, callbacks=callbacks)
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
            for d in _doc_classes_for_prompt()
        )
        base_prompt = base_prompt.replace("{{doc_type_descriptions}}", doc_type_descriptions)
        if "{{contract_subtypes}}" in base_prompt:
            contract_subtypes = "\n".join(
                f"- {s['key']}: {s['label']} — {s['description']}"
                for s in CONTRACT_SUBTYPES
            )
            base_prompt = base_prompt.replace("{{contract_subtypes}}", contract_subtypes)
        if "{{doc_subclasses}}" in base_prompt:
            from langchain_agents.doc_inventories import format_sorter_subclass_catalogs

            base_prompt = base_prompt.replace(
                "{{doc_subclasses}}", format_sorter_subclass_catalogs()
            )
        return base_prompt

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
        user_message = _classification_user_message(
            truncated, subtype_focus=subtype_focus
        )
        result = self._call_structured(
            user_message,
            json_schema=_sorter_schema(),
            temperature=0.1,
            pages=pages,  # MAILROOM PATCH
        )
        if result.get("_parse_error"):
            logger.error("sorter_parse_error")
            return {
                "doc_type": "correspondence",
                "contract_subtype": None,
                "doc_subclass": None,
                "confidence": 0.3,
                "reasoning": "parse error — defaulting to correspondence",
            }
        # MAILROOM PATCH: never silently remap an unknown class onto
        # correspondence at the model's confidence.
        result["doc_type"] = result.get("doc_type") or ""
        result = finalize_sorter_result(result)
        try:
            result["confidence"] = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            result["confidence"] = 0.5
        logger.info(
            "classified",
            doc_type=result.get("doc_type"),
            contract_subtype=result.get("contract_subtype"),
            doc_subclass=result.get("doc_subclass"),
            confidence=result.get("confidence"),
        )
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
        # MAILROOM PATCH: do NOT coerce an unknown/retired/hallucinated
        # label onto `correspondence` at the model's confidence — that is
        # the vision twin of the text-path defect. Invalid labels stay as-is
        # so after_classify parks them for review.
        if doc_type not in DOC_CLASS_KEYS:
            logger.error("sorter_vision_invalid_label", raw_label=doc_type)

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
            from pipeline.config import UNKNOWN_DOC_TYPE

            return {"doc_type": UNKNOWN_DOC_TYPE, "confidence": 0.0,
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
        # MAILROOM PATCH: same as classify_image — never silently remap.
        if doc_type not in DOC_CLASS_KEYS:
            logger.error("sorter_vision_invalid_label", raw_label=doc_type)

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

        result = self._call_structured(prompt, json_schema=_sorter_schema(), temperature=0.1)

        if result.get("_parse_error"):
            from pipeline.config import UNKNOWN_DOC_TYPE

            return (
                previous_result.get("doc_type") or UNKNOWN_DOC_TYPE,
                0.3,
                "re-evaluation parse error",
            )

        from pipeline.config import UNKNOWN_DOC_TYPE

        doc_type = result.get("doc_type") or previous_result.get("doc_type") or UNKNOWN_DOC_TYPE
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return (doc_type, confidence, result.get("reasoning", ""))
