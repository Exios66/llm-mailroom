"""Opt-in label-aligned specialist guidance (llm/label_alignment.py)."""

import pytest

from llm import label_alignment as LA
from llm.frozen_v1 import SPECIALIST_AGENTS, load_specialist_v1

ALIGNED = ("correspondence_specialist", "corporate_records_specialist", "contracts_specialist")


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(LA.ALIGNMENT_ENV, raising=False)
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")


def test_default_mode_is_off_and_prompts_are_frozen_bytes():
    assert LA.alignment_mode() == "off"
    for agent in SPECIALIST_AGENTS:
        base = load_specialist_v1(agent)
        assert LA.apply_label_alignment(agent, base) is base


def test_taxonomy_mode_is_a_string_not_a_yaml_boolean():
    from pipeline.config import load_config

    assert load_config()["prompt_alignment"]["mode"] == "off"


@pytest.mark.parametrize("raw,expected", [
    ("rules", "rules"), (" RULES_FEW_SHOT ", "rules_few_shot"), ("off", "off"),
    ("", "off"), ("typo", "off"), ("true", "off"),
])
def test_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv(LA.ALIGNMENT_ENV, raw)
    assert LA.alignment_mode() == expected


def test_taxonomy_mode_applies_without_env(monkeypatch):
    monkeypatch.setattr("pipeline.config.load_config", lambda: {"prompt_alignment": {"mode": "rules"}})
    assert LA.alignment_mode() == "rules"


@pytest.mark.parametrize("agent", ALIGNED)
def test_rules_append_after_the_untouched_base(agent):
    base = load_specialist_v1(agent)
    out = LA.apply_label_alignment(agent, base, mode="rules")
    assert out.startswith(base.rstrip())
    assert "Label-aligned rules" in out
    assert "Worked examples" not in out


def test_few_shot_only_with_rules_few_shot():
    base = load_specialist_v1("correspondence_specialist")
    out = LA.apply_label_alignment("correspondence_specialist", base, mode="rules_few_shot")
    assert out.index("Label-aligned rules") < out.index("Worked examples")


@pytest.mark.parametrize("agent", ("insurance_claims_specialist", "merger_agreement_specialist", "judge"))
def test_agents_without_addenda_are_unchanged(agent):
    assert LA.apply_label_alignment(agent, "BASE", mode="rules_few_shot") == "BASE"


def test_correspondence_rules_state_the_reviewed_label_rules():
    text = LA.CORRESPONDENCE_RULES
    for rule in ("reply typed above quoted mail", "Forwarded by X on <date>", "bare forward",
                 "Promotional or bulk mail", "generic salutation", "RECIPIENT pay",
                 "calendar entry", "bare pointer"):
        assert rule in text, rule
    for token in ("routine", "time-sensitive", "urgent", "critical"):
        assert token in text


def test_contract_roles_come_from_the_dojo_vocabulary():
    from llm_dojo_scoring.schemas.cuad_schema import CONTRACT_PARTY_ROLES

    text = LA.alignment_templates()["contracts_specialist-label-rules"]
    assert ", ".join(CONTRACT_PARTY_ROLES) in text
    assert "{roles}" not in text


def test_contract_rules_cover_the_fields_the_v1_prompt_omits():
    from schemas.documents import ContractExtraction

    base = load_specialist_v1("contracts_specialist")
    omitted = [f for f in ContractExtraction.model_fields if f not in base and f not in ("reasoning",)]
    assert omitted, "v1 now names every field; revisit the contract addendum"
    rules = LA.alignment_templates()["contracts_specialist-label-rules"]
    assert all(f"- {field}:" in rules for field in omitted), omitted


def test_addenda_are_registered_managed_prompts():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    for key, text in LA.alignment_templates().items():
        assert templates[key] == text
        assert "{{" not in text


def test_managed_addendum_text_wins(monkeypatch):
    seen = []

    def fake(name, default, variables=None, label="production"):
        seen.append(name)
        return f"MANAGED {name}", None

    monkeypatch.setattr("llm.prompts.get_managed_prompt", fake)
    out = LA.apply_label_alignment("correspondence_specialist", "BASE", mode="rules_few_shot")
    assert seen == ["correspondence_specialist-label-rules", "correspondence_specialist-label-examples"]
    assert out == "BASE\n\nMANAGED correspondence_specialist-label-rules\n\nMANAGED correspondence_specialist-label-examples"


def test_specialist_system_prompt_follows_the_env(monkeypatch):
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist.__new__(CorrespondenceSpecialist)
    off = agent.system_prompt()
    monkeypatch.setenv(LA.ALIGNMENT_ENV, "rules")
    on = agent.system_prompt()
    assert off == load_specialist_v1("correspondence_specialist")
    assert on.startswith(off.rstrip()) and "Label-aligned rules" in on


@pytest.mark.parametrize("raw,expected", [(None, "off"), ("rules_few_shot", "rules_few_shot")])
def test_trace_metadata_records_the_alignment_arm(monkeypatch, tmp_path, temp_base_dir, raw, expected):
    import contextlib

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
        monkeypatch.setenv(LA.ALIGNMENT_ENV, raw)
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")
    with contextlib.suppress(RuntimeError):
        bg.run_pipeline(doc, "M-1", run_id="r-align")
    assert captured["prompt_alignment"] == expected


def test_unknown_env_value_warns(monkeypatch):
    calls = []
    monkeypatch.setattr(LA.logger, "warning", lambda event, **kw: calls.append((event, kw)))
    monkeypatch.setenv(LA.ALIGNMENT_ENV, "rule")
    assert LA.alignment_mode() == "off"
    assert calls and calls[0][0] == "prompt_alignment_env_ignored"
