"""Deterministic prompt/request contracts, never model-quality measurements."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json

import pytest

from agents.corporate_records_specialist import CorporateRecordsSpecialist
from langchain_agents import prompts as registry
from langchain_agents.prompt_candidates import corporate_flat9 as candidates
from langchain_agents.specialist_agents import CORPORATE_RECORDS_SCHEMA
from llm.frozen_v1 import load_specialist_v1
from llm.prompts import _bound_prompt_versions, prompt_templates
from llm_dojo_scoring import get_suite
from llm_dojo_scoring.prompts import get_prompt as dojo_prompt, get_prompt_version
from schemas.documents import CorporateRecordExtraction

CHARTER_KEY = "corporate_records_specialist_flat9_charter_v0"
POA_KEY = "corporate_records_specialist_flat9_poa_v0"
PARENT_SHA = "fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399"
EVAL_SHA = "253559fcb7ac6b36878453821a4fab0aa94228ad5add77d5aeabb06bb265134c"
FLAT9 = {
    "entity_name", "record_type", "effective_date", "signatories", "jurisdiction",
    "filing_number", "intent", "subject_matter", "keywords",
}
CHARTER_OLD = "intent corporate_action_approval or entity_formation as stated."
CHARTER_NEW = "intent entity_formation."
POA_OLD = "entity_name or principal name in entity_name when corp POA"
POA_NEW = (
    "entity_name as the owning company (registrant); never put an individual "
    "principal or attorney-in-fact in entity_name; null if no company is stated"
)
BOARD_RULE = (
    "- board_resolution: record_type other (board action); entity_name; "
    "signatories directors; effective_date; intent corporate_action_approval; "
    "subject_matter resolution topic."
)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _brief(text, subclass):
    return next(line for line in text.splitlines() if line.startswith(f"- {subclass}:"))


def _assert_charter_contract(text):
    """Also executed against frozen v6 in the scratch red gate."""
    line = _brief(text, "charter_amendment")
    assert "intent entity_formation." in line
    assert "corporate_action_approval" not in line


def _assert_poa_contract(text):
    """Text contract only: no claim about model-following success."""
    line = _brief(text, "powers_of_attorney")
    assert "entity_name as the owning company (registrant)" in line
    assert "never put an individual principal or attorney-in-fact in entity_name" in line
    assert "null if no company is stated" in line
    assert "entity_name or principal name" not in line


def _render(key, document):
    # Execute production's pure rendering methods, bypassing provider setup.
    agent = object.__new__(CorporateRecordsSpecialist)
    system, user = agent._structured_messages(
        document, CORPORATE_RECORDS_SCHEMA, system_prompt=registry.get_prompt(key)
    )
    return agent.system_prompt_with_skills(system), user


@pytest.mark.parametrize(("key", "constant", "old", "new", "digest", "chars", "byte_count"), [
    (CHARTER_KEY, "CHARTER_PROMPT", CHARTER_OLD, CHARTER_NEW,
     "7f7d0dbcb7f7184d2e22cab1f1d65d8c03b25a24b4dda587402f7f68d35992c3", 5826, 5850),
    (POA_KEY, "POA_PROMPT", POA_OLD, POA_NEW,
     "e3c692abf0902f9998ad22c9f0623785d8000127ec489d9c61df77baf994aaf2", 5953, 5977),
])
def test_literal_single_lesson_identity(key, constant, old, new, digest, chars, byte_count):
    parent = get_prompt_version("corporate_records_specialist_v6").text
    text = registry.get_prompt(key)
    assert _sha(parent) == PARENT_SHA
    assert parent.count(old) == 1
    assert text == parent.replace(old, new, 1)
    assert text == candidates.CANDIDATE_PROMPTS[key] == getattr(candidates, constant)
    assert (_sha(text), len(text), len(text.encode("utf-8"))) == (digest, chars, byte_count)
    metadata = candidates.CANDIDATE_METADATA[key]
    assert metadata == {
        "parent_family": "production",
        "parent_key": "corporate_records_specialist_v6",
        "parent_sha256": PARENT_SHA,
        "parent_dojo_commit": "3eb56be1c445b2f71c461ffa5f26660ca8516f98",
        "pipeline_source_commit": "85a0009ac3efb86aaae403e52afb75355b10d409",
        "replacement_count": 1,
        "sha256": digest,
        "chars": chars,
        "utf8_bytes": byte_count,
        "status": "offline_unmeasured_not_live",
    }
    # Freeze complete literal bytes, not a .replace() of a future catalog head.
    module = ast.parse(inspect.getsource(candidates))
    value = next(node.value for node in module.body if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == constant for t in node.targets))
    assert isinstance(value, ast.Constant) and value.value == text


def test_charter_candidate_contract():
    _assert_charter_contract(registry.get_prompt(CHARTER_KEY))


def test_poa_candidate_contract():
    _assert_poa_contract(registry.get_prompt(POA_KEY))


def test_each_lesson_stays_separate_and_parent_defects_are_still_visible():
    parent = load_specialist_v1("corporate_records_specialist")
    assert CHARTER_OLD in parent and POA_OLD in parent
    assert POA_OLD in registry.get_prompt(CHARTER_KEY)
    assert CHARTER_OLD in registry.get_prompt(POA_KEY)


def test_keys_are_explicit_local_experiments_and_not_canonical_v8_v9():
    assert set(candidates.CANDIDATE_PROMPTS) == {CHARTER_KEY, POA_KEY}
    for key in (CHARTER_KEY, POA_KEY):
        assert key in registry.list_prompts()
        with pytest.raises(KeyError):
            registry.get_prompt(key + "_typo")
        with pytest.raises(KeyError):
            get_prompt_version(key)
    assert "corporate_records_specialist_v8" not in registry.PROMPT_VERSIONS
    assert "corporate_records_specialist_v9" not in registry.PROMPT_VERSIONS


def test_live_eval_defaults_and_managed_sync_catalog_remain_frozen():
    live = load_specialist_v1("corporate_records_specialist")
    assert _sha(live) == PARENT_SHA
    assert registry.get_prompt("corporate_records_specialist") == live
    assert _sha(dojo_prompt("corporate_records_specialist", family="production").text) == PARENT_SHA
    assert _sha(dojo_prompt("corporate_records_specialist", family="production_prompts").text) == EVAL_SHA
    assert _sha(get_prompt_version("corporate_records_specialist_v7").text) == EVAL_SHA
    assert registry.DEFAULT_PROMPT_VERSION == "sorter"
    assert _bound_prompt_versions()["corporate_records_specialist"] == "frozen_v1"
    # This is the catalog imported by scripts/sync_prompts.py, not the legacy
    # uppercase all-version history exporter in langchain_agents.prompts.
    templates = prompt_templates()
    assert templates["corporate_records_specialist"] == live
    assert not set(candidates.CANDIDATE_PROMPTS).intersection(templates)
    assert not set(candidates.CANDIDATE_PROMPTS.values()).intersection(templates.values())
    assert object.__new__(CorporateRecordsSpecialist).system_prompt() == live


@pytest.mark.parametrize("key", [CHARTER_KEY, POA_KEY])
def test_actual_rendered_request_stays_flat9(key):
    document = "CORPORATE RECORD: a literal offline request probe."
    system, user = _render(key, document)
    assert system.startswith(registry.get_prompt(key) + "\n\nOutput must be a single json object")
    assert "## Skill reference:" in system
    assert user.startswith(document + "\n\nReturn ONLY a valid json object")
    schema = json.loads(user.split("JSON schema:\n", 1)[1])
    assert schema == CORPORATE_RECORDS_SCHEMA
    assert set(schema["properties"]) == set(schema["required"]) == FLAT9
    assert schema["additionalProperties"] is False
    assert _sha(json.dumps(schema, sort_keys=True, separators=(",", ":"))) == (
        "b268b685c61466ec02d6328ffd64848011afb0c9111573bace34a47ac89c1628"
    )
    assert set(CorporateRecordExtraction.model_fields) == FLAT9


@pytest.mark.parametrize("key", [CHARTER_KEY, POA_KEY])
def test_genuine_board_resolution_keeps_approval_rule_and_request(key):
    document = (
        "BOARD RESOLUTION OF ACME CORPORATION. The board resolves to approve "
        "the purchase of a new office. Approved and signed by director Jane Doe."
    )
    system, user = _render(key, document)
    assert BOARD_RULE in system
    assert document in user
    # These literal labels are scorer controls, not model-produced outputs.
    out = get_suite("corporate_records_specialist").score(
        {"intent": "corporate_action_approval"}, {"intent": "corporate_action_approval"},
        detailed=True, doc_subclass="board_resolution",
    )
    assert out["extraction"].field_scores["intent"] == 1.0


def test_charter_reciting_board_approval_still_has_formation_rule():
    document = (
        "CERTIFICATE OF AMENDMENT TO THE ARTICLES OF INCORPORATION OF ACME "
        "CORPORATION. The board and stockholders approved this charter amendment."
    )
    system, user = _render(CHARTER_KEY, document)
    _assert_charter_contract(system)
    assert BOARD_RULE in system and document in user


@pytest.mark.parametrize(("expected", "predicted", "score"), [
    ("entity_formation", "corporate_action_approval", 0.0),
    ("entity_formation", "entity_formation", 1.0),
    ("corporate_action_approval", "corporate_action_approval", 1.0),
    ("corporate_action_approval", "entity_formation", 0.0),
])
def test_existing_dojo_label_controls_do_not_equate_the_two_intents(expected, predicted, score):
    out = get_suite("corporate_records_specialist").score(
        {"intent": expected}, {"intent": predicted}, detailed=True,
    )
    assert out["extraction"].field_scores["intent"] == score
    assert out["extraction_precision"] == out["extraction_recall"] == out["extraction_f1"] == score


@pytest.mark.parametrize(("document", "company", "signatories"), [
    ("POWER OF ATTORNEY. Registrant: Acme Corporation. Individual principal "
     "Jane Doe appoints John Roe. Signed by Jane Doe.", "Acme Corporation", ["Jane Doe"]),
    ("POWER OF ATTORNEY. Acme Corporation, the principal and registrant, "
     "appoints John Roe. Signed for Acme Corporation by Jane Doe.", "Acme Corporation", ["Jane Doe"]),
    ("POWER OF ATTORNEY. Jane Doe appoints John Roe. Signed by Jane Doe. "
     "No company is identified in this instrument.", None, ["Jane Doe"]),
    ("POWER OF ATTORNEY. Registrant: Acme Corporation. Jane Doe appoints "
     "John Roe and Alex Smith as attorneys-in-fact. Signed by Jane Doe.",
     "Acme Corporation", ["Jane Doe"]),
])
def test_poa_company_individual_null_and_attorneys_request_controls(document, company, signatories):
    system, user = _render(POA_KEY, document)
    _assert_poa_contract(system)
    assert document in user
    assert "signatories as grantor/attorney-in-fact" in _brief(system, "powers_of_attorney")
    assert "individuals who executed or approved, full names as written" in system
    # Hand-labeled synthetic payload validates type/shape only. No fake LLM.
    labeled = {
        "entity_name": company, "record_type": "powers_of_attorney",
        "effective_date": None, "signatories": signatories, "jurisdiction": None,
        "filing_number": None, "intent": "authority_delegation",
        "subject_matter": None, "keywords": [],
    }
    parsed = CorporateRecordExtraction.model_validate(labeled)
    assert parsed.model_dump() == labeled
    assert set(labeled) == FLAT9
    assert parsed.entity_name == company and parsed.signatories == signatories
    assert "principal" not in CORPORATE_RECORDS_SCHEMA["properties"]
    assert "attorneys_in_fact" not in CORPORATE_RECORDS_SCHEMA["properties"]


def test_schema_types_alone_cannot_detect_an_individual_in_entity_name():
    # Semantic ambiguity is in the text; do not claim a schema/type gate fixes it.
    parsed = CorporateRecordExtraction.model_validate({"entity_name": "Jane Doe"})
    assert parsed.entity_name == "Jane Doe"


def test_unlabeled_poa_entity_is_not_fabricated_accuracy(monkeypatch):
    """Unlabeled name support stays absent under an explicit offline posture."""
    from llm_dojo_scoring.config import get_settings
    from llm_dojo_scoring.field_scoring import embedding_enabled

    monkeypatch.setattr(get_settings().field_scoring, "embedding_enabled", False)
    assert embedding_enabled() is False
    out = get_suite("corporate_records_specialist").score(
        {"intent": "authority_delegation"},
        {"intent": "authority_delegation", "entity_name": "Jane Doe"},
        doc_subclass="powers_of_attorney", detailed=True,
    )
    assert "entity_name" in out["unlabeled_fields"]
    assert "entity_name" not in out["extraction"].field_scores
    assert out["extraction"].field_scores == {"intent": 1.0}
    assert out["extraction_f1"] == 1.0  # intent support only, no name supervision
