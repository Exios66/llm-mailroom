"""Gmail intake triage agent (HUB-037) — network-free tests.

Covers the advisory pre-pipeline read: vocabulary clamping, the agent call
contract, prompt registration, the watcher wiring (gmail-only, fail-soft),
the env gate, and the completion-echo rendering. No socket or real LLM is
ever touched.
"""

import json

from agents.gmail_triage import (
    GmailTriageAgent,
    TRIAGE_SCHEMA,
    _triage_system_prompt,
    validate_triage,
)
from pipeline import gmail_intake


# ── vocabulary clamping ──────────────────────────────────────────────────


def test_validate_triage_clamps_unknown_class():
    out = validate_triage({"primary_doc_class": "not-a-real-class", "confidence": 0.9, "gist": "x"})
    assert out["primary_doc_class"] == "unknown"


def test_validate_triage_accepts_live_class():
    out = validate_triage(
        {"primary_doc_class": "INSURANCE_CLAIM", "confidence": 0.9, "gist": "x"}
    )
    assert out["primary_doc_class"] == "insurance_claim"  # normalized to the live token


def test_validate_triage_clamps_confidence_and_keywords():
    out = validate_triage(
        {
            "primary_doc_class": "insurance_claim",
            "confidence": 7.5,
            "gist": "  ",
            "keywords": [f"k{i}" for i in range(20)],
        }
    )
    assert out["confidence"] == 1.0
    assert len(out["keywords"]) <= 6
    assert out["doc_subclass"] is None
    assert out["gist"] == ""


def test_validate_triage_handles_garbage_inputs():
    # Non-list keywords must not be char-iterated; subclass coerced to str.
    out = validate_triage(
        {
            "primary_doc_class": None,
            "confidence": "high",
            "gist": 42,
            "keywords": "hail,roof",
            "doc_subclass": 123,
        }
    )
    assert out["primary_doc_class"] == "unknown"
    assert out["confidence"] == 0.0
    assert out["keywords"] == []
    assert out["doc_subclass"] == "123"


def test_validate_triage_truncates_long_gist_and_subclass():
    out = validate_triage(
        {
            "primary_doc_class": "contract",
            "doc_subclass": "x" * 200,
            "confidence": 0.5,
            "gist": "y" * 1000,
            "keywords": ["k"],
        }
    )
    assert len(out["gist"]) == 300
    assert len(out["doc_subclass"]) == 80


# ── prompt ───────────────────────────────────────────────────────────────


def test_triage_system_prompt_lists_live_classes():
    from pipeline.config import get_all_doc_types

    prompt = _triage_system_prompt()
    for cls in get_all_doc_types():
        assert cls in prompt
    assert "unknown" in prompt


def test_triage_prompt_registered_in_prompt_templates():
    from llm.prompts import prompt_templates

    from agents.gmail_triage import TRIAGE_SYSTEM_PROMPT

    templates = prompt_templates()
    assert "gmail_triage" in templates
    assert templates["gmail_triage"] == TRIAGE_SYSTEM_PROMPT


# ── agent call contract ──────────────────────────────────────────────────


