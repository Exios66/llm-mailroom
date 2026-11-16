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
# Long enough to be checkable, and in no window's text.
_FABRICATED = "Exclusivity: Supplier grants Buyer worldwide exclusive distribution rights in perpetuity"


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
    item = _NONCOMPETE + " unless the Buyer consents"
    agent = _contracts(mock_openai_client, [
        _window(),
        _window(cuad_clauses=[item], entries=[{
            "field": "cuad_clauses",
            "evidence": "the Seller shall not compete with the Buyer",
            "section_ref": "7.2",
        }]),
        _window(),
    ])
    assert _run(agent)["cuad_clauses"] == [item]


def test_reasoning_quote_must_occur_in_the_asserting_window():
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    claim = "Termination: The seller may terminate at any time"
    result = merge_windows_by_evidence([
        (_window(cuad_clauses=[claim], entries=[{
            "field": "cuad_clauses", "evidence": "The seller may terminate at any time",
        }]), "This window says nothing about termination."),
        (_window(), "Another unrelated window."),
    ])
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"]["cuad_clauses"] == [claim]


def test_fabricated_scalar_quote_has_no_evidence_priority():
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    result = merge_windows_by_evidence([
        (_window(0.6, governing_law="Delaware", entries=[{
            "field": "governing_law", "evidence": "Governed by Delaware law",
        }]), "This window contains only payment terms."),
        (_window(0.9, governing_law="New York"), "New York law governs this agreement."),
    ])
    assert result["governing_law"] == "New York"


def test_bare_field_level_reasoning_entry_does_not_evidence_every_item(mock_openai_client, monkeypatch):
    # The model emits one entry per populated field, so a field-level entry alone
    # says nothing about an individual item.
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=[_FABRICATED],
                entries=[{"field": "cuad_clauses", "evidence": "see section 3"}]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"]["cuad_clauses"] == [_FABRICATED]


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
        _window(cuad_clauses=[_FABRICATED]), _window(), _window(),
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
        _window(cuad_clauses=[_FABRICATED]), _window(), _window(),
    ])
    assert _run(agent)["cuad_clauses"] == [_FABRICATED]


def test_unsupported_claims_never_reach_the_judge():
    from graph.build_graph import _clean_fields_for_judge

    cleaned = _clean_fields_for_judge({
        "cuad_clauses": [], "_unsupported_chunk_claims": {"cuad_clauses": ["x"]},
        "_chunk_merge_policy": "evidence",
    })
    assert cleaned == {"cuad_clauses": []}


# --- scope, unverifiable items, tolerant matching (review of the first cut) ---


def _run_text(agent, doc, **kw):
    return agent.extract_chunked(doc, chunk_chars=400, overlap_chars=0, **kw)


def test_entity_and_label_lists_survive_evidence_policy(mock_openai_client, monkeypatch):
    # Window 1's text names the parties, but the 12-char span rule used to wipe
    # ``parties`` (and every short label list) under ``evidence``.
    _config(monkeypatch, policy="evidence", min_windows=2)
    doc = "\n\n".join([
        "This agreement is made between Acme Inc and Beta LLC. " + "ALPHA " * 40,
        "BETA " * 50,
        "GAMMA " * 50,
    ])
    agent = _contracts(mock_openai_client, [
        _window(parties=["Acme Inc", "Beta LLC"], party_roles=["Acme Inc: licensor"],
                signatories=["Jane Roe"], keywords=["exclusivity", "indemnity"],
                maud_clauses=["No-Shop: Yes"]),
        _window(parties=["Gamma Corp invented"], keywords=["unseen keyword here"]),
        _window(),
    ])
    result = _run_text(agent, doc)
    assert result["parties"] == ["Acme Inc", "Beta LLC", "Gamma Corp invented"]
    assert result["party_roles"] == ["Acme Inc: licensor"]
    assert result["signatories"] == ["Jane Roe"]
    assert result["keywords"] == ["exclusivity", "indemnity", "unseen keyword here"]
    assert result["maud_clauses"] == ["No-Shop: Yes"]
    assert "_unsupported_chunk_claims" not in result


