"""llm-dojo-scoring v0.16.0 pin — registry, prompt catalog, serving suite."""

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


def test_installed_dojo_is_v0160():
    pin = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "llm-dojo-scoring.git@v0.16.0" in pin
    version = re.match(r"(\d+)\.(\d+)", llm_dojo_scoring.__version__)
    assert version is not None
    assert tuple(map(int, version.groups())) >= (0, 12)
