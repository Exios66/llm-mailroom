"""Guards for the mailroom production prompt mutation lineage.

Lineage: frozen predecessors stay byte-identical; new production versions are
pure appends of ``llm.prompt_doctrine``.
"""

from agents import (
    arbiter,
    boss,
    correspondence_specialist,
    corporate_records_specialist,
    insurance_claims_specialist,
    merger_agreement_specialist,
    judge,
    pdf_transcriber,
    reporter,
    sorter_reviewer,
)
from langchain_agents import prompts as LP
from llm.prompt_doctrine import (
    ARBITER,
    BOSS,
    CONTRACTS,
    CORPORATE_RECORDS,
    CORRESPONDENCE,
    INSURANCE_CLAIMS,
    MERGER_AGREEMENT,
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
    assert "doc_subclass" in LP.SORTER_PROMPT_V14
    assert "content_topic and sentiment_label are not sorter outputs" in LP.SORTER_PROMPT_V14
    # Frozen predecessors
    assert "insurance_claim" not in LP.SORTER_PROMPT_V0
    assert LP.PROMPT_TEMPLATES()["sorter_v12"] is LP.SORTER_PROMPT_V12
    assert LP.PROMPT_TEMPLATES()["sorter_v13"] is LP.SORTER_PROMPT_V13
    assert LP.PROMPT_TEMPLATES()["sorter"] is LP.SORTER_PROMPT_V14


def test_contracts_v33_is_pure_append_of_v32():
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V33.startswith(
        LP.CONTRACTS_SPECIALIST_PROMPT_V32.rstrip()
    )
    assert "PARED EXTRACTION" in LP.CONTRACTS_SPECIALIST_PROMPT_V33
    assert LP.PROMPT_TEMPLATES()["contracts_specialist_v32"] is LP.CONTRACTS_SPECIALIST_PROMPT_V32
    assert LP.PROMPT_TEMPLATES()["contracts_specialist_v33"] is LP.CONTRACTS_SPECIALIST_PROMPT_V33


def test_contracts_v32_is_pure_append_of_v31():
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V32.startswith(
        LP.CONTRACTS_SPECIALIST_PROMPT_V31.rstrip()
    )
    assert CONTRACTS in LP.CONTRACTS_SPECIALIST_PROMPT_V32
    assert "numeric zero" in LP.CONTRACTS_SPECIALIST_PROMPT_V32.lower()
    assert LP.PROMPT_TEMPLATES()["contracts_specialist_v31"] is LP.CONTRACTS_SPECIALIST_PROMPT_V31
    assert LP.PROMPT_TEMPLATES()["contracts_specialist_v32"] is LP.CONTRACTS_SPECIALIST_PROMPT_V32


def test_mailroom_specialist_v0_plus_doctrine_is_preserved_as_history():
    """V0 + doctrine stays constructible; production SYSTEM_PROMPT is frozen v1."""
    from llm.frozen_v1 import load_specialist_v1

    historical = [
        (corporate_records_specialist, CORPORATE_RECORDS, "corporate_records_specialist"),
        (correspondence_specialist, CORRESPONDENCE, "correspondence_specialist"),
        (insurance_claims_specialist, INSURANCE_CLAIMS, "insurance_claims_specialist"),
        (merger_agreement_specialist, MERGER_AGREEMENT, "merger_agreement_specialist"),
    ]
    for module, doctrine, agent in historical:
        v0 = module.SYSTEM_PROMPT_V0
        mutated = v0.rstrip() + "\n\n" + doctrine
        assert mutated.startswith(v0.rstrip())
        assert doctrine in mutated
        assert mutated != v0
        assert module.SYSTEM_PROMPT == load_specialist_v1(agent)
        assert module.SYSTEM_PROMPT != mutated

    v0 = pdf_transcriber.SYSTEM_PROMPT_V0
    current = pdf_transcriber.SYSTEM_PROMPT
    assert current.startswith(v0.rstrip())
    assert PDF_TRANSCRIBER in current
    assert current != v0


def test_supporting_prompts_are_pure_appends_of_v0():
    pairs = [
        (boss.BOSS_SYSTEM_PROMPT_V0, boss.BOSS_SYSTEM_PROMPT, BOSS),
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
    # Reporter is procedural (no LLM doctrine append).
    assert reporter.COMPILE_SYSTEM_PROMPT == reporter.COMPILE_SYSTEM_PROMPT_V0
    assert "procedural" in reporter.COMPILE_SYSTEM_PROMPT.lower()


def test_production_templates_are_the_mutated_versions():
    from llm.frozen_v1 import load_specialist_v1

    templates = prompt_templates()
    assert templates["sorter"] == LP.SORTER_PROMPT_V14
    assert templates["contracts_specialist"] == load_specialist_v1("contracts_specialist")
    assert templates["corporate_records_specialist"] == corporate_records_specialist.SYSTEM_PROMPT
    assert templates["insurance_claims_specialist"] == insurance_claims_specialist.SYSTEM_PROMPT
    assert templates["merger_agreement_specialist"] == merger_agreement_specialist.SYSTEM_PROMPT
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
    assert contracts_params["prompt_version"].default == "contracts_specialist"


def test_doctrine_has_no_mustache_placeholders():
    import llm.prompt_doctrine as doctrine

    for name in dir(doctrine):
        if name.startswith("_"):
            continue
        value = getattr(doctrine, name)
        if isinstance(value, str):
            assert "{{" not in value, name


def test_extraction_doctrine_lists_exactly_the_current_schema_fields():
    """The 'Registered schema fields' line must not drift from the schemas."""
    from langchain_agents.specialist_agents import (
        CONTRACTS_SCHEMA,
        CORPORATE_RECORDS_SCHEMA,
        CORRESPONDENCE_SCHEMA,
        INSURANCE_CLAIMS_SCHEMA,
    )

    def listed_fields(doctrine: str) -> set[str]:
        marker = "Registered schema fields: "
        line = next(line for line in doctrine.splitlines() if marker in line)
        rest = line.split(marker, 1)[1].split(". Return every key", 1)[0]
        return {field.strip() for field in rest.split(",") if field.strip()}

    for doctrine, schema in (
        (CONTRACTS, CONTRACTS_SCHEMA),
        (CORPORATE_RECORDS, CORPORATE_RECORDS_SCHEMA),
        (CORRESPONDENCE, CORRESPONDENCE_SCHEMA),
        (INSURANCE_CLAIMS, INSURANCE_CLAIMS_SCHEMA),
    ):
        assert listed_fields(doctrine) == set(schema["properties"])

    # Retired / stale names must not reappear.
    for stale in (
        "termination_clauses",
        "key_obligations",
        "key_provisions",
        "key_points",
        "referenced_communications",
    ):
        for doctrine in (CONTRACTS, CORPORATE_RECORDS, CORRESPONDENCE, INSURANCE_CLAIMS):
            assert stale not in doctrine

