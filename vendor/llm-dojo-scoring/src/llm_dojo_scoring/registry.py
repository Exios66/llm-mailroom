"""Metric definitions registry — the single source mapping every score name
to its tier, units, aggregation, and the agents that consume it.

The registry is ORGANIZATIONAL, not computational: every metric here maps to a
function that already exists in this package (see ``source``), or — for
emitter-only mailroom aliases — to a name the pipeline emits. T0/T1 entries
carry ``citation``, ``inclusion``, and ``ground_truth`` (merged from
:mod:`llm_dojo_scoring.metric_meta` when the YAML omits them).
No calculation logic lives in this module.

Tiers (the dashboard discipline — everything below T1 is opt-in exploration):

- **T0 HEADLINE** — board-level. ONE number per agent on the default view.
- **T1 CORE** — "what broke yesterday?" diagnostics (P/R/F1/F2, rates, cost).
- **T2 DEEP** — root-cause investigation (confusion matrices, failure modes,
  bootstrap CIs, calibration).
- **T3 LOG** — audit trail / regression comparison only; never on dashboards.

Resolution order for a custom registry file:

1. explicit ``path`` argument to :func:`load_registry`
2. ``LLM_DOJO_SCORING_REGISTRY`` environment variable
3. the built-in :data:`DEFAULT_METRICS_YAML` (always present, always valid)

The built-in YAML embeds the full current surface: the classification/task
metrics from this package, the 37 flat Langfuse score-config names from
llm-mailroom's ``observability/scores.py`` (so consolidation is lossless),
and the two new audit-agent metrics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Iterable

import yaml

from .metric_meta import ALLOWED_GROUND_TRUTH, METRIC_META

__all__ = [
    "MetricTier",
    "MetricDef",
    "Registry",
    "DEFAULT_METRICS_YAML",
    "SPECIALIST_AGENTS",
    "LIVE_SPECIALIST_AGENTS",
    "AUDITOR_AGENTS",
    "CLASSIFIER_AGENTS",
    "TRANSCRIBER_AGENTS",
    "INTAKE_AGENTS",
    "SERVING_AGENTS",
    "expand_agent_families",
    "load_registry",
    "get_registry",
    "clear_registry_cache",
    "ALLOWED_GROUND_TRUTH",
]

# Canonical pipeline roster. Family tokens in ``applicable_agents``
# (``SPECIALISTS``, ``AUDITORS``, ``CLASSIFIERS``, ``TRANSCRIBERS``) expand
# to these tuples so a newly added specialist cannot be omitted from the
# extraction metric surface (the v0.7.0 ``insurance_claims_specialist`` gap).
# All specialists the registry still scores (live + retired historical).
SPECIALIST_AGENTS: tuple[str, ...] = (
    "contracts_specialist",
    "merger_agreement_specialist",
    "corporate_records_specialist",
    "due_diligence_specialist",
    "correspondence_specialist",
    "compliance_specialist",
    "court_opinions_specialist",
    "insurance_claims_specialist",
)
#: Live llm-mailroom extraction roster (mailroom-issues #236–#238).
#: Retired specialists stay in SPECIALIST_AGENTS so historical traces
#: still validate.
LIVE_SPECIALIST_AGENTS: tuple[str, ...] = (
    "contracts_specialist",
    "merger_agreement_specialist",
    "corporate_records_specialist",
    "correspondence_specialist",
    "insurance_claims_specialist",
)

AUDITOR_AGENTS: tuple[str, ...] = (
    "audit_agent",
    "contract_auditor",
    "corporate_records_auditor",
    "due_diligence_auditor",
    "correspondence_auditor",
    "compliance_auditor",
    "court_opinions_auditor",
    "insurance_claims_auditor",
    "arbiter",
)

CLASSIFIER_AGENTS: tuple[str, ...] = (
    "sorter",
    "sorter_reviewer",
    "judge",
)

TRANSCRIBER_AGENTS: tuple[str, ...] = (
    "pdf_transcriber",
    "image_extractor",
)

INTAKE_AGENTS: tuple[str, ...] = (
    "intake",
)

SERVING_AGENTS: tuple[str, ...] = (
    "local_vs_api",
)

_AGENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "SPECIALISTS": SPECIALIST_AGENTS,
    "LIVE_SPECIALISTS": LIVE_SPECIALIST_AGENTS,
    "AUDITORS": AUDITOR_AGENTS,
    "CLASSIFIERS": CLASSIFIER_AGENTS,
    "TRANSCRIBERS": TRANSCRIBER_AGENTS,
    "INTAKE": INTAKE_AGENTS,
    "SERVING": SERVING_AGENTS,
}


def expand_agent_families(agents: Iterable[str]) -> tuple[str, ...]:
    """Expand roster family tokens; unknown names pass through unchanged."""
    out: list[str] = []
    seen: set[str] = set()
    for agent in agents:
        members = _AGENT_FAMILIES.get(agent, (agent,))
        for member in members:
            if member not in seen:
                seen.add(member)
                out.append(member)
    return tuple(out)

_ENV_VAR = "LLM_DOJO_SCORING_REGISTRY"


class MetricTier(IntEnum):
    """Dashboard tier. Lower = more prominent."""

    HEADLINE = 0
    CORE = 1
    DEEP = 2
    LOG = 3


_TIER_NAMES = {
    "headline": MetricTier.HEADLINE,
    "core": MetricTier.CORE,
    "deep": MetricTier.DEEP,
    "log": MetricTier.LOG,
}


@dataclass(frozen=True)
class MetricDef:
    """One metric definition. Pure metadata — no behavior."""

    name: str
    tier: MetricTier
    units: str = "float[0,1]"
    description: str = ""
    #: Agents that consume the metric; ``["ALL"]`` means every agent.
    applicable_agents: tuple[str, ...] = ("ALL",)
    #: How per-document values roll up to run level.
    aggregation: str = "mean"
    #: Dotted path of the existing function that computes it, if any.
    source: str | None = None
    #: Migration/pruning notes (aliases, consolidations, promotions).
    notes: str = ""
    #: Method / paper the implemented scorer follows. Empty on custom YAML.
    citation: str = ""
    #: When the metric is computed vs skipped / ``None``.
    inclusion: str = ""
    #: ``required`` | ``optional`` | ``structural`` | ``none`` (empty = unset).
    ground_truth: str = ""

    def applies_to(self, agent: str) -> bool:
        """Check whether the metric targets this agent or all agents."""
        return "ALL" in self.applicable_agents or agent in self.applicable_agents


@dataclass
class Registry:
    """A loaded set of metric definitions with tier/agent filtering."""

    metrics: dict[str, MetricDef] = field(default_factory=dict)

    # -- lookups -------------------------------------------------------------

    def get(self, name: str) -> MetricDef:
        """Look up a metric, listing known names when the name is unknown."""
        try:
            return self.metrics[name]
        except KeyError:
            raise KeyError(
                f"unknown metric {name!r}; known: {sorted(self.metrics)}"
            ) from None

    def names(self) -> list[str]:
        """Return all registered metric names in sorted order."""
        return sorted(self.metrics)

    # -- filtering -----------------------------------------------------------

    def filter(
        self,
        *,
        max_tier: int | MetricTier | None = None,
        tier: int | MetricTier | None = None,
        agent: str | None = None,
    ) -> list[MetricDef]:
        """Metrics matching the filters, ordered by tier then name.

        ``max_tier=1`` (the common dashboard query) returns T0+T1 only;
        ``agent="sorter"`` keeps metrics whose ``applicable_agents`` include
        the agent (or declare ``ALL``).
        """
        out: list[MetricDef] = []
        for m in self.metrics.values():
            if tier is not None and m.tier != MetricTier(tier):
                continue
            if max_tier is not None and m.tier > MetricTier(max_tier):
                continue
            if agent is not None and not m.applies_to(agent):
                continue
            out.append(m)
        return sorted(out, key=lambda m: (m.tier, m.name))

    def names_for(self, *, max_tier: int | None = None, agent: str | None = None) -> list[str]:
        """Return names of metrics matching the optional tier and agent filters."""
        return [m.name for m in self.filter(max_tier=max_tier, agent=agent)]

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Registry":
        """Load metric definitions from a UTF-8 YAML registry file."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        """Build a registry, normalizing tiers and agents and validating GT labels."""
        reg = cls()
        for name, spec in (data.get("metrics") or {}).items():
            spec = dict(spec or {})
            tier_raw = str(spec.get("tier", 1)).strip().lower()
            tier = _TIER_NAMES.get(tier_raw, None)
            if tier is None:
                tier = MetricTier(int(tier_raw))
            agents = spec.get("applicable_agents") or ["ALL"]
            if isinstance(agents, str):
                agents = [agents]
            agents = expand_agent_families(agents)
            meta = METRIC_META.get(name, {})
            citation = str(spec.get("citation") or meta.get("citation") or "")
            inclusion = str(spec.get("inclusion") or meta.get("inclusion") or "")
            ground_truth = str(
                spec.get("ground_truth") or meta.get("ground_truth") or ""
            )
            if ground_truth not in ALLOWED_GROUND_TRUTH:
                raise ValueError(
                    f"metric {name!r} ground_truth={ground_truth!r}; "
                    f"allowed: {sorted(ALLOWED_GROUND_TRUTH - {''})}"
                )
            source = spec.get("source")
            if source is None and name in (
                "audit_disagreement_rate",
                "audit_resolution_rate",
            ):
                source = "suites.ScoringSuite._score_audit"
            reg.metrics[name] = MetricDef(
                name=name,
                tier=tier,
                units=str(spec.get("units", "float[0,1]")),
                description=str(spec.get("description", "")),
                applicable_agents=agents,
                aggregation=str(spec.get("aggregation", "mean")),
                source=source,
                notes=str(spec.get("notes", "")),
                citation=citation,
                inclusion=inclusion,
                ground_truth=ground_truth,
            )
        return reg


# ---------------------------------------------------------------------------
# Built-in default registry
# ---------------------------------------------------------------------------

DEFAULT_METRICS_YAML = """
# llm-dojo-scoring default metric registry (KANBAN-061).
# Every entry maps to an EXISTING package function (source:) or is an
# emitter-level definition (audit metrics). Aliases of the 37 flat
# llm-mailroom SCORE_CONFIGS names plus Langfuse transport aliases
# (extraction_verified_precision) and The-Mailroom judge scores.
#
# Optional keys (empty-string default so older custom YAML still loads):
#   citation      — method/paper the implemented scorer follows
#   inclusion     — when the metric is computed vs skipped / None
#   ground_truth  — required | optional | structural | none
# T0/T1 defaults are filled from llm_dojo_scoring.metric_meta when omitted.

