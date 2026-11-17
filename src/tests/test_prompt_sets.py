"""Versioned prompt sets (llm/prompt_sets): catalog, selection, served text, fixes."""

import contextlib

import pytest

from llm import prompt_sets as PS
from llm.frozen_v1 import SPECIALIST_AGENTS, load_specialist_v1

DEPLOY_AGENTS = (
    "contracts_specialist", "correspondence_specialist", "corporate_records_specialist",
    "insurance_claims_specialist", "judge", "judge-correctness",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(PS.PROMPT_SET_ENV, raising=False)
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")


def _taxonomy(monkeypatch, block):
    monkeypatch.setattr("pipeline.config.load_config", lambda: {"prompt_set": block})


def test_catalog_files_exist_and_deploy_is_default():
    assert PS.catalog()["default_set"] == "deploy_v1"
    assert {"frozen_v1", "deploy_v1", "deploy_v1_examples"} <= set(PS.set_names())
    templates = PS.set_templates()
    assert len(templates) == 7
    assert all(text.strip() for text in templates.values())


def test_taxonomy_selects_deploy_v1():
    from pipeline.config import load_config

    assert load_config()["prompt_set"]["active"] == "deploy_v1"
    assert PS.active_prompt_set() == "deploy_v1"


@pytest.mark.parametrize("agent", DEPLOY_AGENTS)
def test_deploy_set_serves_its_file_under_a_set_scoped_name(agent):
    name, text = PS.served_prompt(agent, "CODE DEFAULT")
    assert name == f"{agent}--deploy_v1"
    assert text == PS.set_templates()[f"{agent}--deploy_v1"]
    assert PS.prompt_id(agent) == f"{agent}@deploy_v1"


@pytest.mark.parametrize("agent", ("sorter", "merger_agreement_specialist", "boss", "arbiter", "judge-classification"))
def test_carried_agents_keep_their_code_default(agent):
    assert PS.served_prompt(agent, "CODE DEFAULT") == (agent, "CODE DEFAULT")
    assert PS.prompt_id(agent) == f"{agent}@code"


def test_frozen_v1_set_is_the_pre_set_behavior(monkeypatch):
    monkeypatch.setenv(PS.PROMPT_SET_ENV, "frozen_v1")
    for agent in DEPLOY_AGENTS:
        assert PS.served_prompt(agent, "CODE DEFAULT") == (agent, "CODE DEFAULT")


def test_examples_set_extends_deploy(monkeypatch):
    monkeypatch.setenv(PS.PROMPT_SET_ENV, "deploy_v1_examples")
    name, text = PS.served_prompt("correspondence_specialist", "X")
    assert name == "correspondence_specialist--deploy_v1_examples" and "Worked examples" in text
    assert PS.served_prompt("contracts_specialist", "X")[0] == "contracts_specialist--deploy_v1"


def test_unknown_env_value_warns_and_falls_back(monkeypatch):
    calls = []
    monkeypatch.setattr(PS.logger, "warning", lambda event, **kw: calls.append(event))
    monkeypatch.setenv(PS.PROMPT_SET_ENV, "deploy_v2")
    assert PS.active_prompt_set() == "deploy_v1"
    assert calls == ["prompt_set_env_ignored"]


def test_by_agent_pin_and_env_precedence(monkeypatch):
    _taxonomy(monkeypatch, {"active": "deploy_v1", "by_agent": {"correspondence_specialist": "frozen_v1"}})
    assert PS.served_prompt("correspondence_specialist", "X") == ("correspondence_specialist", "X")
    assert PS.served_prompt("contracts_specialist", "X")[0] == "contracts_specialist--deploy_v1"
    monkeypatch.setenv(PS.PROMPT_SET_ENV, "deploy_v1")
    assert PS.served_prompt("correspondence_specialist", "X")[0] == "correspondence_specialist--deploy_v1"


def test_contract_prompt_names_every_schema_field_and_the_dojo_roles():
    from llm_dojo_scoring.schemas.cuad_schema import CONTRACT_PARTY_ROLES
    from schemas.documents import ContractExtraction

    text = PS.resolve("contracts_specialist", "deploy_v1")[1]
    frozen = load_specialist_v1("contracts_specialist")
    missing_before = [f for f in ContractExtraction.model_fields if f"- {f} (" not in frozen]
    assert {"party_roles", "signatories", "agreement_date", "expiration_date",
            "termination_notice_period", "dispute_forum", "payment_terms"} <= set(missing_before)
    scored = [f for f in ContractExtraction.model_fields if f not in ("merger_consideration", "maud_clauses")]
    assert [f for f in scored if f"- {f} (" not in text] == []
    assert ", ".join(CONTRACT_PARTY_ROLES) in text


@pytest.mark.parametrize("agent,schema", [
    ("correspondence_specialist", "CorrespondenceExtraction"),
    ("corporate_records_specialist", "CorporateRecordExtraction"),
    ("insurance_claims_specialist", "InsuranceClaimExtraction"),
])
def test_condensed_specialists_name_every_model_field(agent, schema):
    import schemas.documents as D

    text = PS.resolve(agent, "deploy_v1")[1]
    derived = {"cms_source_table"}  # filled by enrichment, never asked of the model
    assert [f for f in getattr(D, schema).model_fields if f not in derived and f not in text] == []


def test_correspondence_deploy_states_the_label_rules():
    text = PS.resolve("correspondence_specialist", "deploy_v1")[1]
    for rule in ("reply typed above quoted mail", "Forwarded by X on <date>", "bare forward",
                 "Promotional or bulk mail", "generic salutation", "RECIPIENT pay", "calendar entry",
                 "bare pointer", "Type the content, not the carrier"):
        assert rule in text, rule


def test_corporate_deploy_states_the_label_rules():
    text = PS.resolve("corporate_records_specialist", "deploy_v1")[1]
    for rule in ("adoption or filing date", "under whose law the entity is organised", "execution block"):
        assert rule in text, rule


@pytest.mark.parametrize("agent,attr", [("judge", "SYSTEM_PROMPT"), ("judge-correctness", "CORRECTNESS_SYSTEM_PROMPT")])
def test_judge_deploy_is_current_production_plus_conventions(agent, attr):
    from agents import judge

    text = PS.resolve(agent, "deploy_v1")[1]
    assert text.startswith(getattr(judge, attr).rstrip()), "re-render the judge deploy file from the production prompt"
    assert "Class conventions" in text


def test_examples_use_no_corpus_names():
    text = PS.resolve("correspondence_specialist", "deploy_v1_examples")[1]
    for real in ("Enron", "GISB", "ICAP", "Houston"):
        assert real not in text


def test_set_files_are_registered_managed_prompts():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    for name, text in PS.set_templates().items():
        assert templates[name] == text and "{{" not in text
    for agent in SPECIALIST_AGENTS:
        assert templates[agent] == load_specialist_v1(agent)


@pytest.mark.parametrize("module,cls", [
    ("agents.correspondence_specialist", "CorrespondenceSpecialist"),
    ("agents.corporate_records_specialist", "CorporateRecordsSpecialist"),
    ("agents.insurance_claims_specialist", "InsuranceClaimsSpecialist"),
    ("agents.contracts_specialist", "ContractsSpecialist"),
])
def test_specialists_serve_the_active_set(monkeypatch, module, cls):
    import importlib

    klass = getattr(importlib.import_module(module), cls)
    agent = klass.__new__(klass)
    agent.prompt_version = klass.agent_name
    deployed = agent.system_prompt()
    assert deployed == PS.resolve(klass.agent_name, "deploy_v1")[1]
    monkeypatch.setenv(PS.PROMPT_SET_ENV, "frozen_v1")
    frozen = agent.system_prompt()
    assert frozen != deployed
    if klass.agent_name in SPECIALIST_AGENTS:
        assert frozen == load_specialist_v1(klass.agent_name)


def test_contracts_eval_pin_is_not_replaced():
    from agents.contracts_specialist import ContractsSpecialist
    from langchain_agents.prompts import get_prompt

    agent = ContractsSpecialist.__new__(ContractsSpecialist)
    agent.prompt_version = "contracts_specialist_v33"
    assert agent.system_prompt() == get_prompt("contracts_specialist_v33")


@pytest.mark.parametrize("raw,expected", [(None, "deploy_v1"), ("frozen_v1", "frozen_v1")])
def test_trace_metadata_records_the_prompt_set(monkeypatch, tmp_path, temp_base_dir, raw, expected):
    import graph.build_graph as bg
    import observability.tracing as tracing

    captured: dict = {}

    @contextlib.contextmanager
    def fake_trace(**kwargs):
        captured.update(kwargs["metadata"])
        raise RuntimeError("stop before the graph runs")
        yield  # pragma: no cover

    monkeypatch.setattr(tracing, "pipeline_trace", fake_trace)
    if raw is not None:
        monkeypatch.setenv(PS.PROMPT_SET_ENV, raw)
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    with contextlib.suppress(RuntimeError):
        bg.run_pipeline(doc, "M-1", run_id="r-set")
    assert captured["prompt_set"] == expected
