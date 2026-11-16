"""Routing must not read ``ground_truth`` unless oracle routing is switched on (#207).

``run_pipeline(ground_truth=...)`` keeps the label in state for tracing, scoring and
result emission. Six routing call sites used to read it as well, so experiment runs
were oracle-assisted and their automation / accuracy numbers did not transfer to
production. One explicit switch, default off, now gates those six sites:

* taxonomy ``routing.use_ground_truth`` (default ``false``);
* env ``MAILROOM_ORACLE_ROUTING`` (``1``/``true``/``yes``/``on`` or the negations),
  which wins over the taxonomy for a single run.

Cause labelling (``collect_review_causes``) is evidence, not routing, and stays on.
"""

from __future__ import annotations

import copy

import pytest

import pipeline.config as pconfig
from graph.routing import (
    after_classification_arbiter,
    after_classify,
    after_extraction,
    after_retry_classify,
    after_review_classify,
)
from pipeline.reconsideration import CLASS_MISS, SUBCLASS_MISS, collect_review_causes

ENV = "MAILROOM_ORACLE_ROUTING"


@pytest.fixture(autouse=True)
def _no_oracle_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _taxonomy_with(monkeypatch, value):
    cfg = copy.deepcopy(pconfig.load_config())
    if value is None:
        cfg.pop("routing", None)
    else:
        cfg["routing"] = {"use_ground_truth": value}
    monkeypatch.setattr(pconfig, "load_config", lambda: cfg)


GT_CLASS = {"expected_doc_class": "correspondence"}
GT_FIELDS = {
    "expected_fields": {
        "parties": ["Acme"],
        "governing_law": "Delaware",
        "effective_date": "2024-01-01",
    }
}

# (site name, router, state with a GT that disagrees, route with GT ignored, route with oracle)
CLASS_SITES = {
    "after_classify": (
        after_classify,
        {
            "doc_type": "contract",
            "classification_confidence": 0.99,
            "classification_attempts": 1,
            "ground_truth": GT_CLASS,
        },
        "extract",
        "review_classify",
    ),
    "after_retry_classify": (
        after_retry_classify,
        {
            "doc_type": "contract",
            "classification_confidence": 0.99,
            "classification_attempts": 2,
            "ground_truth": GT_CLASS,
        },
        "extract",
        "review_classify",
    ),
    "after_classification_arbiter": (
        after_classification_arbiter,
        {
            "doc_type": "contract",
            "classification_arbiter_decision": "accept_sorter",
            "ground_truth": GT_CLASS,
        },
        "extract",
        "human_review",
    ),
    "after_review_classify_decision_route": (
        after_review_classify,
        {
            "review_route": "extract",
            "doc_type": "contract",
            "reviewer_doc_type": "contract",
            "ground_truth": GT_CLASS,
        },
        "extract",
        "human_review",
    ),
    "after_review_classify_llm_accept": (
        after_review_classify,
        {
            "review_verdict": "reviewer_agrees_high",
            "reviewer_confidence": 0.99,
            "reviewer_doc_type": "contract",
            "doc_type": "contract",
            "ground_truth": GT_CLASS,
        },
        "extract",
        "human_review",
    ),
}

COVERAGE_STATE = {
    "doc_type": "contract",
    "extraction_confidence": 0.99,
    "extraction_attempts": 1,
    "extracted_data": {"parties": ["Acme"], "governing_law": ""},
    "ground_truth": GT_FIELDS,
}


def _without_gt(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "ground_truth"}


@pytest.mark.parametrize("site", sorted(CLASS_SITES))
def test_gt_class_miss_is_ignored_by_default(site):
    router, state, ignored, _oracle = CLASS_SITES[site]
    assert router(state) == ignored
    # exactly what a production run (no label in state) does
    assert router(state) == router(_without_gt(state))


@pytest.mark.parametrize("site", sorted(CLASS_SITES))
def test_gt_class_miss_routes_when_oracle_env_is_on(site, monkeypatch):
    router, state, _ignored, oracle = CLASS_SITES[site]
    monkeypatch.setenv(ENV, "1")
    assert router(state) == oracle


@pytest.mark.parametrize("site", sorted(CLASS_SITES))
def test_gt_class_miss_routes_when_taxonomy_is_on(site, monkeypatch):
    router, state, _ignored, oracle = CLASS_SITES[site]
    _taxonomy_with(monkeypatch, True)
    assert router(state) == oracle


def test_gt_coverage_floor_is_ignored_by_default():
    assert after_extraction(COVERAGE_STATE) == "compile_report"
    assert after_extraction(COVERAGE_STATE) == after_extraction(_without_gt(COVERAGE_STATE))
    assert after_extraction({**COVERAGE_STATE, "extraction_attempts": 3}) == "compile_report"


def test_gt_coverage_floor_routes_when_oracle_env_is_on(monkeypatch):
    monkeypatch.setenv(ENV, "true")
    assert after_extraction(COVERAGE_STATE) == "retry_extract"
    assert after_extraction({**COVERAGE_STATE, "extraction_attempts": 3}) == "human_review"


def test_gt_coverage_floor_routes_when_taxonomy_is_on(monkeypatch):
    _taxonomy_with(monkeypatch, True)
    assert after_extraction(COVERAGE_STATE) == "retry_extract"


