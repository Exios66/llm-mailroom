"""llm-dojo-scoring v0.12.2 pin — registry, prompt catalog, serving suite."""

from __future__ import annotations

import re
from pathlib import Path

import llm_dojo_scoring
from llm_dojo_scoring import get_suite, list_suites, load_registry
from llm_dojo_scoring.pruning import headline_metrics
from llm_dojo_scoring.prompts import get_prompt, list_prompts
from llm_dojo_scoring.registry import MetricTier
from llm_dojo_scoring.serving import compare_serving
from observability.scores import SCORE_CONFIGS, registry_score_meta
from observability.suite_scoring import SUITE_EXTRA_SCORE_NAMES


def test_installed_dojo_is_v0122():
    # Release contract = the git pin in pyproject.toml. Monorepo dev resolves
    # the pin to the workspace member via [tool.uv.sources], so the installed
    # version may be newer than the pin (>= 0.12 required).
    pin = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "llm-dojo-scoring.git@v0.12.2" in pin
    version = re.match(r"(\d+)\.(\d+)", llm_dojo_scoring.__version__)
    assert version is not None
    assert tuple(map(int, version.groups())) >= (0, 12)


def test_local_vs_api_serving_suite_registered():
    assert "local_vs_api" in list_suites(kind="serving")
    suite = get_suite("local_vs_api")
    assert suite.retired is not True
    assert "ttft_seconds" in headline_metrics("local_vs_api")


def test_compare_serving_table_and_scorecard():
    local = [{"ttft_seconds": 0.5, "tokens_per_second": 120.0, "model": "ollama/qwen"}]
    api = [{"ttft_seconds": 0.3, "tokens_per_second": 200.0, "model": "qwen/qwen3.7-flash"}]
    out = compare_serving(local, api)
    assert "table" in out
    assert "scorecard" in out
    assert any(row.get("metric") == "ttft_seconds" for row in out["table"])
    assert out["scorecard"]["agent"] == "local_vs_api"


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
    serving = get_prompt("local_vs_api")
    assert serving.kind == "procedural"
    assert serving.text == ""
    sorter = get_prompt("sorter")
    assert sorter.family == "production"
    assert sorter.version == "sorter_v14"
    assert sorter.text.strip()
    docclass = get_prompt("sorter", family="docclass")
    assert docclass.family == "docclass"
    assert docclass.text != sorter.text


def test_production_prompt_catalog_is_populated():
    """Catalog is a scored snapshot; mailroom templates are source of truth."""
    prod = list_prompts(family="production", kind="llm")
    agents = {rec.agent for rec in prod}
    assert "contracts_specialist" in agents
    assert "sorter" in agents
    assert all(rec.text.strip() or rec.kind != "llm" for rec in prod)


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
