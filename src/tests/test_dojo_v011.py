"""llm-dojo-scoring v0.11.0 pin — registry metadata + prompt catalog."""

from __future__ import annotations

import re

import llm_dojo_scoring
from llm_dojo_scoring import load_registry
from llm_dojo_scoring.pruning import headline_metrics
from llm_dojo_scoring.prompts import get_prompt, list_prompts
from llm_dojo_scoring.registry import MetricTier
from observability.scores import SCORE_CONFIGS, registry_score_meta
from observability.suite_scoring import SUITE_EXTRA_SCORE_NAMES


def test_installed_dojo_is_v011():
    assert llm_dojo_scoring.__version__ == "0.11.0"


def test_extraction_f1_carries_citation_and_required_gt():
    metric = load_registry().get("extraction_f1")
    assert metric.citation.strip()
    assert metric.inclusion.strip()
    assert metric.ground_truth == "required"
    meta = registry_score_meta("extraction_f1")
    assert "van Rijsbergen" in meta["citation"] or "ACE" in meta["citation"]
    assert meta["ground_truth"] == "required"


def test_structural_and_emitter_ground_truth_labels():
    reg = load_registry()
    assert reg.get("determination_consistency").ground_truth == "structural"
    assert reg.get("intake_prep_completeness").ground_truth == "structural"
    assert reg.get("expected_field_presence").ground_truth == "none"
    assert reg.get("expected_field_presence").source is None


def test_field_presence_is_not_a_mailroom_score_config():
    names = {c["name"] for c in SCORE_CONFIGS}
    assert "field_presence" not in names
    assert "field_presence" not in SUITE_EXTRA_SCORE_NAMES
    metric = load_registry().get("field_presence")
    blob = (metric.citation + " " + metric.inclusion + " " + metric.notes).lower()
    assert "does not emit" in blob or "not computed" in blob


def test_score_configs_t0_t1_have_registry_metadata():
    reg = load_registry()
    names = {c["name"] for c in SCORE_CONFIGS}
    for metric in reg.metrics.values():
        if metric.tier > MetricTier.CORE or metric.name not in names:
            continue
        assert metric.citation.strip(), metric.name
        assert metric.ground_truth in {"required", "optional", "structural", "none"}, metric.name


def test_headline_metrics_live_specialists():
    insurance = headline_metrics("insurance_claims_specialist")
    assert "extraction_overall_score" in insurance
    assert "extraction_f1" in insurance
    assert "extraction_f2" in insurance
    correspondence = headline_metrics("correspondence_specialist")
    assert "content_topic_f1_macro" in correspondence
    sorter = headline_metrics("sorter")
    assert "accuracy" in sorter
    assert "f1_macro" in sorter


def test_prompt_catalog_honest_non_llm_roles():
    intake = get_prompt("intake")
    assert intake.kind == "deterministic"
    assert intake.text == ""
    archivist = get_prompt("archivist")
    assert archivist.kind == "procedural"
    assert archivist.text == ""
    auditor = get_prompt("insurance_claims_auditor")
    assert auditor.kind == "proposed"
    assert auditor.text == ""
    sorter = get_prompt("sorter")
    assert sorter.family == "production"
    assert sorter.version == "sorter_v14"
    assert sorter.text.strip()
    docclass = get_prompt("sorter", family="docclass")
    assert docclass.family == "docclass"
    assert docclass.text != sorter.text


def test_production_prompt_templates_match_or_extend_catalog():
    """Live mailroom templates are the source of truth; catalog is a snapshot.

    Equal is the happy pin. A pure append (mailroom mutated after the
    snapshot) is allowed. A rewrite that drops catalog bytes is drift.
    """
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    for rec in list_prompts(family="production", kind="llm"):
        if rec.agent not in templates:
            continue
        live = templates[rec.agent]
        catalog = rec.text
        assert live.strip() and catalog.strip(), rec.agent
        assert live == catalog or live.startswith(catalog.rstrip()), rec.agent


def test_live_prompts_omit_t0_t1_registry_ids():
    """Anti-priming: snake_case T0/T1 ids stay out of model-visible text."""
    from llm.prompts import prompt_templates

    reg = load_registry()
    denylist = [
        m.name
        for m in reg.metrics.values()
        if m.tier <= MetricTier.CORE and "_" in m.name
    ]
    templates = prompt_templates()
    for agent, text in templates.items():
        priming = ()
        try:
            priming = get_prompt(agent).priming
        except KeyError:
            pass
        for name in denylist:
            if name in priming:
                continue
            if re.search(rf"\b{re.escape(name)}\b", text):
                raise AssertionError(
                    f"prompt_templates()[{agent!r}] contains registry id {name!r}"
                )
