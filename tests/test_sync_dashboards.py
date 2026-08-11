"""Issue #2: tailored Langfuse dashboard definitions (sync_dashboards.py).

Validates the widget/dashboard specs without touching the Langfuse API:
every widget's score filter references a registered score config, and the
three dashboards cover the required dimensions (completion, correctness,
accuracy, latency, duration).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import sync_dashboards as sd  # noqa: E402


def _score_names():
    from observability.scores import SCORE_CONFIGS

    return {c["name"] for c in SCORE_CONFIGS}


class TestDimensionWidgets:
    def test_all_dashboards_defined(self):
        assert sd.QUALITY_DASHBOARD["name"]
        assert sd.JUDGE_DASHBOARD["name"]
        assert sd.DIMENSION_DASHBOARD["name"]
        assert sd.PERF_DASHBOARD["name"]

    def test_issue2_dimensions_covered(self):
        names = " ".join(w.name for w in sd.DIMENSION_WIDGETS)
        for dimension in ("Completion", "Correctness", "Accuracy", "Duration", "Latency", "Cost"):
            assert dimension in names, f"missing {dimension} widget"

    def test_score_filters_reference_registered_configs(self):
        registered = _score_names()
        for w in sd.DIMENSION_WIDGETS + sd.PERF_WIDGETS:
            for f in w.filters:
                if f["column"] == "name":
                    assert f["value"] in registered, (
                        f"{w.name} filters on unregistered score {f['value']!r}"
                    )

    def test_pilot_env_scoped(self):
        for w in sd.DIMENSION_WIDGETS:
            envs = [f for f in w.filters if f["column"] == "environment"]
            assert envs, f"{w.name} has no environment filter"
            assert envs[0]["value"] == ["pilot"]

    def test_models_dimension_not_provided_model_name(self):
        """Dashboard-correctness: model widgets dimension on the requested
        `model` string with a positive configured-models filter — never the
        adapter's providedModelName with a negative 'not openai' filter."""
        for w in sd.QUALITY_WIDGETS + sd.JUDGE_WIDGETS + sd.PERF_WIDGETS:
            if "model" in w.dimensions:
                assert "providedModelName" not in w.dimensions
                model_filters = [f for f in w.filters if f["column"] == "model"]
                assert model_filters, f"{w.name} dimensions by model without a model filter"
                assert model_filters[0]["operator"] == "any of"
        for w in sd.JUDGE_WIDGETS + sd.PERF_WIDGETS:
            for f in w.filters:
                assert f.get("operator") != "does not contain", (
                    f"{w.name} uses a negative model filter (can hide real models)"
                )

    def test_score_widgets_no_trace_dimension(self):
        """Scores attach to traces named 'document-pipeline' — dimensioning by
        traceName collapses everything; score widgets must use no dimension or
        the score name."""
        for w in sd.DIMENSION_WIDGETS + sd.PERF_WIDGETS:
            if w.view == "scores-numeric":
                assert "traceName" not in w.dimensions, f"{w.name} dimensions by traceName"
                assert "observationPromptName" not in w.dimensions, f"{w.name} dimensions by observationPromptName"

    def test_widget_signature_roundtrip(self):
        for w in sd.QUALITY_WIDGETS + sd.JUDGE_WIDGETS + sd.DIMENSION_WIDGETS:
            req = sd._spec_to_request(w)
            assert sd._widget_signature_from_req(req) == sd._widget_signature(
                type("W", (), {
                    "view": req["view"],
                    "dimensions": [type("D", (), {"field": d["field"]}) for d in req["dimensions"]],
                    "metrics": [type("M", (), {"measure": m["measure"], "agg": m["agg"]}) for m in req["metrics"]],
                    "chart_type": req["chart_type"],
                    "filters": [
                        type("F", (), {
                            "column": f["column"], "operator": f["operator"],
                            "type": f["type"], "value": f.get("value"),
                            "key": f.get("key"),
                        }) for f in req["filters"]
                    ],
                })
            )
