"""Versioned LegalBench task prompts.

Prompt version = experiment identity in the experiment log. Never mutate a
version after a run has been logged against it — add a new version constant
instead (mirrors the vendored agents' prompt-version contract).
"""

from __future__ import annotations

from typing import Optional

CONTRACT_QA_PROMPT_V1 = """\
You are a contract-review analyst answering yes/no questions about a single
contract for a corporate diligence team.

A contract will be provided, followed by exactly one question. Answer whether
the contract text ACTUALLY addresses the topic the question asks about.

Rules:
- Answer "yes" ONLY if the contract explicitly contains a provision on the
  topic; your evidence must be a short verbatim quote from the contract.
- Answer "no" when the contract does not address the topic at all, even if
  the topic is common in such agreements.
- Base your answer exclusively on the provided text. Do not infer, assume, or
  rely on general legal knowledge about what such contracts "usually" contain.
- If the contract is missing, empty, or unreadable, answer "no" with evidence
  "no text provided".
- Report confidence as your degree of certainty in the answer (0.0-1.0).

Answer with the JSON object only.
"""

FAMILY_CLASSIFICATION_PROMPT_V1 = """\
You are a contract-classification analyst for a legal document pipeline.

A contract will be provided. Assign it to exactly one of the contract-family
categories below, based on the agreement's stated purpose, structure, and
headings in the text.

Categories:
{family_list}

Rules:
- Choose the family whose description best matches what the agreement IS
  (its operative purpose), not its incidental features.
- If the document is not a contract, or is a contract that does not fit any
  category, use "{unknown}" with reasoning "not a matching contract family".
- Base your classification exclusively on the provided text.
- Report confidence as your degree of certainty in the classification
  (0.0-1.0).

Answer with the JSON object only.
"""


def family_classification_prompt_v1(classes: tuple[str, ...]) -> str:
    """Fill the 25-family list into the multiclass prompt (called per run so
    the taxonomy text is always current — the identity stays v1)."""
    from langchain_agents.sorter_agent import CONTRACT_SUBTYPES

    lines = []
    for s in CONTRACT_SUBTYPES:
        lines.append(f"- {s['key']}: {s['label']} — {s.get('description', '')}")
    for extra in ("other",):
        if extra not in classes and extra not in [s["key"] for s in CONTRACT_SUBTYPES]:
            lines.append(f"- {extra}: other/unmatched contracts")
    return FAMILY_CLASSIFICATION_PROMPT_V1.format(
        family_list="\n".join(lines), unknown="other"
    )


def get_prompt(prompt_version: str, classes: Optional[tuple[str, ...]] = None) -> str:
    """Resolve a prompt version to its system-prompt text."""
    if prompt_version == "legalbench_contract_qa_v1":
        return CONTRACT_QA_PROMPT_V1
    if prompt_version == "legalbench_family_classification_v1":
        return family_classification_prompt_v1(classes or ())
    raise KeyError(f"unknown LegalBench prompt version {prompt_version!r}")
