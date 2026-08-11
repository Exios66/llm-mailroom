"""LegalBench task registry.

Two task families, per the LegalBench taxonomy:

- ``binary_answer`` — yes/no questions over contracts (the CUAD
  contract-QA family; 20,910 local questions).
- ``multiclass_classification`` — assign one of 25 CUAD contract families
  (+ ``other``) to a contract (200 locally-labeled documents).

Each task declares its loader, prompt version, answer schema, the model-call
shape, and its deterministic scorer. The runner is task-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import data as _data
from . import scoring as _scoring

# Task ids as they appear in the experiment log `task` field (site dispatch).
TASK_BINARY = "legalbench_binary_answer"
TASK_MULTICLASS = "legalbench_multiclass_classification"

# Per-task prompt versions — the experiment identity (never mutate after a
# run has been logged; add a new version instead).
PROMPT_CONTRACT_QA_V1 = "legalbench_contract_qa_v1"
PROMPT_FAMILY_V1 = "legalbench_family_classification_v1"


@dataclass(frozen=True)
class LegalBenchTask:
    id: str  # e.g. "contract_qa"
    kind: str  # TASK_BINARY / TASK_MULTICLASS
    label: str
    prompt_version: str
    classes: tuple[str, ...]  # answer space (yes/no) or family keys
    loader: Callable[[int, int], list[dict[str, Any]]]
    scorer: Callable[[list[dict[str, Any]]], dict[str, Any]]
    # model-call shape: (agent, row) -> prediction dict
    call: Callable[["object", dict[str, Any]], dict[str, Any]]
    # extract (row, prediction) -> (predicted_label, confidence)
    extract: Callable[[dict[str, Any], dict[str, Any]], tuple[str, Any, str]]
    data_source_label: str = ""
    ground_truth_label: str = ""


def _call_binary(agent, row):
    return agent.answer_binary(row["question"], row["document_text"])


def _call_family(agent, row):
    return agent.classify_family(row["text"])


def _extract_binary(row, pred):
    ans = _data._normalize_prediction(pred.get("answer") if isinstance(pred, dict) else pred)
    conf = pred.get("confidence") if isinstance(pred, dict) else None
    evid = str(pred.get("evidence") or "") if isinstance(pred, dict) else ""
    return (ans or "error"), conf, evid


def _extract_family(row, pred):
    raw = pred.get("family") if isinstance(pred, dict) else pred
    conf = pred.get("confidence") if isinstance(pred, dict) else None
    reasoning = str(pred.get("reasoning") or "") if isinstance(pred, dict) else ""
    if isinstance(raw, str):
        fam = raw.strip().lower().replace(" ", "_")
        # lenient: accept label aliases back to keys
        for key, label in _family_labels().items():
            if fam in (key, label.lower().replace(" ", "_")):
                fam = key
                break
    else:
        fam = "error"
    return fam, conf, reasoning


def _family_labels() -> dict[str, str]:
    from langchain_agents.sorter_agent import CONTRACT_SUBTYPES

    return {s["key"]: s["label"] for s in CONTRACT_SUBTYPES}


def _binary_classes() -> tuple[str, ...]:
    return ("yes", "no")


def _family_classes() -> tuple[str, ...]:
    from langchain_agents.sorter_agent import CONTRACT_SUBTYPES

    return tuple([s["key"] for s in CONTRACT_SUBTYPES] + ["other"])


TASKS: dict[str, LegalBenchTask] = {
    "contract_qa": LegalBenchTask(
        id="contract_qa",
        kind=TASK_BINARY,
        label="Contract QA — CUAD binary-answer (yes/no)",
        prompt_version=PROMPT_CONTRACT_QA_V1,
        classes=_binary_classes(),
        loader=lambda n, seed: _data.load_cuad_qa(n, seed),
        scorer=_scoring.score_binary,
        call=_call_binary,
        extract=_extract_binary,
        data_source_label="local:cuad",
        ground_truth_label="cuad_v1_annotations",
    ),
    "family_classification": LegalBenchTask(
        id="family_classification",
        kind=TASK_MULTICLASS,
        label="Contract-family classification — CUAD 25 families (+other)",
        prompt_version=PROMPT_FAMILY_V1,
        classes=_family_classes(),
        loader=lambda n, seed: _data.load_family_rows(n, seed),
        scorer=_scoring.score_multiclass,
        call=_call_family,
        extract=_extract_family,
        data_source_label="local:cuad",
        ground_truth_label="cuad_subtype_families",
    ),
}


def get_task(task_id: str) -> LegalBenchTask:
    try:
        return TASKS[task_id]
    except KeyError:
        raise KeyError(
            f"unknown LegalBench task {task_id!r}; available: {sorted(TASKS)}"
        ) from None


def task_help() -> str:
    lines = []
    for task in TASKS.values():
        lines.append(f"  {task.id:<22} {task.label}")
    return "\n".join(lines)
