"""Pre-built metric bundles — named groupings of registry metrics per
task type, so a new agent is ONE profile entry instead of 4+ file edits.

A bundle is just a curated name → metric-name list. Every name must resolve
in the :class:`~llm_dojo_scoring.registry.Registry` — ``validate_bundle``
enforces it at load time so a typo fails fast instead of silently dropping
a metric from a dashboard.

Bundles (KANBAN-061):

===================  =====================================================
name                 for
===================  =====================================================
classification       sorter, judge, boss
extraction           all specialists (typed fields)
extraction_open      open-ended extractors (Jaccard/EM)
cost                 every agent
factuality           extractors + audit (verification-backed precision)
laziness_detection   contract evaluators
audit                audit agent (disagreement/resolution)
reporter             aggregators (derived-only)
transcription        pdf_transcriber, image_extractor
intake               intake clerk (pre-sorter text prep)
serving              local vs API serving comparison
===================  =====================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import MetricTier, Registry, load_registry

__all__ = [
    "Bundle",
    "BUILTIN_BUNDLES",
    "get_bundle",
    "list_bundles",
    "validate_bundle",
    "bundle_metric_names",
]


@dataclass(frozen=True)
class Bundle:
    """A named metric bundle. ``metric_names`` must resolve in the registry."""

    name: str
    description: str
    metric_names: tuple[str, ...]
    #: Optional per-agent extras merged on top (agent → extra metric names).
    agent_overrides: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def metrics_for(self, agent: str | None = None) -> tuple[str, ...]:
        """Return base metrics plus unique extras configured for the requested agent."""
        names = list(self.metric_names)
        if agent and agent in self.agent_overrides:
            for extra in self.agent_overrides[agent]:
                if extra not in names:
                    names.append(extra)
        return tuple(names)


BUILTIN_BUNDLES: dict[str, Bundle] = {
    b.name: b
    for b in (
        Bundle(
            name="classification",
            description="Classifier runs — sorter, judge, boss",
            metric_names=(
                "accuracy",              # T0 headline
                "f1_macro",              # T0 headline
                "classification_correct",
                "precision",
                "recall",
                "f2",
                "precision_macro",
                "recall_macro",
                "f2_macro",
                "false_positive_rate",
                "false_negative_rate",
                "exact_accuracy",
                "aligned_accuracy",
                "subclass_accuracy",
                "subclass_f1_macro",
                "subclass_precision_macro",
                "subclass_recall_macro",
                "subclass_f2_macro",
                "estimated_cost_usd",
                "cost_per_document",
                "schema_valid",
                "parse_error",
                "success_rate",
            ),
            agent_overrides={
                "sorter": ("confusion_matrix", "failure_mode_breakdown", "per_class_stats", "bootstrap_ci"),
                "sorter_reviewer": ("confusion_matrix", "per_class_stats", "bootstrap_ci"),
                "judge": ("confidence_calibration_error",),
            },
        ),
        Bundle(
            name="extraction",
            description="Typed-field extraction — all specialists",
            metric_names=(
                "extraction_overall_score",   # T0 headline
                "extraction_f1",              # T0 headline
                "extraction_f2",              # T0 (insurance board number; all specialists compute)
                "extraction_precision",
                "extraction_recall",
                "field_presence",
                "entity_list_precision",
                "entity_list_recall",
                "entity_list_f1",
                "verified_precision",
                "completeness",
                "schema_valid",
                "parse_error",
                "success_rate",
                "estimated_cost_usd",
                "cost_per_document",
            ),
            agent_overrides={
                "contracts_specialist": (
                    "jaccard_similarity",
                    "laziness_rate",
                    "hallucination_rate",
                    "extraction_category_presence",
                    "date_mae_days",
                    "money_mae_usd",
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
                ),
            },
        ),
        Bundle(
            name="extraction_open",
            description="Open-ended extraction — token-overlap quality",
            metric_names=(
                "jaccard_similarity",
                "accuracy",
                "contracteval_false_no_related",
                "f1_macro",
                "f2",
            ),
        ),
        Bundle(
            name="cost",
            description="Financial efficiency — every agent",
            metric_names=(
                "estimated_cost_usd",
                "cost_per_document",
                "total_tokens",
                "llm_call_count",
            ),
        ),
        Bundle(
            name="factuality",
            description="Verification-backed precision — extractors + audit",
            metric_names=(
                "verified_precision",
                "hallucination_rate",
            ),
        ),
        Bundle(
            name="laziness_detection",
            description="Bail-out detection — contract evaluators",
            metric_names=(
                "laziness_rate",
                "contracteval_false_no_related",
            ),
        ),
        Bundle(
            name="audit",
            description="Verification agent — disagreement/resolution (KANBAN-060 feed)",
            metric_names=(
                "audit_disagreement_rate",
                "audit_resolution_rate",
                "verified_precision",
                "hallucination_rate",
                "cost_per_document",
            ),
        ),
        Bundle(
            name="reporter",
            description="Aggregators — derived metrics only, no own emissions",
            metric_names=(
                "accuracy",
                "f1_macro",
                "extraction_overall_score",
                "success_rate",
                "cost_per_document",
            ),
        ),
        Bundle(
            name="transcription",
            description="PDF→text / OCR quality (WER/CER + token-F1 + exact match)",
            metric_names=(
                "accuracy",
                "f1_macro",
                "wer",
                "cer",
                "word_accuracy",
            ),
        ),
        Bundle(
            name="intake",
            description=(
                "Pre-sorter text prep (NFC, hyphen unwrap, whitespace, "
                "messy flag) — deterministic clerk gold; LLM intake scored "
                "against the same gold"
            ),
            metric_names=(
                "accuracy",
                "f1_macro",
                "intake_prep_completeness",
                "intake_changed_rate",
                "intake_messy_rate",
                "success_rate",
                "cost_per_document",
            ),
            agent_overrides={
                "intake": (
                    "intake_hyphen_unwraps",
                    "intake_collapsed_blanks",
                ),
            },
        ),
        Bundle(
            name="serving",
            description="Local vs API serving comparison — TTFT, throughput, utilization, identity",
            metric_names=(
                "ttft_seconds",
                "tokens_per_second",
                "tpot_seconds",
                "e2e_latency_seconds",
                "ttft_p50",
                "ttft_p95",
                "e2e_p50",
                "e2e_p95",
                "output_tokens_per_second",
                "prompt_tokens_per_second",
                "requests_per_second",
                "docs_per_second",
                "gpu_utilization",
                "kv_cache_utilization",
                "gpu_memory_used_gb",
                "queue_time_seconds",
                "error_rate",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "estimated_cost_usd",
                "cost_per_document",
                "serving_kind",
                "quantization",
                "gpu_name",
                "max_model_len",
                "model",
                "provider",
                "dtype",
                "gpu_count",
                "tensor_parallel",
                "serving_profile",
                "prompt_version",
                "model_slug",
                "base_url_host",
                "n_requests",
                "n_docs",
            ),
        ),
    )
}


def get_bundle(
    name: str,
    *,
    registry: Registry | None = None,
    validate: bool = True,
) -> Bundle:
    """Return the named bundle, optionally validating against the registry."""
    try:
        bundle = BUILTIN_BUNDLES[name]
    except KeyError:
        raise KeyError(
            f"unknown bundle {name!r}; known: {sorted(BUILTIN_BUNDLES)}"
        ) from None
    if validate:
        validate_bundle(bundle, registry=registry)
    return bundle


def list_bundles() -> list[str]:
    """Return the built-in bundle names in sorted order."""
    return sorted(BUILTIN_BUNDLES)


def validate_bundle(
    bundle: Bundle,
    *,
    registry: Registry | None = None,
    min_tier: MetricTier = MetricTier.LOG,
) -> list[str]:
    """Every bundle metric must exist in the registry (fail-fast on typos).

    Returns the validated names; raises ``KeyError`` on the first unknown one.
    """
    reg = registry or load_registry()
    for name in bundle.metric_names:
        reg.get(name)
    for extras in bundle.agent_overrides.values():
        for extra in extras:
            reg.get(extra)
    return list(bundle.metric_names)


def bundle_metric_names(
    bundle: str | Bundle,
    *,
    agent: str | None = None,
    max_tier: int | None = None,
    registry: Registry | None = None,
) -> list[str]:
    """Resolve a bundle (plus agent extras) to concrete metric names.

    ``max_tier`` prunes the result the same way :meth:`Registry.filter` does —
    this is what dashboards call for a "headlines only" panel.
    """
    b = bundle if isinstance(bundle, Bundle) else get_bundle(bundle, registry=registry)
    reg = registry or load_registry()
    names = list(b.metrics_for(agent))
    if max_tier is not None:
        names = [n for n in names if reg.get(n).tier <= MetricTier(max_tier)]
    return names