def test_other_specialists_entity_lists_survive(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    queue = [
        {"additional_recipients": ["Carol Ng"], "keywords": ["renewal notice"],
         "action_items": ["fabricated action nobody wrote down"], "confidence": 0.9},
        {"confidence": 0.9},
        {"confidence": 0.9},
    ]
    agent.extract = lambda *a, **k: queue.pop(0)
    result = agent.extract_chunked(_DOC, chunk_chars=400, overlap_chars=0)
    assert result["additional_recipients"] == ["Carol Ng"]
    assert result["keywords"] == ["renewal notice"]
    assert result["action_items"] == []  # in scope and checkable: still filtered
    assert result["_unsupported_chunk_claims"] == {
        "action_items": ["fabricated action nobody wrote down"]
    }


def test_curly_quotes_dashes_and_apostrophes_match_straight_ones(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    doc = "\n\n".join([
        "ALPHA " * 40,
        "Exclusivity: the Seller\u2019s obligation is \u201cexclusive\u201d \u2014 during the term. " + "BETA " * 30,
        "GAMMA " * 40,
    ])
    straight = 'Exclusivity: the Seller\'s obligation is "exclusive" - during the term'
    agent = _contracts(mock_openai_client, [_window(), _window(cuad_clauses=[straight]), _window()])
    result = _run_text(agent, doc)
    assert result["cuad_clauses"] == [straight]
    assert "_unsupported_chunk_claims" not in result


def test_straight_window_text_matches_curly_item(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    doc = "\n\n".join([
        "ALPHA " * 40,
        'The Seller\'s obligation is "exclusive" for the whole term. ' + "BETA " * 30,
        "GAMMA " * 40,
    ])
    curly = "Exclusivity: the Seller\u2019s obligation is \u201cexclusive\u201d for the whole term"
    agent = _contracts(mock_openai_client, [_window(), _window(cuad_clauses=[curly]), _window()])
    assert _run_text(agent, doc)["cuad_clauses"] == [curly]


def test_short_unverifiable_in_scope_item_survives(mock_openai_client, monkeypatch):
    # No fragment reaches _MIN_SPAN_CHARS, so there is nothing to check: keep it.
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Governing Law: Delaware", "Audit Rights: yes"]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == ["Governing Law: Delaware", "Audit Rights: yes"]
    assert "_unsupported_chunk_claims" not in result


def test_long_fabricated_in_scope_item_is_still_filtered(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2)
    agent = _contracts(mock_openai_client, [
        _window(cuad_clauses=["Governing Law: Delaware", _FABRICATED]), _window(), _window(),
    ])
    result = _run(agent)
    assert result["cuad_clauses"] == ["Governing Law: Delaware"]
    assert result["_unsupported_chunk_claims"] == {"cuad_clauses": [_FABRICATED]}


def test_fields_allow_list_widens_the_scope(mock_openai_client, monkeypatch):
    # Opting ``keywords`` in puts it under the verbatim check; ``cuad_clauses`` is no
    # longer in the list, so its fabricated item passes through.
    _config(monkeypatch, policy="evidence", min_windows=2, fields=["keywords"])
    agent = _contracts(mock_openai_client, [
        _window(keywords=["an unquoted invented keyword"], cuad_clauses=[_FABRICATED]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["keywords"] == []
    assert result["cuad_clauses"] == [_FABRICATED]
    assert result["_unsupported_chunk_claims"] == {"keywords": ["an unquoted invented keyword"]}


def test_empty_fields_list_filters_nothing(mock_openai_client, monkeypatch):
    _config(monkeypatch, policy="evidence", min_windows=2, fields=[])
    agent = _contracts(mock_openai_client, [_window(cuad_clauses=[_FABRICATED]), _window(), _window()])
    result = _run(agent)
    assert result["cuad_clauses"] == [_FABRICATED]
    assert "_unsupported_chunk_claims" not in result
    assert result["_chunk_merge_policy"] == "evidence"  # scalars still use the evidence pick


def test_by_agent_fields_override(mock_openai_client, monkeypatch):
    from langchain_agents.specialist_agents import chunk_merge_settings

    _config(monkeypatch, policy="evidence", min_windows=2, fields=["cuad_clauses"],
            by_agent={"contracts_specialist": {"fields": ["keywords"]}})
    assert chunk_merge_settings("contracts_specialist").fields == frozenset({"keywords"})
    assert chunk_merge_settings("merger_agreement_specialist").fields == frozenset({"cuad_clauses"})
    agent = _contracts(mock_openai_client, [
        _window(keywords=["an unquoted invented keyword"], cuad_clauses=[_FABRICATED]),
        _window(),
        _window(),
    ])
    result = _run(agent)
    assert result["keywords"] == []
    assert result["cuad_clauses"] == [_FABRICATED]


def test_fields_default_when_unset_or_malformed(monkeypatch):
    from langchain_agents.specialist_agents import _DEFAULT_EVIDENCE_FIELDS, chunk_merge_settings

    _config(monkeypatch, policy="evidence", min_windows=2)  # no ``fields`` key
    assert chunk_merge_settings("contracts_specialist").fields == frozenset(_DEFAULT_EVIDENCE_FIELDS)
    _config(monkeypatch, policy="evidence", min_windows=2, fields="cuad_clauses")  # not a list
    assert chunk_merge_settings("contracts_specialist").fields == frozenset(_DEFAULT_EVIDENCE_FIELDS)


def test_default_taxonomy_fields_are_claim_lists_only():
    from pipeline.config import load_config

    fields = set(load_config()["chunking"]["merge"]["fields"])
    assert fields == {"cuad_clauses", "claim_checklist", "action_items", "denial_reasons"}
    for entity_or_label in ("parties", "party_roles", "signatories", "keywords", "maud_clauses",
                            "merger_sub", "financial_advisors", "legal_counsel",
                            "additional_recipients", "supporting_documents"):
        assert entity_or_label not in fields


@pytest.mark.parametrize("field", ["action_items", "denial_reasons"])
def test_free_text_colon_prefix_must_also_be_in_source(field):
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    source = "the Seller shall not compete with the Buyer for five years"
    claim = "Invented obligation imposed on the insurer: " + source
    merged = merge_windows_by_evidence([({field: [claim], "confidence": 0.9}, source)])
    assert merged[field] == []
    assert merged["_unsupported_chunk_claims"][field] == [claim]


def test_scalar_without_reasoning_removes_the_losing_witness():
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    old = _window(confidence=0.5, governing_law="New York", entries=[
        {"field": "governing_law", "evidence": "not present in this source"}])
    new = _window(confidence=0.9, governing_law="Delaware")
    result = merge_windows_by_evidence([(old, "a source"), (new, "a different source")])
    assert result["governing_law"] == "Delaware"
    assert not any(e.get("field") == "governing_law" for e in result["reasoning"]["entries"])


@pytest.mark.parametrize("source", ["non-exclusive to Buyer", "preexclusive to Buyer", "exclusive to Buyership"])
def test_evidence_does_not_match_inside_a_word(source):
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    claim = "Exclusivity: exclusive to Buyer"
    result = merge_windows_by_evidence([({"cuad_clauses": [claim], "confidence": 0.9}, source)])
    assert result["cuad_clauses"] == []
    assert result["_unsupported_chunk_claims"]["cuad_clauses"] == [claim]


def test_evidence_accepts_a_whole_phrase_next_to_punctuation():
    from langchain_agents.specialist_agents import merge_windows_by_evidence

    claim = "Exclusivity: exclusive to Buyer"
    result = merge_windows_by_evidence([({"cuad_clauses": [claim], "confidence": 0.9}, 'It is "exclusive to Buyer", as agreed.')])
    assert result["cuad_clauses"] == [claim]