def test_triage_agent_returns_validated_result(mock_openai_client, sample_insurance_claim_text):
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = json.dumps(
        {
            "primary_doc_class": "insurance_claim",
            "doc_subclass": "other",
            "confidence": 0.91,
            "gist": "FNOL for hail damage",
            "keywords": ["hail", "roof", "policy"],
        }
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "insurance_claim"
    assert out["doc_subclass"] == "other"
    assert out["confidence"] == 0.91
    assert out["gist"] == "FNOL for hail damage"
    assert out["keywords"] == ["hail", "roof", "policy"]


def test_triage_agent_fails_soft_on_garbage_model_output(mock_openai_client, sample_insurance_claim_text):
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = "this is not json at all"
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "unknown"  # clamped, never raises
    assert out["confidence"] == 0.0


def test_triage_schema_contract():
    assert set(TRIAGE_SCHEMA["properties"]) == {
        "primary_doc_class",
        "doc_subclass",
        "confidence",
        "gist",
        "keywords",
    }
    assert set(TRIAGE_SCHEMA["required"]) == {"primary_doc_class", "confidence", "gist"}


# ── env gate ─────────────────────────────────────────────────────────────


def test_triage_enabled_gate(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    monkeypatch.setenv("MAILROOM_GMAIL_TRIAGE", "1")
    assert gmail_intake.triage_enabled() is True
    monkeypatch.setenv("MAILROOM_GMAIL_TRIAGE", "0")
    assert gmail_intake.triage_enabled() is False
    monkeypatch.delenv("MAILROOM_GMAIL_TRIAGE")
    assert gmail_intake.triage_enabled() is True  # default: with the channel
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "0")
    assert gmail_intake.triage_enabled() is False  # channel master switch wins


# ── watcher wiring ───────────────────────────────────────────────────────


def _gmail_inbox_file(temp_base_dir, name="fnol_triage.txt"):
    from pipeline.bins import inbox_dir, write_inbox_meta

    inbox_file = inbox_dir() / name
    inbox_file.write_text("ACME INSURANCE COMPANY — FNOL hail damage to roof and garage")
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="MATTER-TR",
        message_id="<msg-triage@example.com>",
        sender="client@firm.example",
        subject="FNOL [M:MATTER-TR]",
    )
    return inbox_file


def test_watcher_gmail_claim_runs_triage_before_pipeline(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    instance = fake_agent.return_value
    instance.triage.return_value = {
        "primary_doc_class": "insurance_claim",
        "doc_subclass": "other",
        "confidence": 0.9,
        "gist": "FNOL",
        "keywords": ["hail"],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    instance.triage.assert_called_once()
    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs["intake_meta"]["triage"]["primary_doc_class"] == "insurance_claim"


def test_watcher_skips_triage_for_upload_source(temp_base_dir, mocker):
    from pipeline.bins import inbox_dir
    from pipeline.watcher import Watcher

    inbox_file = inbox_dir() / "plain_upload.txt"
    inbox_file.write_text("plain upload document")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()
    assert (spy.call_args.kwargs.get("intake_meta") or {}).get("triage") is None


def test_watcher_triage_disabled_by_env_gate(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=False)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()
    assert (spy.call_args.kwargs.get("intake_meta") or {}).get("triage") is None


def test_watcher_triage_failure_fails_soft(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    mocker.patch(
        "agents.gmail_triage.GmailTriageAgent",
        side_effect=RuntimeError("free model down"),
    )
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    # A triage failure must never block the claim.
    assert spy.call_count == 1
    assert "triage" not in spy.call_args.kwargs["intake_meta"]


def test_watcher_triage_agent_failure_fails_soft(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    instance = fake_agent.return_value
    instance.triage.side_effect = RuntimeError("rate limited")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    assert spy.call_count == 1
    assert "triage" not in spy.call_args.kwargs["intake_meta"]


# ── completion echo ──────────────────────────────────────────────────────


def test_build_echo_body_renders_triage_section():
    manifest = {
        "doc_id": "d-echo-triage",
        "matter_id": "M-1",
        "original_filename": "fnol.pdf",
        "stage": "archived",
        "doc_type": "insurance_claim",
        "classification_confidence": 0.98,
        "extracted_data": {"confidence": 0.97},
        "intake": {
            "source": "gmail",
            "message_id": "<echo-triage@example.com>",
            "sender": "client@firm.example",
            "subject": "FNOL [M:M-1]",
            "triage": {
                "primary_doc_class": "insurance_claim",
                "doc_subclass": "other",
                "confidence": 0.91,
                "gist": "FNOL for hail damage",
                "keywords": ["hail", "roof"],
            },
        },
    }
    body = gmail_intake.build_echo_body(manifest, [], None)
    assert "-- INTAKE TRIAGE (pre-pipeline) --" in body
    assert "insurance_claim" in body
    assert "0.91" in body
    assert "FNOL for hail damage" in body
    assert "hail, roof" in body


def test_build_echo_body_omits_triage_section_when_absent():
    manifest = {
        "doc_id": "d-echo-plain",
        "matter_id": "M-1",
        "original_filename": "upload.pdf",
        "stage": "archived",
        "intake": {"source": "upload", "upload_id": "x"},
    }
    body = gmail_intake.build_echo_body(manifest, [], None)
    assert "INTAKE TRIAGE" not in body