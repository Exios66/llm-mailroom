"""#279: a stated ``confidence`` of 0.0 is the model's answer, not a missing value.

``_SpecialistBase.extract`` / ``extract_chunked`` replace a *missing* confidence with the
evidence share (the fraction of schema fields found). A stated 0.0 must survive: promoting
it to a higher evidence score could land an extraction the model called worthless above the
judge band. Only an omitted key (or a null) takes the evidence fallback.
"""

from __future__ import annotations

import pytest

_FILLED = {
    "document_name": "MSA",
    "parties": ["Acme Corp", "Beta LLC"],
    "governing_law": "Delaware",
    "effective_date": "2021-03-05",
}
_DOC = "Master Services Agreement between Acme Corp and Beta LLC.\n\n" + ("filler text " * 40)
_CHUNK_DOC = "\n\n".join(("WINDOW%d " % i) * 50 for i in range(3))


def _agent(client, cls_name):
    if cls_name == "contracts":
        from agents.contracts_specialist import ContractsSpecialist as cls
    else:
        from agents.merger_agreement_specialist import MergerAgreementSpecialist as cls
    agent = cls()
    agent.client = client
    agent.model = "test-model"
    return agent


def _stub(agent, payloads):
    queue = list(payloads)
    agent._call_structured = lambda *a, **k: dict(queue.pop(0))
    return agent


@pytest.mark.parametrize("cls_name", ["contracts", "merger"])
def test_stated_zero_confidence_is_kept(mock_openai_client, cls_name):
    agent = _stub(_agent(mock_openai_client, cls_name), [{**_FILLED, "confidence": 0.0}])
    assert agent.extract(_DOC)["confidence"] == 0.0


@pytest.mark.parametrize("cls_name", ["contracts", "merger"])
@pytest.mark.parametrize("missing", ["omitted", "null"])
def test_omitted_confidence_falls_back_to_evidence(mock_openai_client, cls_name, missing):
    payload = dict(_FILLED)
    if missing == "null":
        payload["confidence"] = None
    agent = _stub(_agent(mock_openai_client, cls_name), [payload])
    result = agent.extract(_DOC)
    assert result["confidence"] > 0.0
    assert result["confidence"] == round(agent._evidence_confidence(payload), 4)


def test_stated_zero_survives_the_chunked_path(mock_openai_client):
    agent = _stub(
        _agent(mock_openai_client, "contracts"),
        [{**_FILLED, "confidence": 0.0}, {**_FILLED, "confidence": 0.9}, {**_FILLED, "confidence": 0.8}],
    )
    result = agent.extract_chunked(_CHUNK_DOC, chunk_chars=400, overlap_chars=0)
    assert result["confidence"] == 0.0  # the weakest window, as stated


def test_omitted_window_confidence_uses_evidence_in_the_chunked_path(mock_openai_client):
    agent = _stub(
        _agent(mock_openai_client, "contracts"),
        [dict(_FILLED), {**_FILLED, "confidence": 0.9}, {**_FILLED, "confidence": 0.8}],
    )
    result = agent.extract_chunked(_CHUNK_DOC, chunk_chars=400, overlap_chars=0)
    assert 0.0 < result["confidence"] < 0.8  # the evidence share of the first window


@pytest.mark.parametrize(
    "agent_path",
    [
        "agents.corporate_records_specialist:CorporateRecordsSpecialist",
        "agents.correspondence_specialist:CorrespondenceSpecialist",
        "agents.insurance_claims_specialist:InsuranceClaimsSpecialist",
    ],
)
def test_pipeline_agents_keep_the_model_confidence(mock_openai_client, agent_path):
    """Corporate / correspondence / insurance never had the evidence fallback: the stated value stands."""
    import importlib

    module, name = agent_path.split(":")
    agent = getattr(importlib.import_module(module), name)()
    agent.client = mock_openai_client
    agent.model = "test-model"
    for stated in (0.0, 0.42):
        agent._call_structured = lambda *a, _s=stated, **k: {"confidence": _s}
        assert agent.extract(_DOC)["confidence"] == stated
