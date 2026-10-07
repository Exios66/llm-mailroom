"""experiments/mailroom_gate.py — the experiment-action entry point (plan Task 22)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langfuse import RegressionError

GATE = Path(__file__).resolve().parents[2] / "experiments" / "mailroom_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("mailroom_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    """Stand-in for RunnerContext: runs the task + evaluators locally."""

    def __init__(self, corrupt=False):
        self.corrupt = corrupt

    def run_experiment(self, *, name, data, task, evaluators, run_evaluators, **kw):
        results = []
        for item in data:
            out = task(item=item)
            if self.corrupt:
                out = {**out, "doc_type": "wrong"}
            results.append(SimpleNamespace(item=item, output=out, evaluations=[]))
        return SimpleNamespace(item_results=results, run_evaluations=[])


def test_gate_passes_against_committed_baseline():
    result = _load().experiment(_Ctx())
    assert len(result.item_results) >= 4


def test_gate_raises_regression_error_when_classification_degrades():
    with pytest.raises(RegressionError):
        _load().experiment(_Ctx(corrupt=True))


def test_committed_baseline_is_well_formed():
    base = json.loads((GATE.parent.parent / "docs/superpowers/baselines/experiment-baseline.json").read_text())
    assert base["n_items"] > 0 and base["n_errors"] == 0
    assert base["metrics"]["class_accuracy"] == 1.0


def test_langfuse_floor_covers_mask_otel_spans():
    import re

    text = (GATE.parent.parent / "pyproject.toml").read_text()
    m = re.search(r'"langfuse>=(\d+)\.(\d+)', text)
    assert (int(m.group(1)), int(m.group(2))) >= (4, 9)


def test_gate_runs_in_mock_environment(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_ENVIRONMENT", raising=False)
    mod = _load()
    mod.experiment(_Ctx())
    import os

    assert os.environ["OBSERVABILITY_ENVIRONMENT"] == "mock"
