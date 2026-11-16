"""#255: opt-in evidence policy for the chunked-extraction merge.

``chunking.merge.policy`` is ``union`` (default: every window's list items,
first non-null scalar) or ``evidence`` (precision filter: a list item survives
only when its window evidences it or >= ``min_windows`` windows assert it; a
scalar comes from the window that evidences it, then the more confident one).
"""

from __future__ import annotations

import copy

import pytest

# Three windows, one paragraph each (chunk_chars=400, no overlap).
_W1 = "ALPHA " * 50
_W2 = "BETA clause: the Seller shall not compete with the Buyer for five years. " + "BETA " * 40
_W3 = "GAMMA recital: this Agreement is effective as of March 5, 2021. " + "GAMMA " * 40
_DOC = "\n\n".join(p[:300] for p in (_W1, _W2, _W3))

_NONCOMPETE = "Non-Compete: the Seller shall not compete with the Buyer for five years"


def _config(monkeypatch, **merge):
    from pipeline import config as cfg_mod

    cfg = copy.deepcopy(cfg_mod.load_config())
    if merge:
        cfg["chunking"]["merge"] = merge
    else:
        cfg["chunking"].pop("merge", None)
    monkeypatch.setattr(cfg_mod, "load_config", lambda: cfg)
    return cfg


def _contracts(client, windows):
    """A contracts specialist whose i-th window returns ``windows[i]``."""
    from agents.contracts_specialist import ContractsSpecialist

    agent = ContractsSpecialist()
    agent.client = client
    agent.model = "test-model"
    queue = [copy.deepcopy(w) for w in windows]
    agent._call_structured = lambda *a, **k: queue.pop(0)
    return agent


def _window(confidence=0.9, entries=None, **fields):
    out = {"confidence": confidence, **fields}
    if entries is not None:
        out["reasoning"] = {"summary": "", "entries": entries}
    return out


def _run(agent):
    return agent.extract_chunked(_DOC, chunk_chars=400, overlap_chars=0)


def test_default_taxonomy_policy_is_union():
    from pipeline.config import load_config

    merge = load_config()["chunking"]["merge"]
    assert merge["policy"] == "union"
    assert merge["min_windows"] == 2


def test_union_keeps_unevidenced_single_window_claim_and_first_scalar(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="union", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(effective_date="2020-01-01"),
        _window(cuad_clauses=["Governing Law: laws of Delaware"]),
        _window(effective_date="2021-03-05",
                entries=[{"field": "effective_date", "evidence": "effective as of March 5, 2021"}]),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == ["Governing Law: laws of Delaware"]
    assert result["effective_date"] == "2020-01-01"
    assert "_unsupported_chunk_claims" not in result
    assert "_chunk_merge_policy" not in result


def test_union_output_is_exactly_the_pairwise_fold(mock_openai_client, monkeypatch):
    from langchain_agents.specialist_agents import (
        _SpecialistBase,
        finalize_chunked_merge,
        normalize_extraction,
    )

    _config(monkeypatch)  # no merge block at all: still union
    windows = [
        _window(0.8, parties=["A", "B"], effective_date=None, cuad_clauses=["X: one"]),
        _window(0.6, parties=["b ", "C"], effective_date="2020-01-01", cuad_clauses=["x:  ONE", "Y: two"]),
        _window(0.9, parties=[], effective_date="2021-01-01", governing_law="Delaware"),
    ]
    agent = _contracts(mock_openai_client, windows)
    expected = None
    for w in windows:
        w = normalize_extraction(copy.deepcopy(w), agent.schema)
        expected = w if expected is None else _SpecialistBase._merge_extractions(expected, w)
    expected = finalize_chunked_merge(
        expected, agent_name="contracts_specialist", n_chunks=3, failed=0
    )
    assert _run(agent) == expected


def test_evidence_excludes_unevidenced_single_window_claim_and_records_it(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Exclusivity: Supplier is the exclusive provider"]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"] == {
        "cuad_clauses": ["Exclusivity: Supplier is the exclusive provider"]
    }
    assert result["_chunk_merge_policy"] == "evidence"


def test_evidence_keeps_single_window_claim_whose_span_is_in_its_window(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(),
        _window(cuad_clauses=[_NONCOMPETE]),
        _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == [_NONCOMPETE]
    assert "_unsupported_chunk_claims" not in result


def test_span_must_be_in_the_window_that_asserted_it(mock_openai_client, monkeypatch):
    # The quote sits in window 2 but only window 3 asserts the clause.
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [_window(), _window(), _window(cuad_clauses=[_NONCOMPETE])])
    result = _run(agent)
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"]["cuad_clauses"] == [_NONCOMPETE]


def test_evidence_keeps_claim_backed_by_its_reasoning_entry(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    item = "Audit Rights: Licensee may audit Licensor's books once per year on notice"
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=[item], entries=[{
            "field": "cuad_clauses",
            "evidence": "Licensee may audit Licensor's books once per year",
            "section_ref": "7.2",
        }]),
        _window(),
        _window(),
    ])
    assert _run(agent)["cuad_clauses"] == [item]


