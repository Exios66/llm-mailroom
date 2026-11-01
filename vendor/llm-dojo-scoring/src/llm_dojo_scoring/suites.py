"""Dedicated per-agent scoring suites.

Task bundles (``bundles``) group metrics by what an agent *does*.
Doc-type bundles (``doc_bundles``) group them by the *kind of document*.
This module is the third, consumer-facing layer: **one importable suite
per pipeline agent**, so llm-mailroom / local-mailroom-sandbox can do::

    from llm_dojo_scoring import get_suite, score_suite

    result = get_suite("sorter").score(expected, predicted)
    result = get_suite("insurance_claims_specialist").score(
        expected_fields, predicted_fields, doc_text=text,
    )
    result = get_suite("contract").score(...)   # doc-type alias

instead of assembling a profile + bundle + field-type map themselves.

Honesty mandate (KANBAN-067): suites only compute with functions that
already exist in this package. Type-specific scorers that are still
future work are recorded on ``ScoringSuite.honest_gap`` rather than
invented here. Enron topic/sentiment, MAUD per-question extraction,
WER/CER, field-micro extraction P/R/F1/F2, and insurance
determination-consistency now ship as real scorers. Remaining gaps
(retired court/DD, zero-row compliance, corporate_record with no
*external* extraction benchmark, CMS GT homogeneity) stay documented.
New scorers land by adding a metric to the registry and an extra on
the matching suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .bundles import Bundle, bundle_metric_names
from .field_scoring import ExtractionScoreResult, score_extraction, score_field
from .profiles import AgentProfile, DEFAULT_PROFILES, get_profile
from .registry import load_registry
from .tasks import score_task

__all__ = [
    "ScoringSuite",
    "DEFAULT_SUITES",
    "DEFAULT_FIELD_TYPES",
    "LEGACY_FULL_EXTRACTION_FIELD_TYPES",
    "SPECIALIST_DOC_TYPES",
    "DOC_TYPE_ALIASES",
    "get_suite",
    "list_suites",
    "score_suite",
    "suite_for_doc_type",
]


#: Specialist profile name → native mailroom document class.
SPECIALIST_DOC_TYPES: dict[str, str] = {
    "contracts_specialist": "contract",
    "merger_agreement_specialist": "merger_agreement",
    "corporate_records_specialist": "corporate_record",
    "due_diligence_specialist": "due_diligence",
    "correspondence_specialist": "correspondence",
    "compliance_specialist": "compliance_filing",
    "court_opinions_specialist": "court_opinion",
    "insurance_claims_specialist": "insurance_claim",
}

#: Doc-type lookup aliases (mailroom ``doc_type`` → specialist suite).
#: ``merger_agreement`` is ``MergerAgreementExtraction`` scored by its own
#: specialist — not an extract alias of ``contract``.
DOC_TYPE_ALIASES: dict[str, str] = {
    **{doc_type: agent for agent, doc_type in SPECIALIST_DOC_TYPES.items()},
}

#: Default field→scoring-type maps, mirrored from llm-mailroom
#: ``config/taxonomy.yaml`` / ``EXTRACTION_SCHEMAS`` (v0.6.0 pared product).
#: Open-ended ``key_obligations`` / ``termination_clauses`` / ``key_provisions``
#: / long ``key_points`` are retired from the live board — score CUAD/MAUD/
#: insurance checklists + the semantic trio instead. Override with
#: ``field_types=`` on ``score()``, or use
#: :data:`LEGACY_FULL_EXTRACTION_FIELD_TYPES` for historical free-text dumps.
DEFAULT_FIELD_TYPES: dict[str, dict[str, str]] = {
    "contract": {
        "document_name": "name",
        "parties": "entity_list:name",
        "effective_date": "date",
        "term_length": "free_text",
        "governing_law": "name",
        "contract_value": "money",
        "renewal_terms": "free_text",
        "cuad_family": "name",
        "merger_consideration": "name",
        "cuad_clauses": "entity_list:free_text",
        "maud_clauses": "entity_list:free_text",
    },
    "corporate_record": {
        "entity_name": "name",
        "record_type": "name",
        "effective_date": "date",
        "signatories": "entity_list:name",
        "jurisdiction": "name",
        "filing_number": "id",
        "intent": "label",
        "subject_matter": "free_text",
        "keywords": "entity_list:name",
    },
    "due_diligence": {
        "target_entity": "name",
        "diligence_type": "name",
        "material_findings": "entity_list:free_text",
        "risk_flags": "entity_list:free_text",
        "outstanding_items": "entity_list:free_text",
        "document_date": "date",
        "prepared_by": "name",
    },
    "correspondence": {
        "sender": "name",
        "recipient": "name",
        "additional_recipients": "entity_list",
        "communication_type": "name",
        "communication_date": "date",
        "demand_amount": "money",
        "action_items": "entity_list",
        "urgency": "name",
        "intent": "label",
        "subject_matter": "free_text",
        "keywords": "entity_list:name",
    },
    "compliance_filing": {
        "filing_type": "name",
        "regulatory_body": "name",
        "filing_date": "date",
        "due_date": "date",
        "entity_name": "name",
        "key_requirements": "entity_list:free_text",
        "status": "name",
        "reference_number": "id",
    },
    "court_opinion": {
        "case_name": "name",
        "court": "name",
        "date_decided": "date",
        "docket_number": "id",
        "opinion_type": "name",
        "parties": "entity_list:name",
        "holding": "free_text",
        "legal_issues": "entity_list:free_text",
        "outcome": "free_text",
        "citations": "entity_list:id",
        "authored_by": "name",
    },
    "insurance_claim": {
        "claim_number": "id",
        "policy_number": "id",
        "insurer": "name",
        "insured_party": "name",
        "claim_type": "name",
        "date_of_loss": "date",
        "date_filed": "date",
        "claimed_amount": "money",
        "adjuster": "name",
        "damages_description": "free_text",
        "coverage_determination": "name",
        "denial_reasons": "entity_list:free_text",
        "supporting_documents": "entity_list",
        "intent": "label",
        "subject_matter": "free_text",
        "keywords": "entity_list:name",
        "claim_checklist": "entity_list:free_text",
    },
    "merger_agreement": {
        "document_name": "name",
        "parties": "entity_list:name",
        "effective_date": "date",
        "effective_time": "free_text",
        "governing_law": "name",
        "merger_consideration": "name",
        "maud_clauses": "entity_list:free_text",
        "intent": "name",
        "subject_matter": "free_text",
        "keywords": "entity_list:name",
    },
}

#: Pre-v0.6.0 mailroom field maps that still score open-ended free-text
#: obligation dumps. Use only for historical rescoring — live board numbers
#: should use :data:`DEFAULT_FIELD_TYPES`.
LEGACY_FULL_EXTRACTION_FIELD_TYPES: dict[str, dict[str, str]] = {
    "contract": {
        "document_name": "name",
        "parties": "entity_list:name",
        "effective_date": "date",
        "term_length": "free_text",
        "termination_clauses": "entity_list:free_text",
        "governing_law": "name",
        "key_obligations": "entity_list:free_text",
        "contract_value": "money",
        "renewal_terms": "free_text",
        "cuad_family": "name",
        "merger_consideration": "name",
        "cuad_clauses": "entity_list:free_text",
        "maud_clauses": "entity_list:free_text",
    },
    "merger_agreement": {
        "document_name": "name",
        "parties": "entity_list:name",
        "effective_date": "date",
        "term_length": "free_text",
        "termination_clauses": "entity_list:free_text",
        "governing_law": "name",
        "key_obligations": "entity_list:free_text",
        "contract_value": "money",
        "renewal_terms": "free_text",
        "cuad_family": "name",
        "merger_consideration": "name",
        "cuad_clauses": "entity_list:free_text",
        "maud_clauses": "entity_list:free_text",
    },
    "corporate_record": {
        "entity_name": "name",
        "record_type": "name",
        "effective_date": "date",
        "key_provisions": "entity_list:free_text",
        "signatories": "entity_list:name",
        "jurisdiction": "name",
        "filing_number": "id",
    },
    "correspondence": {
        "sender": "name",
        "recipient": "name",
        "additional_recipients": "entity_list",
        "communication_type": "name",
        "communication_date": "date",
        "key_points": "entity_list",
        "demand_amount": "money",
        "action_items": "entity_list",
        "urgency": "name",
        "referenced_communications": "entity_list",
    },
}

#: Per-agent extras on top of the task-bundle surface. Only names that
#: already resolve in the registry (no invented KPIs).
_AGENT_EXTRAS: dict[str, tuple[str, ...]] = {
    "sorter": (
        "confusion_matrix",
        "failure_mode_breakdown",
        "per_class_stats",
        "bootstrap_ci",
    ),
    "sorter_reviewer": (
        "confusion_matrix",
        "per_class_stats",
        "bootstrap_ci",
    ),
    "judge": ("confidence_calibration_error",),
    "contracts_specialist": (
        "jaccard_similarity",
        "laziness_rate",
        "hallucination_rate",
        "extraction_category_presence",
        "date_mae_days",
        "money_mae_usd",
        "maud_question_accuracy",
        "maud_question_macro_accuracy",
        "maud_clause_presence",
        "maud_valid_class_rate",
    ),
    "merger_agreement_specialist": (
        "date_mae_days",
        "per_field_scores",
        "hallucination_rate",
        "maud_question_accuracy",
        "maud_question_macro_accuracy",
        "maud_clause_presence",
        "maud_valid_class_rate",
        "maud_category_accuracy",
    ),
    "corporate_records_specialist": (
        "date_mae_days",
        "per_field_scores",
        "hallucination_rate",
    ),
    "due_diligence_specialist": (
        "date_mae_days",
        "per_field_scores",
        "hallucination_rate",
    ),
    "correspondence_specialist": (
        "date_mae_days",
        "money_mae_usd",
        "per_field_scores",
        "hallucination_rate",
        "content_topic_accuracy",
        "content_topic_f1_macro",
        "sentiment_accuracy",
        "sentiment_f1_macro",
    ),
    "compliance_specialist": (
        "date_mae_days",
        "per_field_scores",
        "hallucination_rate",
    ),
    "court_opinions_specialist": (
        "legalbench_accuracy",
        "legalbench_macro_f1",
        "date_mae_days",
        "hallucination_rate",
    ),
    "insurance_claims_specialist": (
        "date_mae_days",
        "money_mae_usd",
        "per_field_scores",
        "hallucination_rate",
        "determination_consistency",
        "amount_exactness",
        "extraction_f2",
    ),
    "pdf_transcriber": (
        "wer",
        "cer",
        "word_accuracy",
    ),
    "image_extractor": (
        "wer",
        "cer",
        "word_accuracy",
    ),
    "intake": (
        "intake_prep_completeness",
        "intake_changed_rate",
        "intake_messy_rate",
        "intake_hyphen_unwraps",
        "intake_collapsed_blanks",
    ),
    "local_vs_api": (
        "ttft_seconds",
        "tokens_per_second",
        "tpot_seconds",
        "e2e_latency_seconds",
        "gpu_utilization",
        "kv_cache_utilization",
        "serving_kind",
        "quantization",
        "model",
        "provider",
    ),
}

#: MAUD per-question extras rebound onto ``get_suite("merger_agreement")``.
_MERGER_EXTRAS: tuple[str, ...] = (
    "maud_question_accuracy",
    "maud_question_macro_accuracy",
    "maud_clause_presence",
    "maud_valid_class_rate",
    "maud_category_accuracy",
)

#: Corpus GT differentiator a content/MAUD extra can actually score. When a
#: suite carries the extra, a GT row holding only that key is scorable on the
#: content metric instead of being suppressed by the fail-closed GT gate (#16).
_GT_KEYS_BY_EXTRA: dict[str, str] = {
    "content_topic_accuracy": "content_topic",
    "content_topic_f1_macro": "content_topic",
    "sentiment_accuracy": "sentiment_label",
    "sentiment_f1_macro": "sentiment_label",
    "maud_question_accuracy": "maud_clause_labels",
    "maud_question_macro_accuracy": "maud_clause_labels",
    "maud_clause_presence": "maud_clause_labels",
    "maud_valid_class_rate": "maud_clause_labels",
    "maud_category_accuracy": "maud_clause_labels",
}

#: Honest-gap notes — type-specific scorers that do NOT exist yet.
_HONEST_GAPS: dict[str, str] = {
    "insurance_claims_specialist": (
        "HONEST GAP: CMS DE-SynPUF ground truth in the published merge is "
        "homogeneous — coverage_determination is all-approved and "
        "denial_reasons is empty — so determination_consistency is "
        "degenerate (always 1.0 on GT-shaped predictions). The scorer "
        "itself now exists (approved ⇒ empty reasons; denied/partial ⇒ "
        "non-empty). Mailroom claim_type accepts Hub source-table tokens "
        "plus legacy FNOL lines; those catalogs are orthogonal and are "
        "not a KPI. adjuster is Optional (null valid for CMS rows)."
    ),
    "due_diligence_specialist": (
        "HONEST GAP: due_diligence was RETIRED from the live llm-mailroom "
        "pipeline (v0.5.0 / PR #21). The sorter emits unknown (human review) "
        "instead of extracting. This suite remains for historical traces; "
        "zero rows in Lucius-Morningstar/mailroom-dataset."
    ),
    "court_opinions_specialist": (
        "HONEST GAP: court_opinion was RETIRED from the live llm-mailroom "
        "pipeline (v0.5.0 / PR #21). The sorter emits unknown. LegalBench "
        "metrics still ship as the real benchmark surface."
    ),
    "corporate_records_specialist": (
        "HONEST GAP: no *external* extraction benchmark (CUAD/MAUD-grade "
        "coverage is not claimed). The published merge covers 450 "
        "corporate_record rows in 10 record-shape subclasses with per-subclass "
        "entity schemas (corporate_record_schema). Key-entity GT is unlabeled "
        "on every row, so those fields are excluded rather than scored as "
        "expected-empty. Suite scores field-micro P/R/F1/F2 on the labeled "
        "fields (intent / subject_matter / keywords today)."
    ),
    "compliance_specialist": (
        "HONEST GAP: compliance_filing was RETIRED from the live llm-mailroom "
        "extract roster (2026-09-15). Zero rows in Lucius-Morningstar/"
        "mailroom-dataset. Hub SEC form-body inventory (10-K, 10-Q, 8-K, …) "
        "stays the historical subclass catalog; this suite remains for "
        "traces, not archive scoring."
    ),
    "local_vs_api": (
        "HONEST GAP: TTFT is None unless a first-token timestamp or explicit "
        "ttft_seconds is recorded — never inferred from e2e/n_tokens. GPU "
        "utilization, KV-cache occupancy, and GPU memory are local-only; "
        "API-key providers (OpenRouter, …) cannot supply them. Local Ollama "
        "tags without an OpenRouter price table leave estimated_cost_usd None "
        "(do not fabricate electricity)."
    ),
}

#: score() kind — routes to an existing package function.
_KIND_CLASSIFICATION = "classification"
_KIND_REVIEW = "review"
_KIND_EXTRACTION = "extraction"
_KIND_AUDIT = "audit"
_KIND_TRANSCRIPTION = "transcription"
_KIND_INTAKE = "intake"
_KIND_REPORTER = "reporter"
_KIND_COST = "cost"
_KIND_SERVING = "serving"

_COMPUTABLE_KINDS = frozenset(
    {
        _KIND_CLASSIFICATION,
        _KIND_REVIEW,
        _KIND_EXTRACTION,
        _KIND_AUDIT,
        _KIND_TRANSCRIPTION,
        _KIND_INTAKE,
        _KIND_SERVING,
    }
)


def _kind_for(profile: AgentProfile) -> str:
    """Choose the scoring kind from an agent's name and declared tasks."""
    if profile.name == "intake" or "prepare" in profile.tasks or "normalize" in profile.tasks:
        return _KIND_INTAKE
    if profile.name in SPECIALIST_DOC_TYPES:
        return _KIND_EXTRACTION
    if profile.name in ("sorter", "judge"):
        return _KIND_CLASSIFICATION
    if profile.name == "sorter_reviewer":
        return _KIND_REVIEW
    if (
        profile.name == "audit_agent"
        or profile.name.endswith("_auditor")
        or profile.name == "arbiter"
    ):
        return _KIND_AUDIT
    if "transcribe" in profile.tasks:
        return _KIND_TRANSCRIPTION
    if profile.name == "archivist" or "store" in profile.tasks:
        return _KIND_COST
    if profile.name == "local_vs_api" or "compare" in profile.tasks:
        return _KIND_SERVING
    return _KIND_REPORTER


