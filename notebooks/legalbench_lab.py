"""LegalBench lab bench — the reusable module behind
``notebooks/12_legalbench.ipynb``.

Runs the REAL ``legalbench.runner.run_task`` against a committed miniature
CUAD fixture (2 contracts × 3 clause categories) so the notebook is
network-free and does not require ``data/cuad/``. Honesty: this is the same
synthetic shape the unit tests use, not the 20,910-question full corpus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legalbench"
MINI_CUAD = FIXTURES / "cuad_mini.json"
MINI_CONTRACTS = FIXTURES / "contracts"


def task_table() -> list[dict[str, Any]]:
    """Live registry from ``legalbench.tasks`` — ids, kinds, prompt versions."""
    from legalbench.tasks import TASKS

    rows = []
    for task in TASKS.values():
        rows.append(
            {
                "id": task.id,
                "kind": task.kind,
                "label": task.label,
                "prompt_version": task.prompt_version,
                "n_classes": len(task.classes),
                "classes_head": list(task.classes)[:8],
            }
        )
    return rows


def load_mini_qa(n: int = 6, seed: int = 1) -> list[dict[str, Any]]:
    from legalbench.data import load_cuad_qa

    return load_cuad_qa(n, seed=seed, cuad_path=MINI_CUAD, min_text_chars=10)


def load_mini_family(n: int = 2, seed: int = 3) -> list[dict[str, Any]]:
    from legalbench.data import load_family_rows

    return load_family_rows(n, seed=seed, contracts_dir=MINI_CONTRACTS)


def run_mini(task_id: str, *, n: int = 6, seed: int = 1) -> dict[str, Any]:
    """Deterministic mock LegalBench run on the miniature corpus.

    ``trace_enabled=False`` so the notebook never talks to Langfuse.
    Returns the ``RunResult`` as a plain dict of headline scores + per-row
    correctness (the shape ``print_summary`` uses).
    """
    from legalbench.runner import run_task

    if task_id == "contract_qa":
        rows = load_mini_qa(n=n, seed=seed)
    elif task_id == "family_classification":
        rows = load_mini_family(n=n, seed=seed)
    else:
        raise KeyError(task_id)
    result = run_task(
        task_id,
        n=n,
        seed=seed,
        mock=True,
        trace_enabled=False,
        rows=rows,
    )
    return {
        "task": result.task_id,
        "kind": result.kind,
        "model": result.model,
        "n": len(result.results),
        "scores": dict(result.scores),
        "rows": [
            {
                "expected": r.get("expected"),
                "predicted": r.get("predicted"),
                "correct": r.get("correct"),
                "status": r.get("status"),
            }
            for r in result.results
        ],
        "honesty": "mock/mock-legalbench on a 2-contract fixture — not the full CUAD corpus.",
    }