def test_non_gt_extraction_gates_still_apply_with_the_switch_off():
    hollow = {
        "doc_type": "contract",
        "extraction_confidence": 0.99,
        "extraction_attempts": 1,
        "extracted_data": {"confidence": 0.99, "mock_extraction": True},
        "ground_truth": GT_FIELDS,
    }
    assert after_extraction(hollow) == "retry_extract"
    low = {**COVERAGE_STATE, "extraction_confidence": 0.10, "extraction_attempts": 1}
    assert after_extraction(low) == "retry_extract"


# ---- cause labelling is evidence, never gated ----------------------------------------


def test_class_miss_cause_labelling_works_with_the_switch_off():
    state = {
        "doc_type": "contract",
        "ground_truth": {
            "expected_hf_class": "correspondence",
            "expected_subclass": "email",
        },
        "doc_subclass": "letter",
    }
    causes = collect_review_causes(state)
    assert CLASS_MISS in causes
    assert SUBCLASS_MISS in causes


def test_expected_class_and_class_miss_predicate_are_not_gated():
    from pipeline.reconsideration import class_misses_ground_truth, expected_class

    state = {"doc_type": "contract", "ground_truth": GT_CLASS}
    assert expected_class(state) == "correspondence"
    assert class_misses_ground_truth(state) is True


# ---- the switch itself ----------------------------------------------------------------


def test_switch_defaults_off_and_taxonomy_ships_it_off():
    assert pconfig.load_config()["routing"]["use_ground_truth"] is False
    assert pconfig.oracle_routing_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_env_truthy_values_turn_it_on(raw, monkeypatch):
    monkeypatch.setenv(ENV, raw)
    assert pconfig.oracle_routing_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_env_falsy_value_beats_a_taxonomy_on(raw, monkeypatch):
    _taxonomy_with(monkeypatch, True)
    monkeypatch.setenv(ENV, raw)
    assert pconfig.oracle_routing_enabled() is False


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "2"])
def test_unrecognised_env_falls_back_to_taxonomy(raw, monkeypatch):
    monkeypatch.setenv(ENV, raw)
    _taxonomy_with(monkeypatch, True)
    assert pconfig.oracle_routing_enabled() is True
    _taxonomy_with(monkeypatch, False)
    assert pconfig.oracle_routing_enabled() is False


@pytest.mark.parametrize("value", [None, "yes please", 1, []])
def test_taxonomy_needs_a_real_true(value, monkeypatch):
    """A missing block, or anything that is not boolean ``true``, is off."""
    _taxonomy_with(monkeypatch, value)
    assert pconfig.oracle_routing_enabled() is False


def test_override_beats_env_and_taxonomy_and_restores(monkeypatch):
    monkeypatch.setenv(ENV, "0")
    with pconfig.oracle_routing_override(True):
        assert pconfig.oracle_routing_enabled() is True
        with pconfig.oracle_routing_override(False):
            assert pconfig.oracle_routing_enabled() is False
        assert pconfig.oracle_routing_enabled() is True
    assert pconfig.oracle_routing_enabled() is False


# ---- the run says whether it was oracle-assisted ----------------------------------------


def _traced_metadata(monkeypatch, tmp_path, *, ground_truth):
    """Run the real ``run_pipeline`` on a file and return the trace metadata it opened."""
    import contextlib

    import graph.build_graph as bg

    captured: dict = {}

    @contextlib.contextmanager
    def fake_trace(**kwargs):
        captured.update(kwargs["metadata"])
        raise RuntimeError("stop before the graph runs")
        yield  # pragma: no cover

    import observability.tracing as tracing

    monkeypatch.setattr(tracing, "pipeline_trace", fake_trace)
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    with contextlib.suppress(RuntimeError):
        bg.run_pipeline(f, "M-1", ground_truth=ground_truth, run_id="r207")
    return captured


@pytest.mark.parametrize("gt", [None, GT_CLASS])
def test_trace_metadata_records_oracle_routing_off(monkeypatch, tmp_path, gt, temp_base_dir):
    assert _traced_metadata(monkeypatch, tmp_path, ground_truth=gt)["oracle_routing"] is False


def test_trace_metadata_records_oracle_routing_on(monkeypatch, tmp_path, temp_base_dir):
    monkeypatch.setenv(ENV, "1")
    assert _traced_metadata(monkeypatch, tmp_path, ground_truth=GT_CLASS)["oracle_routing"] is True


def test_oracle_routing_warns_once_per_run(monkeypatch, tmp_path, temp_base_dir):
    import graph.build_graph as bg

    seen: list[str] = []
    monkeypatch.setattr(bg.logger, "warning", lambda event, **kw: seen.append(event))
    monkeypatch.setattr(bg, "_ORACLE_ROUTING_WARNED", set())
    monkeypatch.setenv(ENV, "1")
    for _ in range(3):
        _traced_metadata(monkeypatch, tmp_path, ground_truth=GT_CLASS)
    assert seen.count("oracle_routing_enabled") == 1


def test_no_warning_without_oracle_routing(monkeypatch, tmp_path, temp_base_dir):
    import graph.build_graph as bg

    seen: list[str] = []
    monkeypatch.setattr(bg.logger, "warning", lambda event, **kw: seen.append(event))
    monkeypatch.setattr(bg, "_ORACLE_ROUTING_WARNED", set())
    _traced_metadata(monkeypatch, tmp_path, ground_truth=GT_CLASS)
    assert "oracle_routing_enabled" not in seen