def _task_key_for(profile: AgentProfile, kind: str) -> str | None:
    """Resolve task keys for classification, review, court-opinion, and intake suites."""
    if profile.name == "sorter":
        return "docclass"
    if profile.name == "sorter_reviewer":
        return "docclass"
    if profile.name == "judge":
        return "multiclass"
    if profile.name == "court_opinions_specialist":
        return "court_opinion"
    if profile.name == "intake":
        return "intake"
    if kind in (_KIND_CLASSIFICATION, _KIND_REVIEW):
        return "multiclass"
    return None


@dataclass(frozen=True)
class ScoringSuite:
    """One agent's dedicated, importable scoring suite."""

    name: str
    title: str
    kind: str
    profile: AgentProfile
    doc_type: str | None = None
    field_types: dict[str, str] = field(default_factory=dict)
    extra_metrics: tuple[str, ...] = ()
    honest_gap: str | None = None
    task_key: str | None = None
    #: Canonical subclass keys for this agent's document class (empty if none).
    subclasses: tuple[str, ...] = ()
    #: Corpus GT columns that differentiate this document class.
    differentiators: tuple[str, ...] = ()
    #: True when the published mailroom-dataset corpus has rows of this type.
    in_corpus: bool = False
    #: True when the live llm-mailroom pipeline no longer dispatches this agent.
    retired: bool = False

    @property
    def computable(self) -> bool:
        """True when :meth:`score` can run against (expected, predicted)."""
        return self.kind in _COMPUTABLE_KINDS

    def resolve_bundle(self, *, fallback: bool = False) -> Bundle:
        """Delegate metric-bundle resolution and fallback handling to the profile."""
        return self.profile.resolve_bundle(fallback=fallback)

    def resolve_doc_bundle(
        self,
        doc_type: str | None = None,
        *,
        fallback: bool = True,
    ) -> tuple[Bundle, bool]:
        """Resolve a document bundle for the requested type or this suite's type."""
        return self.profile.resolve_doc_bundle(
            doc_type or self.doc_type, fallback=fallback
        )

    def materialize_bundle(self) -> Bundle:
        """Dedicated ``agent:<name>`` bundle: task surface + extras."""
        base = self.resolve_bundle()
        names = list(base.metrics_for(self.name))
        for extra in self.extra_metrics:
            if extra not in names:
                names.append(extra)
        return Bundle(
            name=f"agent:{self.name}",
            description=self.title or f"Dedicated suite for {self.name}",
            metric_names=tuple(names),
        )

    def metric_names(self, *, max_tier: int | None = None) -> list[str]:
        """Full dedicated metric list (task bundle ∩ agent extras)."""
        names = bundle_metric_names(
            self.resolve_bundle(), agent=self.name, max_tier=max_tier
        )
        if max_tier is None:
            for extra in self.extra_metrics:
                if extra not in names:
                    names.append(extra)
        else:
            reg = load_registry()
            for extra in self.extra_metrics:
                if extra not in names and int(reg.get(extra).tier) <= max_tier:
                    names.append(extra)
        return names

    def headline_names(self) -> list[str]:
        """Return the pruned headline metric names for this agent suite."""
        from .pruning import headline_metrics

        return headline_metrics(self.name)

    def normalize_subclass(self, value: Any, *, doc_type: str | None = None) -> str:
        """Normalize a raw subclass into a canonical catalog.

        *doc_type* overrides the suite's bound type so the sorter can
        normalize a label once the parent class is known. Without a
        parent type the result is ``"other"`` — CUAD prefixes are not
        applied to unlabeled values.
        """
        from .corpus import normalize_corpus_subclass

        return normalize_corpus_subclass(doc_type or self.doc_type, value)

    def score(
        self,
        expected: Any,
        predicted: Any,
        *,
        doc_text: str | None = None,
        field_types: dict[str, str] | None = None,
        task: str | None = None,
        metrics: dict[str, Any] | None = None,
        detailed: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Score this agent's outputs with the existing package functions.

        Routing:

        - **extraction** — :func:`score_extraction` with this suite's
          field-type map (override via ``field_types=``). Accepts one
          document (dicts) or a list of documents. Correspondence
          ``content_topic`` / ``sentiment_label`` and merger
          ``maud_clause_labels`` are scored as content extras (not
          extraction fields) when present on the dicts or passed as
          kwargs. Pass ``detailed=True`` (or call
          :meth:`score_document`) to get the full per-document payload
          including ``schema_valid`` / ``parse_ok``, field-micro
          P/R/F1/F2, ``metric_id``, and provenance; the default keeps the
          single-document ``ExtractionScoreResult`` when no extras are
          produced. Batches and results with extras return dictionaries.
          Missing, malformed, or nonempty GT without scorable fields returns
          an unscorable dictionary for a single document. GT may also be a
          JSON or Python dict-repr string; Hub metadata is scoped to the
          suite and stringified field containers are parsed.
        - **classification / review** — :func:`score_task` (default task
          from the suite; override via ``task=``).
        - **audit** — field-type-aware comparison of specialist vs
          auditor output (dicts) via :func:`score_extraction`, plus a
          disagreement rate (``1 - overall_score``).
        - **transcription** — exact match + free-text token F1 + WER/CER.
        - **intake** — deterministic clerk gold vs predicted cleaned text
          or ``normalize-intake`` span payload (prep completeness, messy /
          changed flags, hyphen unwraps). LLM intake is scored against the
          same clerk.
        - **serving** (`local_vs_api`) — compare local vs API-key runs
          (:func:`llm_dojo_scoring.serving.compare_serving`). ``expected``
          is the local record(s); ``predicted`` is the API record(s).
        - **reporter / cost** — emit-only. Pass ``metrics=`` to validate
          a precomputed dict against the suite; otherwise ``TypeError``.
        """
        if metrics is not None:
            return self.validate_metrics(metrics)
        if not self.computable:
            raise TypeError(
                f"{self.name} suite is emit-only (kind={self.kind}); "
                "pass metrics={name: value} to validate, or emit through "
                f"Emitter using metric_names={self.metric_names()!r}"
            )
        if self.kind == _KIND_EXTRACTION:
            return self._score_extraction(
                expected, predicted, doc_text=doc_text, field_types=field_types,
                detailed=detailed, **kwargs,
            )
        if self.kind == _KIND_AUDIT:
            return self._score_audit(
                expected, predicted, doc_text=doc_text, field_types=field_types
            )
        if self.kind == _KIND_TRANSCRIPTION:
            return self._score_transcription(expected, predicted)
        if self.kind == _KIND_INTAKE:
            return self._score_intake(
                expected, predicted, raw_text=kwargs.get("raw_text")
            )
        if self.kind == _KIND_SERVING:
            from .serving import compare_serving

            return compare_serving(
                expected, predicted, quality_metric=kwargs.get("quality_metric")
            )
        task_name = task or self.task_key or "multiclass"
        if (
            self.kind in (_KIND_CLASSIFICATION, _KIND_REVIEW)
            and kwargs.get("expected_subclass") is not None
        ):
            task_name = "docclass"
        return score_task(task_name, expected, predicted, **kwargs)

    def score_document(
        self,
        expected: Any,
        predicted: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Score **one document** and return the full payload for that class.

        Extraction suites only. This is the per-document surface consumers
        should use for archive / comparison work. For scorable document
        dictionaries, it carries
        ``extraction`` (the :class:`ExtractionScoreResult`), field-micro
        ``extraction_precision`` / ``extraction_recall`` / ``extraction_f1`` /
        ``extraction_f2``,
        ``schema_valid`` / ``parse_ok``, applicable class extras (content /
        MAUD / insurance consistency), ``metric_id``, and ``provenance``. It is
        ``score(..., detailed=True)`` with a stable name.

        ``score_document`` accepts the same keyword arguments as
        :meth:`score` (``doc_text``, ``field_types``, provenance stamps,
        ``presence_expectations``, content/MAUD kwargs), except ``detailed``,
        which is supplied internally. Missing, malformed, or nonempty GT
        without scorable fields returns an unscorable dictionary with a
        reason and null scores instead of the full extraction payload.
        Presence-only GT has null field-micro scores and ``overall_score``.

        Raise ``TypeError`` for emit-only or non-extraction suites; errors
        from :meth:`score` propagate.
        """
        if not self.computable:
            raise TypeError(
                f"{self.name} suite is emit-only (kind={self.kind}); "
                "score_document requires an extraction suite"
            )
        if self.kind != _KIND_EXTRACTION:
            raise TypeError(
                f"{self.name} suite kind={self.kind} is not extraction; "
                "score_document is the per-document extraction surface"
            )
        return self.score(expected, predicted, detailed=True, **kwargs)

    def validate_metrics(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """Keep only registry-known names; unknown keys are dropped.

        Returns ``{"emitted": {name: value}, "skipped": [name, ...]}`` so
        consumers never silently invent KPIs.
        """
        reg = load_registry()
        known = set(self.metric_names()) | set(reg.names())
        emitted: dict[str, Any] = {}
        skipped: list[str] = []
        for name, value in metrics.items():
            if name not in known:
                skipped.append(name)
                continue
            emitted[name] = value
        return {"emitted": emitted, "skipped": skipped}

    def _score_extraction(
        self,
        expected: Any,
        predicted: Any,
        *,
        doc_text: str | None,
        field_types: dict[str, str] | None,
        detailed: bool = False,
        **kwargs: Any,
    ) -> ExtractionScoreResult | list[ExtractionScoreResult] | dict[str, Any]:
        """Normalize GT and score extraction, format, and available class extras.

        Return a single ``ExtractionScoreResult`` when there are no extras and
        ``detailed`` is false; otherwise return a dictionary with provenance.
        Invalid or nonempty unscorable single-document GT returns an unscorable
        dictionary. Batches pad missing predictions with ``None`` and ignore
        surplus predictions; a list of ``doc_text`` values can further limit
        extraction results.
        Explicit presence expectations override labels derived from Hub GT.

        Corporate-record dictionary pairs exclude unlabeled entities from both
        sides using ``doc_subclass`` (or ``expected_subclass``), which may be a
        per-row list for batches. Scorable dictionary payloads report
        ``unlabeled_fields``; batch rows without a dictionary pair report ``[]``.
        Extension entities are scored when labeled but are not added to the
        format layer's required keys.

        Dictionary payloads raise ``ProvenanceExportError`` for a conflicting
        caller-supplied ``metric_id`` or an unsupported ``cost_basis``.
        """
        from .content_scoring import (
            peel_non_extraction_fields,
            score_correspondence_content,
            score_maud_extraction,
        )
        from .extraction_metrics import (
            extraction_binary_metrics,
            merge_extraction_counts,
            prf_bundle_keys,
        )
        from .scorecard_honesty import (
            COST_BASIS_VALUES,
            GtAssessment,
            ProvenanceExportError,
            _try_parse_json,
            aggregate_quality_honest,
            assess_extraction_gt,
            check_schema_promotion_gate,
            metric_id_for,
            score_format_layer,
            stamp_provenance,
            unscorable_extraction_result,
        )

        ftypes = field_types or self.field_types
        doc_class = self.doc_type or self.name
        presence = kwargs.get("presence_expectations")
        # The format layer requires the registered core only; subclass key
        # entities are scored (when the dataset labels them) but never required.
        required_ftypes = ftypes
        if doc_class == "corporate_record" and ftypes:
            from .corporate_record_schema import all_entity_field_types

            ftypes = {**all_entity_field_types(), **ftypes}

        def _stamp(payload: dict[str, Any], mid: str) -> dict[str, Any]:
            """Validate caller metric and cost claims before stamping provenance."""
            # Same refusal as Emitter / score_archive_block (#17, #22): a
            # caller's metric_id or cost_basis is checked, never overridden.
            claimed = kwargs.get("metric_id")
            if claimed is not None and claimed != mid:
                raise ProvenanceExportError(
                    f"provenance metric_id {claimed!r} contradicts this "
                    f"payload's metric_id {mid!r}"
                )
            basis = kwargs.get("cost_basis")
            if basis is not None and basis not in COST_BASIS_VALUES:
                raise ProvenanceExportError(
                    f"cost_basis {basis!r} is not one of {sorted(COST_BASIS_VALUES)}"
                )
            return stamp_provenance(
                payload,
                metric_id=mid,
                prompt_id=kwargs.get("prompt_id"),
                dataset_revision=kwargs.get("dataset_revision"),
                split=kwargs.get("split"),
                draw_seed=kwargs.get("draw_seed"),
                serving_kind=kwargs.get("serving_kind"),
                model_id=kwargs.get("model_id"),
                cost_basis=basis,
            )
        # Content/MAUD differentiators this suite can actually score — a GT
        # row holding only these is not suppressed as "no extractable fields".
        scorable_gt_keys = tuple(
            sorted(
                {
                    _GT_KEYS_BY_EXTRA[extra]
                    for extra in self.extra_metrics
                    if extra in _GT_KEYS_BY_EXTRA
                }
            )
        )
        # --- Hub GT metadata normalization -----------------------------------
        # ``mailroom-dataset`` rows carry the union of every class's fields
        # plus annotation stats and a stringified gt_presence map. Scope the
        # GT to THIS suite's class surface so fields that do not apply to the
        # document type (empty / not_applicable / schema_documented_absence /
        # pending_annotation) are never required events, and parse the
        # stringified JSON values ("[]", "{}", '["a", "b"]'). See
        # llm_dojo_scoring.gt_metadata.
        from . import gt_metadata as _gtm

        carries_presence = "extraction_category_presence" in self.extra_metrics

        def _parse_expected_one(value: Any) -> Any:
            """Parse GT dictionaries or strings, retaining invalid strings for assessment."""
            if isinstance(value, str):
                try:
                    return _gtm.parse_gt_fields(value)
                except (TypeError, ValueError):
                    return value  # fail closed -> gt_wrong_schema
            if isinstance(value, dict):
                return _gtm.parse_gt_fields(value)
            return value

        def _scope_expected_one(value: Any) -> Any:
            """Remove annotation keys and blank absent fields in a GT dictionary.

            Apply :func:`scoring_gt_fields` to Hub metadata; preserve unmapped
            keys in plain dictionaries and return non-dict inputs unchanged.
            """
            if not isinstance(value, dict):
                return value
            # Hub metadata carries the stringified gt_presence map; plain
            # field dicts (historical consumers) keep their unmapped keys.
            is_hub_metadata = bool(_gtm.gt_presence_map(value)) or (
                _gtm.GT_PRESENCE_KEY in value
            )
            return _gtm.scoring_gt_fields(
                value,
                field_types=ftypes,
                extra_keys=scorable_gt_keys,
                drop_unmapped=is_hub_metadata,
            )

        scoped_to_empty = False
        scoped_empty_rows: list[bool] = []
        if isinstance(expected, list):
            parsed_expected = [_parse_expected_one(item) for item in expected]
            expected = [_scope_expected_one(item) for item in parsed_expected]
            # Annotation-only Hub rows (e.g. pending CUAD labels) parse as
            # nonempty but scope to {}; keep the flag per row so batches can
            # mark them unscorable without breaking alignment.
            scoped_empty_rows = [
                isinstance(parsed, dict) and bool(parsed) and scoped == {}
                for parsed, scoped in zip(parsed_expected, expected)
            ]
            if presence is None and carries_presence:
                derived = [
                    _gtm.derive_presence_from_gt(item)
                    if isinstance(item, dict)
                    else None
                    for item in parsed_expected
                ]
                if any(entry for entry in derived):
                    presence = derived
        else:
            parsed_one = _parse_expected_one(expected)
            if presence is None and carries_presence and isinstance(parsed_one, dict):
                presence = _gtm.derive_presence_from_gt(parsed_one)
            expected = _scope_expected_one(parsed_one)
            # Annotation-only Hub rows (e.g. pending CUAD labels) parse as
            # nonempty but scope to {}; they carry no extractable fields.
            scoped_to_empty = (
                isinstance(parsed_one, dict) and bool(parsed_one) and expected == {}
            )
        def _parse_predicted_one(value: Any) -> Any:
            """Raw model text → parsed dict, else ``None`` (scored as no fill).

            The raw string still reaches the format layer, so invalid JSON
            fails ``parse_ok`` / ``schema_valid`` instead of crashing the
            field scorer (#18).
            """
            if not isinstance(value, str):
                return value
            parsed, ok = _try_parse_json(value)
            return parsed if ok and isinstance(parsed, dict) else None

        raw_predicted = predicted
        if isinstance(expected, list) and predicted is None:
            # No predictions at all: every GT row is an unfilled prediction.
            raw_predicted = predicted = [None] * len(expected)
        elif isinstance(expected, list) and isinstance(predicted, str):
            # A whole batch answered as one JSON array string.
            parsed_batch, batch_ok = _try_parse_json(predicted)
            raw_predicted = predicted = (
                list(parsed_batch)
                if batch_ok and isinstance(parsed_batch, list)
                else [predicted]
            )
        n_missing_predictions = 0
        if isinstance(expected, list) and isinstance(predicted, list):
            # A GT row with no prediction is an unfilled answer (scored as
            # false negatives), never silently dropped by zip().
            n_missing_predictions = max(0, len(expected) - len(predicted))
            predicted = list(predicted) + [None] * n_missing_predictions
        if isinstance(predicted, str):
            predicted = _parse_predicted_one(predicted)
        elif isinstance(predicted, list):
            predicted = [_parse_predicted_one(item) for item in predicted]
        if isinstance(predicted, dict):
            predicted = _gtm.normalize_field_values(predicted)
        elif isinstance(predicted, list):
            predicted = [
                _gtm.normalize_field_values(item) if isinstance(item, dict) else item
                for item in predicted
            ]
        # corporate_record: key-entity fields the dataset leaves blank without
        # documenting absence are *unlabeled*, not "expected empty". Drop them
        # from both sides so a correct extraction is not scored as spurious
        # fill. See llm_dojo_scoring.corporate_record_schema.
        unlabeled: list[tuple[str, ...]] = []
        audit_predictions = None
        if doc_class == "corporate_record":
            from .corporate_record_schema import align_unlabeled

            # Alignment replaces dictionaries (and batch entries). Keep the
            # original populated fields for verification against doc_text.
            audit_predictions = (
                list(predicted) if isinstance(predicted, list) else predicted
            )
            sub_kw = kwargs.get("doc_subclass") or kwargs.get("expected_subclass")
            if isinstance(expected, list):
                # ``unlabeled_fields`` on a batch payload is per-row and must
                # stay aligned with the GT row order: a row whose prediction
                # is missing (padded ``None``), whose model JSON does not
                # parse, or whose GT object is unparseable gets no alignment
                # at all and reports ``[]`` — never a skipped entry that
                # would shift later rows out of alignment.
                for idx in range(len(expected)):
                    pred = (
                        predicted[idx]
                        if isinstance(predicted, list) and idx < len(predicted)
                        else None
                    )
                    if isinstance(expected[idx], dict) and isinstance(pred, dict):
                        sub = sub_kw[idx] if isinstance(sub_kw, list) and idx < len(sub_kw) else (
                            None if isinstance(sub_kw, list) else sub_kw
                        )
                        expected[idx], predicted[idx], dropped = align_unlabeled(
                            sub, parsed_expected[idx], expected[idx], pred
                        )
                    else:
                        dropped = ()
                    unlabeled.append(dropped)
            elif isinstance(expected, dict) and isinstance(predicted, dict):
                expected, predicted, dropped = align_unlabeled(
                    None if isinstance(sub_kw, list) else sub_kw,
                    parsed_one, expected, predicted,
                )
                unlabeled.append(dropped)
        format_predictions = raw_predicted
        if isinstance(expected, list) and isinstance(raw_predicted, list):
            # The gate covers exactly the GT rows: surplus predictions are
            # dropped and a GT row with no prediction counts as unparsed,
            # so a short prediction list cannot pass on its paired subset.
            padded = list(raw_predicted) + [None] * max(
                0, len(expected) - len(raw_predicted)
            )
            format_predictions = padded[: len(expected)]
            if isinstance(doc_text, list):
                format_predictions = format_predictions[:len(doc_text)]
        format_scores = score_format_layer(
            predicted=format_predictions if not isinstance(raw_predicted, str) else None,
            predicted_raw=(
                raw_predicted
                if isinstance(raw_predicted, str)
                else kwargs.get("predicted_raw")
            ),
            required_keys=list(required_ftypes.keys()) if required_ftypes else None,
        )
        extras: dict[str, Any] = {}
        peeled_exp: list = []
        peeled_pred: list = []
        unscorable_rows: set[int] = set()

        def _run(exp: Any, pred: Any, text: str | None, audit_pred: Any = None):
            """Score one extraction using this suite's document class and field types."""
            return score_extraction(
                doc_class, ftypes, pred, exp, doc_text=text, audit_predicted=audit_pred,
            )

        def _prf_one(exp: Any, pred: Any, result: ExtractionScoreResult) -> dict[str, Any]:
            """Compute field-presence metrics, treating non-mapping predictions as misses."""
            if not isinstance(exp, dict):
                return {}
            if not isinstance(pred, dict):
                # Missing or unparseable answer: every GT field is a miss.
                pred = {}
            return extraction_binary_metrics(
                exp, pred, field_map=ftypes, doc_class=doc_class, result=result
            )

        topic_e = kwargs.get("expected_topic")
        topic_p = kwargs.get("predicted_topic")
        sent_e = kwargs.get("expected_sentiment")
        sent_p = kwargs.get("predicted_sentiment")
        maud_e = kwargs.get("expected_maud")
        maud_p = kwargs.get("predicted_maud")

        gt_target = expected
        if isinstance(expected, list) and len(expected) == 1:
            gt_target = expected[0]
        assessment = assess_extraction_gt(
            gt_target,
            ftypes,
            doc_class=doc_class,
            presence_expectations=presence,
            scorable_gt_keys=scorable_gt_keys,
        )
        if scoped_to_empty and not presence:
            assessment = GtAssessment("unscorable", "gt_no_extractable_fields")
        if not assessment.scorable and not isinstance(expected, list):
            unscorable = unscorable_extraction_result(
                assessment,
                doc_class=doc_class,
                metric_id=metric_id_for(
                    "extraction_overall_score", doc_class=doc_class
                ),
            )
            # The format layer does not depend on GT: a well-formed answer to
            # a triage-only row is not a schema failure.
            unscorable.update(format_scores)
            unscorable["schema_promotion_gate"] = check_schema_promotion_gate(
                format_scores.get("schema_valid"), doc_class=doc_class
            )
            return _stamp(
                unscorable,
                metric_id_for("extraction_overall_score", doc_class=doc_class),
            )

        if isinstance(expected, list) and isinstance(predicted, list):
            texts: Iterable[str | None]
            if isinstance(doc_text, list):
                texts = doc_text
            else:
                texts = [doc_text] * len(expected)
            topics_e: list = []
            topics_p: list = []
            sents_e: list = []
            sents_p: list = []
            mauds_e: list = []
            mauds_p: list = []
            saw_topic = saw_sent = saw_maud = False
            row_assessments: list[GtAssessment] = []
            for idx in range(len(expected)):
                row_presence = (
                    presence[idx]
                    if isinstance(presence, list) and idx < len(presence)
                    else presence
                )
                if (
                    idx < len(scoped_empty_rows)
                    and scoped_empty_rows[idx]
                    and not row_presence
                ):
                    row_assessments.append(
                        GtAssessment("unscorable", "gt_no_extractable_fields")
                    )
                else:
                    # Same fail-closed check as a single document (#16): a
                    # plain triage-only / malformed GT row is unscorable,
                    # never a 0.0 quality row or a run of false negatives.
                    row_assessments.append(
                        assess_extraction_gt(
                            expected[idx],
                            ftypes,
                            doc_class=doc_class,
                            presence_expectations=row_presence,
                            scorable_gt_keys=scorable_gt_keys,
                        )
                    )
            for idx, (exp, pred) in enumerate(zip(expected, predicted)):
                if not row_assessments[idx].scorable:
                    # A GT-less row feeds no topic / sentiment / MAUD / claims
                    # extras; it stays in place only to keep row alignment.
                    peeled_exp.append(exp)
                    peeled_pred.append(pred)
                    unscorable_rows.add(idx)
                elif isinstance(exp, dict):
                    # Missing/malformed answers remain misses in every scored
                    # population. Peel GT extras even when there is no dict
                    # prediction, so they cannot leak into extraction counts.
                    e2, p2, payload = peel_non_extraction_fields(
                        exp, pred if isinstance(pred, dict) else {}
                    )
                    peeled_exp.append(e2)
                    peeled_pred.append(p2)
                    if "content_topic" in payload:
                        saw_topic = True
                        topics_e.append(payload["content_topic"][0])
                        topics_p.append(payload["content_topic"][1])
                    if "sentiment_label" in payload:
                        saw_sent = True
                        sents_e.append(payload["sentiment_label"][0])
                        sents_p.append(payload["sentiment_label"][1])
                    if "maud" in payload:
                        saw_maud = True
                        mauds_e.append(payload["maud"][0])
                        mauds_p.append(payload["maud"][1])
                else:
                    peeled_exp.append(exp)
                    peeled_pred.append(pred)
            extraction = []
            for idx, (exp, pred, text) in enumerate(
                zip(peeled_exp, peeled_pred, texts)
            ):
                row_assessment = row_assessments[idx]
                if not row_assessment.scorable:
                    # Keep row alignment: this row is honestly unscorable,
                    # never scored as gt_empty.
                    extraction.append(
                        unscorable_extraction_result(
                            row_assessment,
                            doc_class=doc_class,
                            metric_id=metric_id_for(
                                "extraction_overall_score", doc_class=doc_class
                            ),
                        )
                    )
                else:
                    audit_pred = (
                        audit_predictions[idx]
                        if isinstance(audit_predictions, list) else None
                    )
                    extraction.append(_run(exp, pred, text, audit_pred))
            if saw_topic and topic_e is None:
                topic_e, topic_p = topics_e, topics_p
            if saw_sent and sent_e is None:
                sent_e, sent_p = sents_e, sents_p
            if saw_maud and maud_e is None:
                maud_e, maud_p = mauds_e, mauds_p
        elif isinstance(expected, dict):
            e2, p2, payload = peel_non_extraction_fields(
                expected, predicted if isinstance(predicted, dict) else {}
            )
            peeled_exp, peeled_pred = [e2], [p2]
            extraction = _run(e2, p2, doc_text, audit_predictions)
            if "content_topic" in payload and topic_e is None:
                topic_e, topic_p = payload["content_topic"]
            if "sentiment_label" in payload and sent_e is None:
                sent_e, sent_p = payload["sentiment_label"]
            if "maud" in payload and maud_e is None:
                maud_e, maud_p = payload["maud"]
        else:
            extraction = _run(expected, predicted, doc_text, audit_predictions)

        if topic_e is not None or topic_p is not None:
            extras.update(
                score_correspondence_content(
                    expected_topic=topic_e if topic_e is not None else "",
                    predicted_topic=topic_p if topic_p is not None else "",
                )
            )
        if sent_e is not None or sent_p is not None:
            extras.update(
                score_correspondence_content(
                    expected_sentiment=sent_e if sent_e is not None else "",
                    predicted_sentiment=sent_p if sent_p is not None else "",
                )
            )
        if maud_e is not None or maud_p is not None:
            maud_result = score_maud_extraction(maud_e, maud_p)
            if maud_result.get("n_questions"):
                extras.update(maud_result)

        # CUAD / claim checklist presence (mailroom v0.6.0 board path). Pass
        # presence_expectations= from Hub GT; default field is cuad_clauses.
        if presence:
            from .field_scoring import score_category_presence

            pred_rows: list = []
            if peeled_pred:
                pred_rows = [p if isinstance(p, dict) else {} for p in peeled_pred]
            elif isinstance(predicted, dict):
                pred_rows = [predicted]
            elif isinstance(predicted, list):
                pred_rows = [p if isinstance(p, dict) else {} for p in predicted]
            if isinstance(presence, list):
                pairs = [
                    (pred, pe)
                    for idx, (pred, pe) in enumerate(zip(pred_rows, presence))
                    if idx not in unscorable_rows and isinstance(pe, dict) and pe
                ]
            else:
                pairs = [(pred, presence) for idx, pred in enumerate(pred_rows)
                         if idx not in unscorable_rows]
            presence_scores: list[float] = []
            presence_detail = None
            for pred, pe in pairs:
                score, detail = score_category_presence(pred, pe, ftypes)
                presence_scores.append(float(score))
                presence_detail = detail
            if presence_scores:
                extras["extraction_category_presence"] = (
                    round(sum(presence_scores) / len(presence_scores), 4)
                    if len(presence_scores) > 1
                    else presence_scores[0]
                )
                if len(presence_scores) == 1 and presence_detail is not None:
                    extras["extraction_category_presence_detail"] = presence_detail

        is_batch = isinstance(extraction, list)
        prf_payload: dict[str, Any] = {}
        # Presence-only GT rows carry no extraction events; predicted clause
        # spans are presence candidates, not false positives. Keep P/R/F1/F2
        # explicitly null rather than reporting a misleading 0.0.
        has_extraction_events = any(
            not _gtm.is_empty_value(value)
            for exp in peeled_exp
            if isinstance(exp, dict)
            for value in exp.values()
        )
        if presence and not has_extraction_events:
            prf_payload = prf_bundle_keys({})
        elif is_batch and peeled_exp:
            rows = []
            for idx, (exp, pred, result) in enumerate(
                zip(peeled_exp, peeled_pred, extraction)
            ):
                if not isinstance(result, ExtractionScoreResult):
                    continue
                row_presence = (
                    presence[idx]
                    if isinstance(presence, list) and idx < len(presence)
                    else presence
                )
                # Presence-only rows are scored by the presence metric; their
                # predicted clause spans must not count as spurious field
                # fills (a zero-denominator row otherwise drags micro P/R/F1).
                if (
                    row_presence
                    and isinstance(exp, dict)
                    and not any(
                        not _gtm.is_empty_value(value) for value in exp.values()
                    )
                ):
                    continue
                row = _prf_one(exp, pred, result)
                if row:
                    rows.append(row)
            if rows:
                prf_payload = prf_bundle_keys(merge_extraction_counts(rows))
        elif isinstance(extraction, ExtractionScoreResult) and peeled_exp:
            one = _prf_one(peeled_exp[0], peeled_pred[0], extraction)
            if one:
                prf_payload = prf_bundle_keys(one)

        # Claims extras wrap the return (batch always; single-doc when the
        # caller asked for the full per-document payload via detailed=True).
        if (
            self.name == "insurance_claims_specialist"
            and peeled_exp
            and (is_batch or detailed)
        ):
            from .claims_consistency import score_claims_extras

            claim_rows = [
                score_claims_extras(exp, pred)
                for idx, (exp, pred) in enumerate(zip(peeled_exp, peeled_pred))
                if idx not in unscorable_rows
                and isinstance(exp, dict)
                and isinstance(pred, dict)
            ]
            if claim_rows:
                consist = [row["determination_consistency"] for row in claim_rows]
                amounts = [
                    row["amount_exactness"]
                    for row in claim_rows
                    if row["amount_exactness"] is not None
                ]
                extras["determination_consistency"] = (
                    round(sum(consist) / len(consist), 4) if consist else None
                )
                extras["amount_exactness"] = (
                    round(sum(amounts) / len(amounts), 4) if amounts else None
                )

        if not extras and not is_batch and not detailed:
            return extraction
        # Batch population counts (#16): unscorable rows are reported, never
        # folded into the quality mean.
        batch_quality: dict[str, Any] = {}
        if is_batch:
            batch_quality = aggregate_quality_honest(
                [
                    row
                    if isinstance(row, dict)
                    else {"overall_score": getattr(row, "overall_score", None)}
                    for row in extraction
                ]
            )
            batch_quality["n_missing_predictions"] = n_missing_predictions
        if not extras and is_batch:
            return _stamp(
                {
                    "extraction": extraction,
                    **prf_payload,
                    **format_scores,
                    **batch_quality,
                    **(
                        {"unlabeled_fields": [list(u) for u in unlabeled]}
                        if unlabeled
                        else {}
                    ),
                    "schema_promotion_gate": check_schema_promotion_gate(
                        format_scores.get("schema_valid"), doc_class=doc_class
                    ),
                    "metric_id": metric_id_for(
                        "extraction_overall_score", doc_class=doc_class
                    ),
                },
                metric_id_for("extraction_overall_score", doc_class=doc_class),
            )
        filtered = {
            k: v for k, v in extras.items()
            if k not in {"task", "kind", "topic", "sentiment", "per_question",
                         "per_document"}
        }
        mid = metric_id_for(
            "extraction_category_presence" if presence else "extraction_overall_score",
            doc_class=doc_class,
            presence_mode=bool(presence),
        )
        filtered["schema_promotion_gate"] = check_schema_promotion_gate(
            format_scores.get("schema_valid"), doc_class=doc_class
        )
        payload = {
            "extraction": extraction,
            **prf_payload,
            **filtered,
            **format_scores,
            **batch_quality,
            "detail": extras,
            "metric_id": mid,
        }
        if unlabeled:
            # Same contract as the no-extras batch branch above:
            # ``unlabeled_fields`` is per-row (list of lists, aligned with the
            # GT row order) whenever *is_batch*, and a flat field list only on
            # a single-document payload. A one-row batch therefore stays
            # ``[[...]]`` instead of collapsing to a bare field list.
            payload["unlabeled_fields"] = (
                [list(u) for u in unlabeled] if is_batch else list(unlabeled[0])
            )
        if isinstance(extraction, ExtractionScoreResult):
            # Single-document payload: flatten the document's own overall
            # score so consumers do not have to dig into the dataclass.
            payload["overall_score"] = extraction.overall_score
        return _stamp(payload, mid)

    def _score_audit(
        self,
        expected: Any,
        predicted: Any,
        *,
        doc_text: str | None,
        field_types: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Compare auditor output to specialist (or GT) output.

        ``expected`` is the reference (specialist or GT dict); ``predicted``
        is the auditor dict. Disagreement is the complement of the
        field-type-aware overall score — no new algorithm.
        """
        if not isinstance(expected, dict) or not isinstance(predicted, dict):
            raise TypeError(
                f"{self.name} audit suite.score() expects two field dicts "
                "(reference, auditor_output)"
            )
        ftypes = field_types or self.field_types
        if not ftypes:
            # Generic auditor / arbiter: treat every shared key as free_text.
            keys = set(expected) | set(predicted)
            ftypes = {k: "free_text" for k in keys if k != "confidence"}
        result = score_extraction(
            self.doc_type or "audit", ftypes, predicted, expected, doc_text=doc_text
        )
        overall = result.overall_score
        disagreement = None if overall is None else round(1.0 - overall, 4)
        return {
            "extraction": result,
            "audit_disagreement_rate": disagreement,
            "audit_resolution_rate": overall,
            "overall_score": overall,
        }

    def _score_transcription(self, expected: Any, predicted: Any) -> dict[str, Any]:
        """Combine exact-match and token-F1 scores with word and character ASR metrics."""
        from .asr import score_transcription
        from .classification import accuracy, exact_match

        asr = score_transcription(expected, predicted)
        if isinstance(expected, list) and isinstance(predicted, list):
            per_row = [exact_match(p, e) for e, p in zip(expected, predicted)]
            token_f1 = [
                float(score_field("free_text", p, e) or 0.0)
                for e, p in zip(expected, predicted)
            ]
            return {
                "accuracy": accuracy(expected, predicted),
                "f1_macro": round(sum(token_f1) / len(token_f1), 4) if token_f1 else 0.0,
                "wer": asr["wer"],
                "cer": asr["cer"],
                "word_accuracy": asr["word_accuracy"],
                "character_accuracy": asr["character_accuracy"],
                "n": len(per_row),
            }
        em = exact_match(predicted, expected)
        token_f1 = float(score_field("free_text", predicted, expected) or 0.0)
        return {
            "accuracy": em,
            "f1_macro": token_f1,
            "wer": asr["wer"],
            "cer": asr["cer"],
            "word_accuracy": asr["word_accuracy"],
            "character_accuracy": asr["character_accuracy"],
            "n": 1,
        }

    def _score_intake(
        self,
        expected: Any,
        predicted: Any,
        *,
        raw_text: str | None = None,
    ) -> dict[str, Any]:
        """Delegate intake scoring with optional raw source text."""
        from .intake import score_intake

        return score_intake(expected, predicted, raw_text=raw_text)

    def to_dict(self) -> dict[str, Any]:
        """Serialize suite configuration, metric names, and corpus metadata."""
        return {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "metrics_bundle": self.profile.metrics_bundle,
            "doc_type": self.doc_type,
            "task_key": self.task_key,
            "computable": self.computable,
            "ground_truth": self.profile.ground_truth,
            "metric_names": self.metric_names(),
            "headline_names": self.headline_names(),
            "honest_gap": self.honest_gap,
            "field_types": dict(self.field_types),
            "subclasses": list(self.subclasses),
            "differentiators": list(self.differentiators),
            "in_corpus": self.in_corpus,
            "retired": self.retired,
        }


def _suite_for_profile(profile: AgentProfile) -> ScoringSuite:
    """Build a suite with corpus-aligned fields and specialist or auditor metadata."""
    kind = _kind_for(profile)
    doc_type = SPECIALIST_DOC_TYPES.get(profile.name) or profile.doc_bundle
    # Per-specialist auditors inherit the specialist's field types so
    # audit.score(specialist_out, auditor_out) is type-aware.
    auditor_doc = None
    if profile.name.endswith("_auditor") and profile.name != "audit_agent":
        stem = profile.name[: -len("_auditor")]
        # contract_auditor → contracts_specialist; others are 1:1.
        specialist = {
            "contract": "contracts_specialist",
            "corporate_records": "corporate_records_specialist",
            "due_diligence": "due_diligence_specialist",
            "correspondence": "correspondence_specialist",
            "compliance": "compliance_specialist",
            "court_opinions": "court_opinions_specialist",
            "insurance_claims": "insurance_claims_specialist",
        }.get(stem)
        if specialist:
            auditor_doc = SPECIALIST_DOC_TYPES[specialist]
            doc_type = auditor_doc
    field_types: dict[str, str] = {}
    if doc_type and doc_type in DEFAULT_FIELD_TYPES:
        field_types = dict(DEFAULT_FIELD_TYPES[doc_type])
    from .corpus import (
        CORPUS_DIFFERENTIATORS,
        CORPUS_DOC_TYPES,
        CORPUS_EXTRACTION_FIELDS,
        DOC_TYPE_SUBCLASSES,
    )

    # Sorter / reviewer see the full merged catalog (no single doc_type).
    subclasses: tuple[str, ...] = ()
    differentiators: tuple[str, ...] = ()
    in_corpus = False
    if doc_type:
        subclasses = DOC_TYPE_SUBCLASSES.get(doc_type, ())
        differentiators = CORPUS_DIFFERENTIATORS.get(doc_type, ())
        in_corpus = doc_type in CORPUS_DOC_TYPES
        expected_fields = CORPUS_EXTRACTION_FIELDS.get(doc_type)
        if expected_fields and set(field_types) != set(expected_fields):
            # Prefer the corpus-aligned field set; keep scoring types from
            # DEFAULT_FIELD_TYPES and default unknown keys to name.
            aligned = {}
            for key in expected_fields:
                aligned[key] = field_types.get(key) or "name"
            field_types = aligned
    elif profile.name in ("sorter", "sorter_reviewer"):
        in_corpus = True
    from .mailroom import RETIRED_AUDITORS, RETIRED_SPECIALISTS

    retired = profile.name in RETIRED_SPECIALISTS or profile.name in RETIRED_AUDITORS
    return ScoringSuite(
        name=profile.name,
        title=profile.title,
        kind=kind,
        profile=profile,
        doc_type=doc_type,
        field_types=field_types,
        extra_metrics=_AGENT_EXTRAS.get(profile.name, ()),
        honest_gap=_HONEST_GAPS.get(profile.name),
        task_key=_task_key_for(profile, kind),
        subclasses=subclasses,
        differentiators=differentiators,
        in_corpus=in_corpus,
        retired=retired,
    )


DEFAULT_SUITES: dict[str, ScoringSuite] = {
    name: _suite_for_profile(profile) for name, profile in DEFAULT_PROFILES.items()
}


def _resolve_suite_name(name: str) -> str:
    """Resolve agent names and document aliases, raising KeyError for unknown suites."""
    if name in DEFAULT_SUITES:
        return name
    if name.startswith("agent:"):
        stripped = name[len("agent:") :]
        if stripped in DEFAULT_SUITES:
            return stripped
    if name.startswith("doc:"):
        name = name[len("doc:") :]
    if name in DOC_TYPE_ALIASES:
        return DOC_TYPE_ALIASES[name]
    raise KeyError(
        f"unknown scoring suite {name!r}; known agents: {sorted(DEFAULT_SUITES)}; "
        f"doc-type aliases: {sorted(DOC_TYPE_ALIASES)}"
    )


def _requested_doc_type(name: str) -> str | None:
    """Return the mailroom class if *name* is a doc-type alias (incl. ``doc:``)."""
    key = name[4:] if name.startswith("doc:") else name
    return key if key in DOC_TYPE_ALIASES else None


def _rebind_for_doc_type(suite: ScoringSuite, doc_type: str) -> ScoringSuite:
    """Keep the specialist profile but bind this document class's catalogs.

    ``merger_agreement`` is ``MergerAgreementExtraction`` (no CUAD family
    or ``cuad_clauses``). Historical callers that rebound the contracts
    specialist onto the merger class pick up the MAUD catalog here.
    """
    from dataclasses import replace

    from .corpus import (
        CORPUS_DIFFERENTIATORS,
        CORPUS_DOC_TYPES,
        CORPUS_EXTRACTION_FIELDS,
        DOC_TYPE_SUBCLASSES,
    )

    field_types = dict(DEFAULT_FIELD_TYPES.get(doc_type, suite.field_types))
    expected_fields = CORPUS_EXTRACTION_FIELDS.get(doc_type)
    if expected_fields and set(field_types) != set(expected_fields):
        field_types = {key: field_types.get(key) or "name" for key in expected_fields}
    honest = suite.honest_gap
    extras = suite.extra_metrics
    if doc_type == "merger_agreement":
        honest = None
        extras = tuple(dict.fromkeys((*suite.extra_metrics, *_MERGER_EXTRAS)))
    return replace(
        suite,
        doc_type=doc_type,
        field_types=field_types,
        extra_metrics=extras,
        subclasses=DOC_TYPE_SUBCLASSES.get(doc_type, ()),
        differentiators=CORPUS_DIFFERENTIATORS.get(doc_type, ()),
        in_corpus=doc_type in CORPUS_DOC_TYPES,
        honest_gap=honest,
    )


def get_suite(name: str) -> ScoringSuite:
    """Return the dedicated suite for an agent or document type.

    Accepts profile names (``sorter``), ``agent:`` prefixes, bare doc
    types (``insurance_claim``), and ``doc:`` prefixes.

    Doc-type aliases that share a specialist but have their own subclass
    catalog are rebound so ``suite.doc_type``, ``suite.subclasses``, and
    ``suite.differentiators`` match the requested class — not the
    specialist's native class.
    """
    suite = DEFAULT_SUITES[_resolve_suite_name(name)]
    requested = _requested_doc_type(name)
    if requested and requested != suite.doc_type:
        return _rebind_for_doc_type(suite, requested)
    return suite


def list_suites(*, kind: str | None = None, live_only: bool = False) -> list[str]:
    """Sorted suite names, optionally filtered by kind and live roster."""
    names = sorted(DEFAULT_SUITES)
    if kind is not None:
        names = [n for n in names if DEFAULT_SUITES[n].kind == kind]
    if live_only:
        names = [n for n in names if not DEFAULT_SUITES[n].retired]
    return names


def suite_for_doc_type(doc_type: str) -> ScoringSuite:
    """Specialist suite for a mailroom document class."""
    try:
        return get_suite(doc_type)
    except KeyError as exc:
        raise KeyError(
            f"unknown doc type {doc_type!r}; known: {sorted(DOC_TYPE_ALIASES)}"
        ) from exc


def score_suite(
    name: str,
    expected: Any,
    predicted: Any,
    **kwargs: Any,
) -> Any:
    """Convenience: ``get_suite(name).score(expected, predicted, **kwargs)``."""
    return get_suite(name).score(expected, predicted, **kwargs)


# Touch get_profile so a typo in DEFAULT_PROFILES fails at import.
for _name in DEFAULT_SUITES:
    get_profile(_name)
