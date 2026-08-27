#!/usr/bin/env python3
"""Evaluate one mailroom LLM agent in isolation (no 13-node graph).

Live Langfuse evaluators stay pipeline-level (``pipeline-result``) by design.
This script is the local methodology: load labeled cases, invoke a single
agent, score with the same deterministic classifiers / field scorers the
pipeline uses.

Usage:
    PYTHONPATH=src python src/scripts/run_agent_eval.py --list
    PYTHONPATH=src python src/scripts/run_agent_eval.py --agent sorter --mock
    PYTHONPATH=src python src/scripts/run_agent_eval.py --agent insurance_claims_specialist --mock --n 3
    PYTHONPATH=src python src/scripts/run_agent_eval.py --agent all --mock --n 1 --self-check
    PYTHONPATH=src python src/scripts/run_agent_eval.py --agent sorter --real --n 5

``--real`` is gated by ``prepare_samples.is_real_sample`` (CUAD / LegalBench
only). Synthetic insurance/compliance/corporate/correspondence samples are
mock-only, matching ``run_pilot.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()
from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

from observability.agent_eval import LLM_AGENTS, evaluate_agent  # noqa: E402


def _install_mocks() -> None:
    """Deterministic fakes so ``--mock`` never hits the network."""
    from unittest.mock import MagicMock

    from langchain_agents.base_agent import BaseAgent as LangChainBase
    from langchain_agents.mock import FakeLangChainLLM

    fake = FakeLangChainLLM()
    LangChainBase.llm = lambda self, _fake=fake: _fake  # type: ignore[method-assign]

    def _structured_json(*_a, **_k):
        payload = dict(fake.extraction)
        payload.setdefault("doc_type", fake.classification.get("doc_type", "contract"))
        payload.setdefault("confidence", 0.9)
        payload.setdefault("completeness", 1.0)
        payload.setdefault("completeness_label", "complete")
        payload.setdefault("decision", "approved")
        payload.setdefault("reasoning", "mock")
        payload.setdefault("fields_to_fix", [])
        payload.setdefault("handoff_summary", "mock")
        payload.setdefault("resolution_notes", "mock")
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(payload)
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = MagicMock(prompt_tokens=10, completion_tokens=10, total_tokens=20)
        return mock_completion

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = _structured_json

    import agents.base as base_mod
    import llm.client as client_mod

    def _init(self, mock=mock_client):
        self.client = mock
        self.model = "mock-model"
        self._langfuse_prompt = None

    base_mod.BaseAgent.__init__ = _init  # type: ignore[method-assign]
    client_mod.OpenAI = lambda *a, **k: mock_client  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default="", help="agent name, or 'all'")
    ap.add_argument("--list", action="store_true", help="list evaluable agents")
    ap.add_argument("--mock", action="store_true", help="deterministic fake LLM (no API key)")
    ap.add_argument("--real", action="store_true", help="real LLM (OPENROUTER_API_KEY); real samples only")
    ap.add_argument("--n", type=int, default=None, help="cap cases per agent")
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="score gold vs gold (no agent invoke) — scorer wiring check",
    )
    ap.add_argument("--json", action="store_true", help="print JSON only")
    args = ap.parse_args()

    if args.list or not args.agent:
        print("Evaluable agents:")
        for name in LLM_AGENTS:
            print(f"  {name}")
        if not args.agent:
            return 0 if args.list else 2

    mock = not args.real
    if args.mock:
        mock = True
    if mock:
        _install_mocks()

    agents = list(LLM_AGENTS) if args.agent == "all" else [args.agent]
    reports = []
    for name in agents:
        report = evaluate_agent(
            name,
            mock=mock,
            n=args.n,
            invoke=not args.self_check,
        )
        reports.append(report)
        if not args.json:
            metrics = report["metrics"]
            print(
                f"{name}: n={metrics['n']} errors={report['errors']} "
                f"class_acc={metrics['class_accuracy']} "
                f"extract={metrics['extraction_overall_mean']} "
                f"overlap={metrics['token_overlap_mean']}"
            )

    if args.json:
        json.dump(reports if len(reports) > 1 else reports[0], sys.stdout, indent=2, default=str)
        print()
    return 0 if all(r["errors"] == 0 or r["metrics"]["n"] > 0 for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