metrics:
  # ===================== T0 — HEADLINE =====================
  f1_macro:
    tier: 0
    description: "Macro-averaged F1 across classes — the universal classifier headline"
    applicable_agents: [ALL]
    aggregation: mean
    source: "classification.macro_prf"
    citation: "Unweighted macro-average of one-vs-rest F1 (classification.macro_prf); F1 is van Rijsbergen Fβ with β=1."
    inclusion: "Computed when expected and predicted label sequences are non-empty. Empty/null labels dropped. score_task drops ERROR_PREFIX rows."
    ground_truth: required
  accuracy:
    tier: 0
    description: "Overall exact-match accuracy"
    applicable_agents: [ALL]
    aggregation: mean
    source: "classification.accuracy"
    citation: "Exact-match accuracy after task-aware normalize_label (classification.accuracy)."
    inclusion: "Computed when expected and predicted label sequences are non-empty. Empty/null labels dropped."
    ground_truth: required
  extraction_overall_score:
    tier: 0
    description: "Specialist headline: overall extraction score for the run"
    applicable_agents: [SPECIALISTS, reporter, boss]
    aggregation: mean
    source: "field_scoring.score_extraction"
    citation: "Mean of per-field typed scores from field_scoring.score_extraction (soft mean; partial list credit stays here)."
    inclusion: "None when there are no scorable fields. Empty/null expected dict yields no overall score."
    ground_truth: required
  extraction_f1:
    tier: 0
    description: "Field-micro F1 over (field, value) events (ACE / CoNLL / SemEval slot filling)"
    applicable_agents: [SPECIALISTS]
    aggregation: mean
    source: "extraction_metrics.extraction_binary_metrics"
    citation: "ACE / CoNLL / SemEval slot filling; TP requires typed score ≥ 1.0. F1 is van Rijsbergen β=1."
    inclusion: "Skipped when expected is empty/null. Empty GT field values are not FN. List F1 is None if no list fields."
    ground_truth: required
  extraction_f2:
    tier: 0
    description: "Field-micro F2 (β=2, van Rijsbergen) — recall-weighted; insurance claims board number"
    applicable_agents: [SPECIALISTS]
    aggregation: mean
    source: "extraction_metrics.extraction_binary_metrics"
    notes: "Partial list matches are not TP; they stay in extraction_overall_score"
    citation: "ACE / CoNLL / SemEval slot filling; F2 is van Rijsbergen Fβ with β=2 (5PR/(4P+R))."
    inclusion: "Same inclusion as extraction_f1. Partial list matches are not TP."
    ground_truth: required

  # ===================== T1 — CORE =====================
  precision:
    tier: 1
    description: "Precision (TP / (TP + FP))"
    applicable_agents: [ALL]
    source: "classification.binary_metrics"
  recall:
    tier: 1
    description: "Recall (TP / (TP + FN))"
    applicable_agents: [ALL]
    source: "classification.binary_metrics"
  f2:
    tier: 1
    description: "F-beta beta=2 — recall-weighted; flags false negatives early (legal work)"
    applicable_agents: [ALL]
    source: "classification.binary_metrics"
  false_positive_rate:
    tier: 1
    description: "FP / (FP + TN)"
    applicable_agents: [ALL]
    source: "classification.binary_metrics"
  false_negative_rate:
    tier: 1
    description: "FN / (FN + TP)"
    applicable_agents: [ALL]
    source: "classification.binary_metrics"
  jaccard_similarity:
    tier: 1
    description: "Token-set Jaccard over positive spans (ContractEval method)"
    applicable_agents: [contracts_specialist, court_opinions_specialist, judge]
    source: "tasks.get_jaccard"
    notes: "Promoted to T1 per proposal section 3.1 (section 2.2 draft had T2); KANBAN-054 made ContractEval KPIs core."
  contracteval_false_no_related:
    tier: 1
    description: "Rate of 'no related clause' answers when ground truth expects content"
    applicable_agents: [contracts_specialist, judge]
    source: "tasks.contracteval_metrics"
    notes: "alias: laziness (KANBAN-054); mirrors ContractEval no_related_rate"
  laziness_rate:
    tier: 1
    description: "Laziness detector — empty/bail responses when content is expected"
    applicable_agents: [contracts_specialist, judge]
    source: "tasks.said_no_related"
    notes: "alias of contracteval_false_no_related at record level"
  field_presence:
    tier: 1
    description: "Share of expected fields populated by the model"
    applicable_agents: [SPECIALISTS]
    source: "field_scoring.score_extraction"
    notes: "mailroom alias: expected_field_presence. HONEST GAP: score_extraction does not emit this name as of 0.11.0."
    citation: "ACE-style expected-field presence. Registry source points at score_extraction, which does not emit this name."
    inclusion: "Not computed in this package as of 0.11.0 — honesty gap, not a scorer. Do not treat a missing key as 0.0."
    ground_truth: required
  entity_list_precision:
    tier: 1
    description: "Precision over extracted list items (bipartite match)"
    applicable_agents: [SPECIALISTS]
    source: "field_scoring.score_entity_list"
  entity_list_recall:
    tier: 1
    description: "Recall over extracted list items (bipartite match)"
    applicable_agents: [SPECIALISTS]
    source: "field_scoring.score_entity_list"
  entity_list_f1:
    tier: 1
    description: "Mean entity-list bipartite F1 (dashboard name for diagnostics.entity_list_raw_f1)"
    applicable_agents: [SPECIALISTS]
    source: "extraction_metrics.mean_entity_list_f1"
    notes: "existing diagnostics.entity_list_raw_f1 registered under this name"
  extraction_precision:
    tier: 1
    description: "Field-micro precision over (field, value) extraction events"
    applicable_agents: [SPECIALISTS]
    source: "extraction_metrics.extraction_binary_metrics"
  extraction_recall:
    tier: 1
    description: "Field-micro recall over (field, value) extraction events"
    applicable_agents: [SPECIALISTS]
    source: "extraction_metrics.extraction_binary_metrics"
  determination_consistency:
    tier: 1
    description: "Insurance coverage_determination agrees with denial_reasons (approved ⇒ empty; denied/partial ⇒ non-empty)"
    applicable_agents: [insurance_claims_specialist]
    source: "claims_consistency.determination_consistency"
    citation: "Structural check on the prediction; ground truth is unused (claims_consistency.determination_consistency)."
    inclusion: "Always defined on a predicted dict; 0.0 when determination is missing. CMS GT homogeneity makes GT-shaped predictions degenerate (always 1.0)."
    ground_truth: structural
  amount_exactness:
    tier: 1
    description: "Claimed-amount exact match after money normalize (complement of money_mae_usd)"
    applicable_agents: [insurance_claims_specialist]
    source: "claims_consistency.amount_exactness"
    citation: "Money-field exact after one-cent normalize (claims_consistency.amount_exactness)."
    inclusion: "None if either side is empty or unparseable."
    ground_truth: required
  precision_macro:
    tier: 1
    description: "Unweighted mean of one-vs-rest precision (doc_type)"
    applicable_agents: [CLASSIFIERS]
    source: "classification.macro_prf"
  recall_macro:
    tier: 1
    description: "Unweighted mean of one-vs-rest recall (doc_type)"
    applicable_agents: [CLASSIFIERS]
    source: "classification.macro_prf"
  f2_macro:
    tier: 1
    description: "Unweighted mean of one-vs-rest F2 (doc_type)"
    applicable_agents: [CLASSIFIERS]
    source: "classification.macro_prf"
  subclass_f1_macro:
    tier: 1
    description: "Macro-F1 over doc subclasses (CUAD family / CMS table / Enron form / …)"
    applicable_agents: [CLASSIFIERS]
    source: "tasks.score_task"
  subclass_precision_macro:
    tier: 1
    description: "Macro precision over doc subclasses"
    applicable_agents: [CLASSIFIERS]
    source: "tasks.score_task"
  subclass_recall_macro:
    tier: 1
    description: "Macro recall over doc subclasses"
    applicable_agents: [CLASSIFIERS]
    source: "tasks.score_task"
  subclass_f2_macro:
    tier: 1
    description: "Macro F2 over doc subclasses"
    applicable_agents: [CLASSIFIERS]
    source: "tasks.score_task"
  verified_precision:
    tier: 1
    description: "Precision restricted to doc-verifiable items"
    applicable_agents: [SPECIALISTS, AUDITORS]
    source: "field_scoring.audit_list_field"
    notes: "mailroom alias: extraction_overall_verified_precision"
  schema_valid:
    tier: 1
    description: "Output parsed to the expected schema (T1 headline gate — v0.18)"
    applicable_agents: [ALL]
    source: "scorecard_honesty.score_format_layer"
    ground_truth: structural
  parse_ok:
    tier: 1
    description: "Prediction parsed as JSON / structured output without prose wrapper"
    applicable_agents: [ALL]
    source: "scorecard_honesty.score_format_layer"
    ground_truth: structural
  schema_adherence:
    tier: 1
    description: "Required schema keys present after parse (alias of schema_valid when keys match)"
    applicable_agents: [ALL]
    source: "scorecard_honesty.score_format_layer"
    ground_truth: structural
  parse_error:
    tier: 1
    description: "Output failed to parse (quick health check — promoted per pruning plan)"
    applicable_agents: [ALL]
    notes: "mailroom SCORE_CONFIGS name preserved; not computed in this package"
    source: null
    ground_truth: none
  success_rate:
    tier: 1
    description: "Runs completing without abort/stage failure"
    applicable_agents: [ALL]
    notes: "consolidates mailroom stage_completed + run_aborted; not computed in this package"
    source: null
    ground_truth: none
  completeness:
    tier: 1
    description: "Numeric completeness of the required output shape"
    applicable_agents: [ALL]
    notes: "mailroom completeness_label (CATEGORICAL) folds into this numeric score; not computed in this package"
    source: null
    ground_truth: none
  classification_correct:
    tier: 1
    description: "Per-document classification correctness (strict/equiv)"
    applicable_agents: [CLASSIFIERS, boss]
    source: "classification.exact_match"
  class_correct:
    tier: 1
    description: "Per-document class correctness (mailroom pipeline pilot)"
    applicable_agents: [ALL]
    notes: "mailroom SCORE_CONFIGS name; alias of classification_correct"
  stage_correct:
    tier: 1
    description: "Per-stage correctness (mailroom pipeline pilot)"
    applicable_agents: [ALL]
    notes: "mailroom SCORE_CONFIGS name"
  extraction_correctness:
    tier: 1
    description: "Per-document extraction correctness (mailroom pilot)"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name"
  extraction_needs_judge_review:
    tier: 1
    description: "Routing signal: extraction ambiguous enough to escalate to the judge"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name"
  expected_field_presence:
    tier: 1
    description: "Share of expected fields populated"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; alias of field_presence"
  extraction_overall_verified_precision:
    tier: 1
    description: "Precision restricted to doc-verifiable items"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; alias of verified_precision"
  extraction_verified_precision:
    tier: 1
    description: "Langfuse wire alias of extraction_overall_verified_precision (35-char config limit)"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom LANGFUSE_SCORE_NAME_ALIASES transport name"
  mailroom-pipeline-judge:
    tier: 1
    description: "The-Mailroom LLM-as-judge verdict (CORRECT / PARTIAL / MISS)"
    applicable_agents: [judge]
    notes: "The-Mailroom JUDGE_VERDICT_SCORES; CATEGORICAL"
  mailroom-pipeline-quality:
    tier: 1
    description: "The-Mailroom LLM-as-judge quality (0..1)"
    applicable_agents: [judge]
    notes: "The-Mailroom JUDGE_QUALITY_SCORES; NUMERIC"
  exact_accuracy:
    tier: 1
    description: "HF pipeline exact doc-type accuracy (merger_agreement ≠ contract)"
    applicable_agents: [CLASSIFIERS]
    source: "mailroom.score_aligned_classification"
  aligned_accuracy:
    tier: 1
    description: "HF pipeline aligned doc-type accuracy (extract aliases only; merger_agreement ≠ contract)"
    applicable_agents: [CLASSIFIERS]
    source: "mailroom.score_aligned_classification"
  subclass_accuracy:
    tier: 1
    description: "HF pipeline subclass accuracy (CUAD family / CMS table / Enron form / …)"
    applicable_agents: [CLASSIFIERS]
    source: "tasks.score_task"
  extraction_hallucination_rate:
    tier: 1
    description: "Share of reported values not grounded in GT or the source doc"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; complement of verified precision"
  completeness_label:
    tier: 2
    description: "Categorical completeness label (complete/partial/incomplete)"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; label form of completeness"
  extraction_correctness_label:
    tier: 2
    description: "Categorical correctness label (accurate/partial/inaccurate)"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; label form of extraction_correctness"
  estimated_cost_usd:
    tier: 1
    description: "Estimated USD cost of the call/run"
    applicable_agents: [ALL]
    units: USD
    aggregation: sum
    source: "cost.estimate_cost"
  cost_per_document:
    tier: 1
    description: "Total cost / documents processed"
    applicable_agents: [ALL]
    units: USD
    aggregation: mean
    source: "cost.estimate_for_record"
  audit_disagreement_rate:
    tier: 1
    description: "Rate where the audit pass disagrees with the specialist output"
    applicable_agents: [AUDITORS]
    source: "suites.ScoringSuite._score_audit"
    notes: "NEW (KANBAN-061) — feeds KANBAN-060's contracts_audit_v0 pass; shared by every named auditor + arbiter"
  audit_resolution_rate:
    tier: 1
    description: "Rate where the specialist adopts the audit pass correction"
    applicable_agents: [AUDITORS]
    source: "suites.ScoringSuite._score_audit"
    notes: "NEW (KANBAN-061); shared by every named auditor + arbiter"
  legalbench_accuracy:
    tier: 1
    description: "LegalBench binary accuracy"
    applicable_agents: [court_opinions_specialist]
    source: "tasks.legalbench_score"
    notes: "part of the legalbench_* cluster — one bundle entry, sub-fields"
  legalbench_macro_f1:
    tier: 1
    description: "LegalBench macro F1"
    applicable_agents: [court_opinions_specialist]
    source: "tasks.legalbench_score"
    notes: "legalbench_* cluster"
  date_mae_days:
    tier: 1
    units: days
    description: "Mean absolute date error in days (run-level diagnostic)"
    applicable_agents: [SPECIALISTS]
    source: "diagnostics.extraction_diagnostics"
    notes: "Existing diagnostics surface; registered so every specialist suite can emit it"
  money_mae_usd:
    tier: 1
    units: USD
    description: "Mean absolute money-field error in USD (run-level diagnostic)"
    applicable_agents: [SPECIALISTS]
    source: "diagnostics.extraction_diagnostics"
    notes: "Existing diagnostics surface; registered so money-bearing specialist suites can emit it"
  duration_mae_days:
    tier: 2
    units: days
    description: "Mean absolute duration-field error in days"
    applicable_agents: [SPECIALISTS]
    source: "diagnostics.extraction_diagnostics"

  # ===================== T1 — CONTENT / ASR =====================
  content_topic_accuracy:
    tier: 1
    description: "Enron correspondence content_topic exact-match accuracy (11 topics)"
    applicable_agents: [correspondence_specialist]
    source: "content_scoring.score_content_topic"
  content_topic_f1_macro:
    tier: 0
    description: "Enron correspondence content_topic macro-F1 over expected topics (imbalanced 11-way)"
    applicable_agents: [correspondence_specialist]
    source: "content_scoring.score_content_topic"
    notes: "Promoted to T0 — topic imbalance makes macro-F1 the correspondence board number"
    citation: "Enron 11-topic catalog; unweighted macro-F1 (content_scoring.score_content_topic)."
    inclusion: "Computed when a content_topic gold label is present; skipped when the field is empty/null."
    ground_truth: required
  sentiment_accuracy:
    tier: 1
    description: "Enron correspondence sentiment_label accuracy (negative/neutral/positive)"
    applicable_agents: [correspondence_specialist]
    source: "content_scoring.score_sentiment"
  sentiment_f1_macro:
    tier: 1
    description: "Enron correspondence sentiment_label macro-F1"
    applicable_agents: [correspondence_specialist]
    source: "content_scoring.score_sentiment"
  maud_question_accuracy:
    tier: 1
    description: "MAUD per-question micro exact-answer accuracy over the 22 Hub keys"
    applicable_agents: [contracts_specialist, merger_agreement_specialist]
    source: "content_scoring.score_maud_extraction"
  maud_question_macro_accuracy:
    tier: 1
    description: "MAUD per-question macro accuracy (unweighted mean over questions)"
    applicable_agents: [contracts_specialist, merger_agreement_specialist]
    source: "content_scoring.score_maud_extraction"
  maud_clause_presence:
    tier: 1
    description: "Share of expected MAUD questions present in the prediction"
    applicable_agents: [contracts_specialist, merger_agreement_specialist]
    source: "content_scoring.score_maud_extraction"
  maud_valid_class_rate:
    tier: 1
    description: "Share of predicted MAUD answers in the question's known class set"
    applicable_agents: [contracts_specialist, merger_agreement_specialist]
    source: "content_scoring.score_maud_extraction"
  maud_category_accuracy:
    tier: 2
    description: "MAUD clause-category exact match when both sides have a category"
    applicable_agents: [contracts_specialist, merger_agreement_specialist]
    source: "content_scoring.score_maud_extraction"
  wer:
    tier: 1
    units: error_rate
    description: "Word error rate (word-level Levenshtein / |reference|); lower is better"
    applicable_agents: [TRANSCRIBERS]
    source: "asr.word_error_rate"
    notes: "WER may exceed 1.0 when the hypothesis is longer than the reference"
  cer:
    tier: 1
    units: error_rate
    description: "Character error rate (character-level Levenshtein / |reference|); lower is better"
    applicable_agents: [TRANSCRIBERS]
    source: "asr.character_error_rate"
  word_accuracy:
    tier: 1
    description: "max(0, 1 - WER) complementary transcription headline"
    applicable_agents: [TRANSCRIBERS]
    source: "asr.word_accuracy"
  character_accuracy:
    tier: 2
    description: "max(0, 1 - CER) complementary character-level transcription accuracy"
    applicable_agents: [TRANSCRIBERS]
    source: "asr.character_accuracy"
  intake_prep_completeness:
    tier: 1
    description: "Share of intake prep-step invariants that hold before sorter handoff"
    applicable_agents: [INTAKE]
    source: "intake.intake_prep_completeness"
    notes: "NFC, newline unify, NBSP, zero-width, C0, hyphen unwrap, blank-run collapse, horizontal space, trim"
  intake_changed_rate:
    tier: 1
    description: "Share of documents whose intake clerk mutated the transcribed text"
    applicable_agents: [INTAKE]
    source: "intake.score_intake"
  intake_messy_rate:
    tier: 1
    description: "Share of documents flagged looks_messy after intake (OCR residue / wrap artifacts)"
    applicable_agents: [INTAKE]
    source: "intake.looks_messy"
  intake_hyphen_unwraps:
    tier: 2
    units: count
    description: "Hyphen-wrap unwraps performed (line-broken words rejoined)"
    applicable_agents: [INTAKE]
    aggregation: mean
    source: "intake.deterministic_normalize"
  intake_collapsed_blanks:
    tier: 2
    units: count
    description: "Collapsed 3+ blank-line runs (paragraph-break normalize)"
    applicable_agents: [INTAKE]
    aggregation: mean
    source: "intake.deterministic_normalize"

  # ===================== T0/T1 — LOCAL VS API SERVING =====================
  ttft_seconds:
    tier: 0
    units: seconds
    description: "Time to first token (streaming first-token timestamp − request start)"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  tokens_per_second:
    tier: 0
    units: tokens/s
    description: "Decode throughput: completion_tokens / e2e latency"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  tpot_seconds:
    tier: 1
    units: seconds
    description: "Time per output token after first token ((e2e − ttft) / (completion_tokens − 1))"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  e2e_latency_seconds:
    tier: 1
    units: seconds
    description: "End-to-end request wall-clock (start → last token)"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  ttft_p50:
    tier: 1
    units: seconds
    description: "Median time to first token over requests"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.aggregate_serving"
  ttft_p95:
    tier: 1
    units: seconds
    description: "95th-percentile time to first token over requests"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.aggregate_serving"
  e2e_p50:
    tier: 1
    units: seconds
    description: "Median end-to-end latency over requests"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.aggregate_serving"
  e2e_p95:
    tier: 1
    units: seconds
    description: "95th-percentile end-to-end latency over requests"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.aggregate_serving"
  output_tokens_per_second:
    tier: 1
    units: tokens/s
    description: "completion_tokens / (e2e − ttft) — decode-only throughput"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  prompt_tokens_per_second:
    tier: 1
    units: tokens/s
    description: "prompt_tokens / ttft — prefill throughput when TTFT is recorded"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  requests_per_second:
    tier: 1
    units: req/s
    description: "n_requests / summed e2e window (sequential throughput)"
    applicable_agents: [SERVING]
    source: "serving.aggregate_serving"
  docs_per_second:
    tier: 1
    units: docs/s
    description: "n_docs / summed e2e window (documents processed per second)"
    applicable_agents: [SERVING]
    source: "serving.aggregate_serving"
  gpu_utilization:
    tier: 1
    description: "Local GPU SM utilization in [0,1] (nvidia-smi / vLLM). None on API-key runs"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  kv_cache_utilization:
    tier: 1
    description: "vLLM KV-cache / prefix-cache occupancy in [0,1]. None on API-key runs"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  gpu_memory_used_gb:
    tier: 1
    units: GB
    description: "Local GPU memory used in GiB. None on API-key runs"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  queue_time_seconds:
    tier: 1
    units: seconds
    description: "Scheduler wait before generation starts (vLLM waiting_time / queue_time)"
    applicable_agents: [SERVING]
    source: "serving.score_serving_run"
  error_rate:
    tier: 1
    description: "Share of serving requests recorded as failed"
    applicable_agents: [SERVING]
    source: "serving.aggregate_serving"
  prompt_tokens:
    tier: 1
    units: count
    description: "Prompt / input token count for the serving run"
    applicable_agents: [SERVING]
    aggregation: sum
    source: "serving.score_serving_run"
  completion_tokens:
    tier: 1
    units: count
    description: "Completion / output token count for the serving run"
    applicable_agents: [SERVING]
    aggregation: sum
    source: "serving.score_serving_run"
  serving_kind:
    tier: 3
    units: tag
    description: "local | api | unknown — serving family of the run"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.classify_serving_kind"
  quantization:
    tier: 3
    units: tag
    description: "Quantization tag (awq, gptq, fp16, q4_k_m, …) registered with the run"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  gpu_name:
    tier: 3
    units: tag
    description: "Local GPU product name when recorded"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  max_model_len:
    tier: 3
    units: count
    description: "Context window / max_model_len of the served model"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  model:
    tier: 3
    units: tag
    description: "Served model slug (Ollama tag or OpenRouter id)"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  provider:
    tier: 3
    units: tag
    description: "Serving provider family (ollama, vllm, openrouter, …)"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  dtype:
    tier: 3
    units: tag
    description: "Model dtype (fp16, bf16, fp8, …) when recorded"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  gpu_count:
    tier: 3
    units: count
    description: "Local GPU count / tensor-parallel width when recorded"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  tensor_parallel:
    tier: 3
    units: count
    description: "vLLM tensor_parallel_size when recorded"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  serving_profile:
    tier: 3
    units: tag
    description: "Sandbox profile name (ollama, vllm-local, openrouter, …)"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  base_url_host:
    tier: 3
    units: tag
    description: "Hostname of the local or API endpoint"
    applicable_agents: [SERVING]
    aggregation: none
    source: "serving.normalize_serving_record"
  n_requests:
    tier: 3
    units: count
    description: "Number of per-request serving observations in the run"
    applicable_agents: [SERVING]
    aggregation: sum
    source: "serving.aggregate_serving"
  n_docs:
    tier: 3
    units: count
    description: "Document count for the serving run (identity.n, else n_requests)"
    applicable_agents: [SERVING]
    aggregation: sum
    source: "serving.aggregate_serving"

  # ===================== T2 — DEEP =====================
  confusion_matrix:
    tier: 2
    description: "Class-confusion matrix"
    applicable_agents: [CLASSIFIERS, boss]
    aggregation: none
    source: "classification.confusion_matrix"
  per_class_stats:
    tier: 2
    description: "Per-class precision/recall/support"
    applicable_agents: [CLASSIFIERS]
    aggregation: none
    source: "classification.per_class_stats"
  failure_mode_breakdown:
    tier: 2
    description: "Failure taxonomy counts (family_confusion, function_over_form, ...)"
    applicable_agents: [CLASSIFIERS]
    aggregation: none
    source: "failure_modes.summarize_failures"
  bootstrap_ci:
    tier: 2
    description: "Percentile bootstrap CI for the headline metric"
    applicable_agents: [ALL]
    aggregation: none
    source: "bootstrap.bootstrap_ci"
  confidence_calibration_error:
    tier: 2
    description: "|confidence - correctness| calibration gap"
    applicable_agents: [ALL]
    source: "trace_knobs.confidence_calibration_error"
    notes: "absorbs mailroom classification_confidence + extraction_confidence (raw confidences stay at T3 as inputs)"
  hallucination_rate:
    tier: 2
    description: "Rate of doc-unverifiable extracted items"
    applicable_agents: [SPECIALISTS, AUDITORS]
    source: "field_scoring.verify_list_items"
    notes: "mailroom alias: extraction_hallucination_rate"
  per_field_scores:
    tier: 2
    description: "Type-aware per-field scores (date MAE, money error, name fuzzy match)"
    applicable_agents: [SPECIALISTS]
    aggregation: none
    source: "field_scoring.score_field"
  extraction_field_score:
    tier: 2
    description: "Per-field score value (mailroom pilot detail)"
    applicable_agents: [SPECIALISTS]
    notes: "mailroom SCORE_CONFIGS name; sibling of per_field_scores"
  extraction_category_presence:
    tier: 2
    description: "Category-presence scoring result (verbatim-clause fields)"
    applicable_agents: [contracts_specialist]
    source: "field_scoring.score_category_presence"
    notes: "mailroom SCORE_CONFIGS name"
  classification_quality:
    tier: 3
    description: "Legacy numeric quality score (mailroom); superseded by structured metrics"
    applicable_agents: [sorter]
    notes: "mailroom SCORE_CONFIGS name; demoted per pruning plan"
  guardrail_triggered:
    tier: 2
    description: "Guardrail fired (interesting in aggregate, not per-document)"
    applicable_agents: [ALL]
    notes: "demoted from mailroom flat list per pruning plan"
  legalbench_calibration_error:
    tier: 2
    description: "LegalBench confidence calibration error"
    applicable_agents: [court_opinions_specialist]
    source: "tasks.legalbench_score"
    notes: "legalbench_* cluster"

  # Additive sandbox benchmark rates: distinct populations from current suites.
  sandbox_maud_accuracy:
    tier: 1
    description: "All labeled sandbox MAUD questions; missing answers and conflicts miss; None with no labels."
    applicable_agents: [merger_agreement_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required
  sandbox_maud_clean_accuracy:
    tier: 2
    description: "Frozen sandbox CLEAN_QUESTIONS subset; None with no supported questions."
    applicable_agents: [merger_agreement_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required
  sandbox_maud_precision_answered:
    tier: 2
    description: "Sandbox real answers only; placeholders excluded except catalog classes; conflicts count answered and wrong."
    applicable_agents: [merger_agreement_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required
  sandbox_cuad_presence_f1:
    tier: 1
    description: "Category detection within the row-labeled universe; empty universe is unscorable."
    applicable_agents: [contracts_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required
  sandbox_cuad_presence_precision:
    tier: 2
    description: "TP/(TP+FP) within row-labeled categories; zero when no detected categories on a labeled row."
    applicable_agents: [contracts_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required
  sandbox_cuad_presence_recall:
    tier: 2
    description: "TP/(TP+FN) within row-labeled categories; zero when no positive categories on a labeled row."
    applicable_agents: [contracts_specialist]
    source: "sandbox_scoring.sandbox_metric_scores"
    aggregation: none
    ground_truth: required

  # ===================== T3 — LOG =====================
  classification_confidence:
    tier: 3
    description: "Raw classifier confidence (input to calibration)"
    applicable_agents: [CLASSIFIERS]
    notes: "merged into confidence_calibration_error per pruning plan"
  extraction_confidence:
    tier: 3
    description: "Raw extractor confidence (input to calibration)"
    applicable_agents: [SPECIALISTS]
    notes: "merged into confidence_calibration_error per pruning plan"
  judge_notes:
    tier: 3
    units: text
    description: "Free-text adjudication notes — belongs in logs, not scored metrics"
    applicable_agents: [judge]
    notes: "demoted from mailroom flat list per pruning plan"
  stage_completed:
    tier: 3
    description: "Legacy per-stage completion flag"
    applicable_agents: [ALL]
    notes: "consolidated into success_rate"
  run_aborted:
    tier: 3
    description: "Legacy abort flag"
    applicable_agents: [ALL]
    notes: "consolidated into success_rate"
  llm_call_count:
    tier: 3
    units: count
    description: "LLM calls made (cost-calc input, not performance)"
    applicable_agents: [ALL]
    aggregation: sum
    notes: "demoted per pruning plan"
  total_tokens:
    tier: 3
    units: count
    description: "Total tokens in+out"
    applicable_agents: [ALL]
    aggregation: sum
    source: "cost.tokens_summary"
  run_duration_seconds:
    tier: 3
    units: seconds
    description: "Wall-clock run duration"
    applicable_agents: [ALL]
    aggregation: mean

  # ============ MAILROOM BERT FAST PATH (#98 intake gate; KANBAN-061) ============
  # Latency/cost/agreement telemetry for the ONNX BERT classification fast
  # path in llm-mailroom intake. Registered for llm-mailroom SCORE_CONFIGS;
  # not computed in this package.
  bert_pass:
    tier: 1
    units: boolean
    description: "Intake BERT gate verdict (true = gate passed / BERT label forwarded)"
    applicable_agents: [INTAKE]
    aggregation: none
    source: null
    ground_truth: none
    notes: "mailroom SCORE_CONFIGS name; emitted by intake gate (llm-mailroom agents/bert_intake.py)"
  bert_sorter_agreement:
    tier: 1
    units: boolean
    description: "BERT fast-path label agrees with the sorter/reviewer reference label"
    applicable_agents: [INTAKE, sorter]
    aggregation: none
    source: null
    ground_truth: none
    notes: "mailroom SCORE_CONFIGS name; computed off-line for drift tracking"
  bert_fail_soft:
    tier: 1
    units: boolean
    description: "BERT gate failed OPEN (label discarded, sorter authority restored)"
    applicable_agents: [INTAKE]
    aggregation: none
    source: null
    ground_truth: none
    notes: "mailroom SCORE_CONFIGS name; never true with bert_pass true for the same doc"
  bert_elapsed_ms:
    tier: 0
    units: milliseconds
    description: "BERT fast-path classifier wall-clock time (ONNX inference, intake doc)"
    applicable_agents: [INTAKE]
    aggregation: mean
    source: null
    ground_truth: none
    notes: "mailroom SCORE_CONFIGS name; measured at intake gate"
  fast_path_est_cost_usd:
    tier: 1
    units: USD
    description: "Derived estimated cost of the BERT fast path (elapsed_ms x power x power price)"
    applicable_agents: [INTAKE]
    aggregation: sum
    source: null
    ground_truth: none
    notes: "mailroom SCORE_CONFIGS name; derived from bert_elapsed_ms, never conflated with estimated_cost_usd (LLM call cost)"
  classification_attempts:
    tier: 3
    units: count
    description: "Retry attempts for the classification stage"
    applicable_agents: [sorter]
    aggregation: sum
  extraction_attempts:
    tier: 3
    units: count
    description: "Retry attempts for the extraction stage"
    applicable_agents: [SPECIALISTS]
    aggregation: sum
  prompt_version:
    tier: 3
    units: tag
    description: "Prompt version used (permanent metadata tag)"
    applicable_agents: [ALL]
    aggregation: none
  model_slug:
    tier: 3
    units: tag
    description: "Model slug used"
    applicable_agents: [ALL]
    aggregation: none
  trace_id:
    tier: 3
    units: tag
    description: "Observability trace id (permanent, linked to source docs)"
    applicable_agents: [ALL]
    aggregation: none
  raw_prediction:
    tier: 3
    units: text
    description: "Per-document raw prediction (30-day retention window)"
    applicable_agents: [ALL]
    aggregation: none
  legalbench_n_questions:
    tier: 3
    units: count
    description: "LegalBench question count for the run"
    applicable_agents: [court_opinions_specialist]
    aggregation: sum
    notes: "legalbench_* cluster"
  legalbench_task:
    tier: 3
    units: tag
    description: "LegalBench task identifier"
    applicable_agents: [court_opinions_specialist]
    aggregation: none
    notes: "legalbench_* cluster"
"""

_CACHE: dict[str, Registry] = {}


def load_registry(path: str | Path | None = None) -> Registry:
    """Load a registry: explicit path > env var > built-in default."""
    resolved: str | Path | None = path or os.environ.get(_ENV_VAR)
    if resolved:
        key = str(Path(resolved).resolve())
        if key not in _CACHE:
            _CACHE[key] = Registry.from_yaml(key)
        return _CACHE[key]
    if "default" not in _CACHE:
        _CACHE["default"] = Registry.from_dict(
            yaml.safe_load(DEFAULT_METRICS_YAML)
        )
    return _CACHE["default"]


def get_registry() -> Registry:
    """Convenience accessor for the effective (default/custom) registry."""
    return load_registry()


def clear_registry_cache() -> None:
    """Drop cached registries (test isolation / config reload)."""
    _CACHE.clear()