def test_bare_field_level_reasoning_entry_does_not_evidence_every_item(mock_openai_client, monkeypatch):
    # The model emits one entry per populated field, so a field-level entry alone
    # says nothing about an individual item.
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Exclusivity: made up"],
                entries=[{"field": "cuad_clauses", "evidence": "see section 3"}]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"]["cuad_clauses"] == ["Exclusivity: made up"]


@pytest.mark.parametrize("min_windows,kept", [(2, True), (3, False)])
def test_evidence_keeps_claim_asserted_by_k_windows(mock_openai_client, monkeypatch, min_windows, kept):
    _config(monkeypatch, policy="evidence", min_windows=min_windows)
    claim = "Exclusivity: Supplier is the exclusive provider"
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=[claim]),
        _window(cuad_clauses=["exclusivity:  SUPPLIER is the exclusive provider"]),
        _window(),
    ])
    result = _run(agent)
    assert (result["cuad_clauses"] == [claim]) is kept
    assert ("_unsupported_chunk_claims" not in result) is kept


def test_evidence_scalar_from_evidenced_later_window_beats_earlier_guess(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(0.95, effective_date="2020-01-01"),
        _window(0.9),
        _window(0.7, effective_date="2021-03-05", entries=[{
            "field": "effective_date",
            "evidence": "effective as of March 5, 2021",
            "section_ref": "Recitals",
        }]),
    ])
    result = _run(agent)
    assert result["effective_date"] == "2021-03-05"
    entries = {e["field"]: e for e in result["reasoning"]["entries"]}
    assert entries["effective_date"]["section_ref"] == "Recitals"  # the winner's trace


def test_evidence_scalar_without_evidence_uses_confidence_then_order_never_null(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(0.6, governing_law="New York", term_length=None),
        _window(0.9, governing_law="Delaware", term_length="3 years"),
        _window(0.9, governing_law="Texas", term_length=None),
    ])
    result = _run(agent)
    assert result["governing_law"] == "Delaware"  # higher confidence; tie with Texas -> earlier
    assert result["term_length"] == "3 years"  # never a null


def test_evidence_all_null_scalar_stays_null(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [_window(), _window(), _window()])
    assert _run(agent)["effective_date"] is None


def test_evidence_confidence_is_still_the_minimum(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [_window(0.9), _window(0.55), _window(0.8)])
    assert _run(agent)["confidence"] == 0.55


def test_by_agent_override_selects_policy_per_specialist(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="union", min_windows=2,
            by_agent={"contracts_specialist": {"policy": "evidence"}})
    contracts = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Exclusivity: made up"]), _window(), _window(),
    ])
    assert _run(contracts)["cuad_clauses"] == []

    from agents.correspondence_specialist import CorrespondenceSpecialist

    other = CorrespondenceSpecialist()
    other.client = mock_openai_client
    other.model = "test-model"
    queue = [{"action_items": ["unevidenced"], "confidence": 0.9}, {"confidence": 0.9}, {"confidence": 0.9}]
    other.extract = lambda *a, **k: queue.pop(0)
    assert other.extract_chunked(_DOC, chunk_chars=400, overlap_chars=0)["action_items"] == ["unevidenced"]


def test_evidence_policy_on_the_pipeline_agent_path(mock_openai_client, monkeypatch):
    # corporate/correspondence/insurance carry no ``reasoning``; the verbatim check still applies.
    _config(monkeypatch, policy="evidence", min_windows=2)
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    queue = [
        {"action_items": ["invented action"], "confidence": 0.9},
        {"action_items": ["the Seller shall not compete with the Buyer for five years"], "confidence": 0.9},
        {"sender": "late", "confidence": 0.9},
    ]
    agent.extract = lambda *a, **k: queue.pop(0)
    result = agent.extract_chunked(_DOC, chunk_chars=400, overlap_chars=0)
    assert result["action_items"] == ["the Seller shall not compete with the Buyer for five years"]
    assert result["_unsupported_chunk_claims"] == {"action_items": ["invented action"]}


def test_unknown_policy_falls_back_to_union(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="bogus", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Exclusivity: made up"]), _window(), _window(),
    ])
    assert _run(agent)["cuad_clauses"] == ["Exclusivity: made up"]


def test_unsupported_claims_never_reach_the_judge():
    from graph.build_graph import _clean_fields_for_judge

    cleaned = _clean_fields_for_judge({
        "cuad_clauses": [], "_unsupported_chunk_claims": {"cuad_clauses": ["x"]},
        "_chunk_merge_policy": "evidence",
    })
    assert cleaned == {"cuad_clauses": []}
