"""Guard the evidence-based confidence calibration rule in agent prompts.

The sorter and every specialist must instruct the model to derive its
`confidence` score from the evidence in the current document (fields found,
nulls, truncation, ambiguity) and never anchor on a fixed high value such as
0.90/0.95. Without these rules the models default to round high scores, which
defeats the confidence-threshold routing in graph/routing.py.
"""

import re

import pytest

from llm.prompts import prompt_templates


AGENT_PROMPTS_WITH_CONFIDENCE = [
    "sorter",
    "contracts_specialist",
    "corporate_records_specialist",
    "due_diligence_specialist",
    "correspondence_specialist",
    "compliance_specialist",
    "court_opinions_specialist",
    "insurance_claims_specialist",
]

ANTI_ANCHOR = "never default to a fixed high value (e.g. 0.90 or 0.95)"
# The vendored LangChain prompts (sorter_v5 / contracts_specialist_v11,
# eval-validated upstream) phrase the same rule more strongly: a high score is
# acceptable only when the reasoning cites the concrete evidence.
ANTI_ANCHOR_VARIANT = (
    "high score (0.90+) is acceptable only when the reasoning cites the concrete evidence"
)
EVIDENCE_BASE = "derived from the evidence"
# ...which sorter_v5 words as "Derive the confidence from the evidence in THIS
# document".
EVIDENCE_VARIANT = "from the evidence in this document"


def _normalize(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt).lower()


@pytest.mark.parametrize("agent_name", AGENT_PROMPTS_WITH_CONFIDENCE)
def test_confidence_calibration_rule_present(agent_name):
    prompt = _normalize(prompt_templates()[agent_name])
    assert any(anchor in prompt for anchor in (ANTI_ANCHOR, ANTI_ANCHOR_VARIANT)), (
        f"{agent_name} prompt must forbid anchoring confidence on a fixed high value"
    )
    assert any(evidence in prompt for evidence in (EVIDENCE_BASE, EVIDENCE_VARIANT)), (
        f"{agent_name} prompt must require evidence-derived confidence"
    )
