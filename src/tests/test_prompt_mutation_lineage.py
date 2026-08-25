"""Guards for the mailroom production prompt mutation + docclass connection.

Lineage: frozen predecessors stay byte-identical; new production versions are
pure appends of ``llm.prompt_doctrine``. Docclass variants are derived from
``prompt_templates()`` so the two arms cannot drift.
"""

from agents import (
    arbiter,
    boss,
    compliance_specialist,
    correspondence_specialist,
    corporate_records_specialist,
    court_opinions_specialist,
    due_diligence_specialist,
    insurance_claims_specialist,
    judge,
    pdf_transcriber,
    reporter,
    sorter_reviewer,
)
from langchain_agents import prompts as LP
from llm.prompt_doctrine import (
    ARBITER,
    BOSS,
    COMPLIANCE,
    CONTRACTS,
    CORPORATE_RECORDS,
    CORRESPONDENCE,
    COURT_OPINIONS,
    DUE_DILIGENCE,
    INSURANCE_CLAIMS,
    JUDGE_CLASSIFICATION,
    JUDGE_COMPLETENESS,
    JUDGE_CORRECTNESS,
    PDF_TRANSCRIBER,
    REPORTER,
    SORTER,
    SORTER_REVIEWER,
)
from llm.prompts import prompt_templates


def test_sorter_v14_is_pure_append_of_v12():
    assert LP.SORTER_PROMPT_V14.startswith(LP.SORTER_PROMPT_V12.rstrip())
    assert LP.SORTER_PROMPT_V14 != LP.SORTER_PROMPT_V12
    assert SORTER in LP.SORTER_PROMPT_V14
    assert "never substitute correspondence" in LP.SORTER_PROMPT_V14.lower()
    # Frozen predecessors
    assert "insurance_claim" not in LP.SORTER_PROMPT_V0
    assert LP.PROMPT_TEMPLATES()["sorter_v12"] is LP.SORTER_PROMPT_V12
    assert LP.PROMPT_TEMPLATES()["sorter_v13"] is LP.SORTER_PROMPT_V13
    assert LP.PROMPT_TEMPLATES()["sorter"] is LP.SORTER_PROMPT_V14


def test_contracts_v32_is_pure_append_of_v31():
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V32.startswith(
        LP.CONTRACTS_SPECIALIST_PROMPT_V31.rstrip()
    )
    assert CONTRACTS in LP.CONTRACTS_SPECIALIST_PROMPT_V32
    assert "numeric zero" in LP.CONTRACTS_SPECIALIST_PROMPT_V32.lower()
    assert LP.PROMPT_TEMPLATES()["contracts_specialist_v31"] is LP.CONTRACTS_SPECIALIST_PROMPT_V31
    assert LP.PROMPT_TEMPLATES()["contracts_specialist"] is LP.CONTRACTS_SPECIALIST_PROMPT_V32


def test_mailroom_specialist_prompts_are_pure_appends_of_v0():
    pairs = [
        (corporate_records_specialist, CORPORATE_RECORDS),
        (due_diligence_specialist, DUE_DILIGENCE),
        (correspondence_specialist, CORRESPONDENCE),
        (compliance_specialist, COMPLIANCE),
        (court_opinions_specialist, COURT_OPINIONS),
        (insurance_claims_specialist, INSURANCE_CLAIMS),
        (pdf_transcriber, PDF_TRANSCRIBER),
    ]
    for module, doctrine in pairs:
        v0 = module.SYSTEM_PROMPT_V0
        current = module.SYSTEM_PROMPT
        assert current.startswith(v0.rstrip())
        assert doctrine in current
        assert current != v0


def test_supporting_prompts_are_pure_appends_of_v0():
    pairs = [
        (boss.BOSS_SYSTEM_PROMPT_V0, boss.BOSS_SYSTEM_PROMPT, BOSS),
        (reporter.COMPILE_SYSTEM_PROMPT_V0, reporter.COMPILE_SYSTEM_PROMPT, REPORTER),
        (sorter_reviewer.REVIEWER_SYSTEM_PROMPT_V0, sorter_reviewer.REVIEWER_SYSTEM_PROMPT, SORTER_REVIEWER),
        (arbiter.ARBITER_SYSTEM_PROMPT_V0, arbiter.ARBITER_SYSTEM_PROMPT, ARBITER),
        (judge.SYSTEM_PROMPT_V0, judge.SYSTEM_PROMPT, JUDGE_COMPLETENESS),
        (judge.CLASSIFICATION_SYSTEM_PROMPT_V0, judge.CLASSIFICATION_SYSTEM_PROMPT, JUDGE_CLASSIFICATION),
        (judge.CORRECTNESS_SYSTEM_PROMPT_V0, judge.CORRECTNESS_SYSTEM_PROMPT, JUDGE_CORRECTNESS),
    ]
    for v0, current, doctrine in pairs:
        assert current.startswith(v0.rstrip())
        assert doctrine in current
        assert current != v0


def test_production_templates_are_the_mutated_versions():
    templates = prompt_templates()
    assert templates["sorter"] == LP.SORTER_PROMPT_V14
    assert templates["contracts_specialist"] == LP.CONTRACTS_SPECIALIST_PROMPT_V32
    assert templates["corporate_records_specialist"] == corporate_records_specialist.SYSTEM_PROMPT
    assert templates["insurance_claims_specialist"] == insurance_claims_specialist.SYSTEM_PROMPT
    assert templates["sorter_reviewer"] == sorter_reviewer.REVIEWER_SYSTEM_PROMPT
    assert templates["arbiter"] == arbiter.ARBITER_SYSTEM_PROMPT
    assert "{{" not in templates["contracts_specialist"]
    assert "{{doc_type_descriptions}}" in templates["sorter"]


def test_runtime_defaults_pin_the_new_versions():
    import inspect

    from agents.contracts_specialist import ContractsSpecialist
    from agents.sorter import SorterAgent

    sorter_params = inspect.signature(SorterAgent.__init__).parameters
    assert sorter_params["prompt_version"].default == "sorter_v14"
    contracts_params = inspect.signature(ContractsSpecialist.__init__).parameters
    assert contracts_params["prompt_version"].default == "contracts_specialist_v32"


def test_doctrine_has_no_mustache_placeholders():
    import llm.prompt_doctrine as doctrine

    for name in dir(doctrine):
        if name.startswith("_"):
            continue
        value = getattr(doctrine, name)
        if isinstance(value, str):
            assert "{{" not in value, name
