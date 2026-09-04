"""Gmail intake triage agent (HUB-037) — network-free tests.

Covers the advisory pre-pipeline read: vocabulary clamping, the agent call
contract, prompt registration, the watcher wiring (gmail-only, fail-soft),
the env gate, and the completion-echo rendering. No socket or real LLM is
ever touched.
"""

import json

import pytest

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


def test_triage_agent_parses_fenced_json(mock_openai_client, sample_insurance_claim_text):
    """Free-tier upstreams (ling) wrap JSON in markdown fences — recover it."""
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = (
        "```json\n"
        + json.dumps(
            {
                "primary_doc_class": "insurance_claim",
                "confidence": 0.9,
                "gist": "FNOL",
                "keywords": ["hail"],
            }
        )
        + "\n```"
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "insurance_claim"
    assert out["debug"]["parse_ok"] is True


def test_triage_agent_parses_prose_wrapped_json(mock_openai_client, sample_insurance_claim_text):
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = (
        "Here is the triage result:\n"
        + json.dumps({"primary_doc_class": "correspondence", "confidence": 0.85, "gist": "email"})
        + "\nHope this helps!"
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "correspondence"
    assert out["debug"]["parse_ok"] is True


def test_triage_agent_writes_full_debug_io(mock_openai_client, sample_insurance_claim_text, temp_base_dir):
    """FULL input/output capture (human directive): every triage call leaves
    the complete system/user/response payloads + validated result on disk."""
    from pathlib import Path
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = "this is not json at all"
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "unknown"
    assert out["debug"]["parse_ok"] is False
    debug_dir = Path(out["debug"]["debug_dir"])
    assert debug_dir.is_dir()
    assert "insurance" in (debug_dir / "system.txt").read_text().lower() or (
        debug_dir / "system.txt"
    ).read_text().strip()
    assert "Document text:" in (debug_dir / "user.txt").read_text()
    assert "this is not json at all" in (debug_dir / "response.txt").read_text()
    assert (debug_dir / "result.json").is_file()
    assert (debug_dir / "meta.json").is_file()


def test_triage_agent_debug_io_on_call_failure(mock_openai_client, sample_insurance_claim_text, temp_base_dir):
    """Even a hard call failure (rate limits) leaves the full INPUT on disk."""
    from pathlib import Path

    agent = GmailTriageAgent()
    mock_openai_client.chat.completions.create.side_effect = RuntimeError("upstream 429 exhausted")

    with pytest.raises(RuntimeError):
        agent.triage(sample_insurance_claim_text, filename="claim.txt")

    debug_root = Path(temp_base_dir) / "debug" / "triage"
    runs = sorted(debug_root.iterdir())
    assert runs, "debug artifacts must exist even when the call fails"
    assert "Document text:" in (runs[-1] / "user.txt").read_text()
    assert "exception" in (runs[-1] / "meta.json").read_text()


def test_triage_schema_contract():
    assert set(TRIAGE_SCHEMA["properties"]) == {
        "primary_doc_class",
        "doc_subclass",
        "confidence",
        "gist",
        "keywords",
    }
    assert set(TRIAGE_SCHEMA["required"]) == {"primary_doc_class", "confidence", "gist"}


# ── key/concise entity extraction (HUB-048) ───────────────────────────────


def test_extraction_schema_for_correspondence_has_key_fields():
    from agents.gmail_triage import extraction_schema_for

    s = extraction_schema_for("correspondence")
    props = set(s["properties"].keys())
    assert {"sender", "recipient", "communication_date", "action_items",
            "subject_matter", "demand_amount", "intent", "urgency"} <= props


def test_validate_triage_extracts_key_entities_for_correspondence():
    out = validate_triage(
        {
            "primary_doc_class": "correspondence",
            "confidence": 0.95,
            "gist": "a short email",
            "extraction": {
                "sender": "phillip.allen@enron.com",
                "recipient": "colleen.sullivan@enron.com",
                "communication_date": "2000-08-09",
                "demand_amount": 1250.00,
                "action_items": ["attend Friday", "reach out to Keith"],
                "subject_matter": "transportation model",
                "intent": "request",
                "urgency": "low",
            },
        }
    )
    ext = out["extraction"]
    assert ext["sender"] == "phillip.allen@enron.com"
    assert ext["recipient"] == "colleen.sullivan@enron.com"
    assert ext["communication_date"] == "2000-08-09"
    assert ext["demand_amount"] == 1250.0
    assert ext["action_items"] == ["attend Friday", "reach out to Keith"]
    assert ext["subject_matter"] == "transportation model"


def test_validate_triage_clamps_extraction_to_class_schema():
    # Free model spuriously emits fields from a DIFFERENT class (contract
    # parties on a correspondence) — they must be dropped by the canonical
    # schema clamp.
    from agents.gmail_triage import validate_triage

    out = validate_triage(
        {
            "primary_doc_class": "correspondence",
            "confidence": 0.9,
            "gist": "x",
            "extraction": {
                "sender": "a@b.c",
                "parties": ["Enron", "El Paso"],       # not in CorrespondenceExtraction
                "governing_law": "NY",                  # not in CorrespondenceExtraction
                "recipient": "c@d.e",
            },
        }
    )
    assert "parties" not in out["extraction"]
    assert "governing_law" not in out["extraction"]
    assert out["extraction"]["sender"] == "a@b.c"
    assert out["extraction"]["recipient"] == "c@d.e"


def test_validate_triage_extraction_empty_on_unknown_class():
    out = validate_triage(
        {"primary_doc_class": "unknown", "confidence": 0.0, "gist": ""}
    )
    assert out["extraction"] == {}


# ── unknown-class best-effort extraction (HUB-049) ────────────────────────


def test_unknown_class_with_doc_text_extracts_header_entities():
    """A short header-bearing document that the free model cannot pin still
    yields grounded key entities via the deterministic header pass."""
    out = validate_triage(
        {
            "primary_doc_class": "unknown",
            "confidence": 0.3,
            "gist": "unclear",
            "keywords": [],
        },
        doc_text=(
            "From: phillip.allen@enron.com\n"
            "To: colleen.sullivan@enron.com\n"
            "Date: 2000-08-09\n"
            "Subject: Transportation model\n\n"
            "I am out on Friday, Keith will attend."
        ),
    )
    ext = out["extraction"]
    assert ext["sender"] == "phillip.allen@enron.com"
    assert ext["recipient"] == "colleen.sullivan@enron.com"
    assert ext["communication_date"] == "2000-08-09"
    assert ext["subject_matter"] == "Transportation model"


def test_unknown_class_deterministic_header_extraction_markdown_table():
    """The Enron Markdown sample header table (| From | … |) form is parsed."""
    from agents.gmail_triage import _deterministic_header_extraction

    out = _deterministic_header_extraction(
        "# Re: TRANSPORTATION MODEL\n"
        "\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| From | phillip.allen@enron.com |\n"
        "| To | colleen.sullivan@enron.com |\n"
        "| Date | 2000-08-09T14:11:00+00:00 |\n"
    )
    assert out["sender"] == "phillip.allen@enron.com"
    assert out["recipient"] == "colleen.sullivan@enron.com"
    assert out["communication_date"] == "2000-08-09T14:11:00+00:00"


def test_unknown_class_extraction_merges_model_keys_and_header():
    """Model-provided correspondence-shaped keys win; the header pass fills
    the missing grounded fields; cross-class model keys are dropped."""
    out = validate_triage(
        {
            "primary_doc_class": "unknown",
            "confidence": 0.3,
            "gist": "unclear",
            "extraction": {
                "sender": "model-guessed@x.com",
                "parties": ["Enron", "El Paso"],  # cross-class — dropped
                "action_items": ["call back"],
            },
        },
        doc_text="From: phillip.allen@enron.com\nTo: colleen.sullivan@enron.com\nDate: 2000-08-09\n",
    )
    ext = out["extraction"]
    assert ext["sender"] == "model-guessed@x.com"  # model key wins
    assert "parties" not in ext
    assert ext["action_items"] == ["call back"]
    assert ext["recipient"] == "colleen.sullivan@enron.com"  # header fills
    assert ext["communication_date"] == "2000-08-09"


def test_unknown_class_fallback_never_invents_when_no_header():
    out = validate_triage(
        {"primary_doc_class": "unknown", "confidence": 0.1, "gist": "gibberish"},
        doc_text="qvx zzrq 12345 blah blah",
    )
    assert out["extraction"] == {}


def test_validate_triage_drops_non_schema_extraction_unknown_fields():
    out = validate_triage(
        {
            "primary_doc_class": "insurance_claim",
            "confidence": 0.9,
            "gist": "fnol",
            "extraction": {
                "claim_number": "2026-CLM-041701",
                "insurer": "Acme Insurance",
                "claimed_amount": 18530.0,
                "not_a_real_field": "junk",
                "denial_reasons": ["late notice", "docs"],
            },
        }
    )
    ext = out["extraction"]
    assert ext["claim_number"] == "2026-CLM-041701"
    assert ext["insurer"] == "Acme Insurance"
    assert ext["claimed_amount"] == 18530.0
    assert ext["denial_reasons"] == ["late notice", "docs"]
    assert "not_a_real_field" not in ext


def test_validate_triage_caps_list_and_string_fields():
    out = validate_triage(
        {
            "primary_doc_class": "correspondence",
            "confidence": 0.9,
            "gist": "x",
            "extraction": {
                "action_items": [f"item {i}" for i in range(50)],
                "sender": "x" * 500,
            },
        }
    )
    assert len(out["extraction"]["action_items"]) <= 10
    assert len(out["extraction"]["sender"]) <= 200


def test_triage_agent_returns_extraction(mock_openai_client, sample_insurance_claim_text):
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = json.dumps(
        {
            "primary_doc_class": "insurance_claim",
            "doc_subclass": "other",
            "confidence": 0.91,
            "gist": "FNOL for hail damage",
            "keywords": ["hail"],
            "extraction": {
                "claim_number": "2026-CLM-041701",
                "insurer": "Acme Insurance",
                "claimed_amount": 18530.0,
            },
        }
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["extraction"]["claim_number"] == "2026-CLM-041701"
    assert out["extraction"]["insurer"] == "Acme Insurance"
    assert out["extraction"]["claimed_amount"] == 18530.0


def test_triage_agent_short_correspondence_gets_key_entities(
    mock_openai_client, sample_correspondence_text
):
    """A SHORT Enron-like email must yield concise key entities (HUB-048)."""
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = json.dumps(
        {
            "primary_doc_class": "correspondence",
            "doc_subclass": "email",
            "confidence": 0.98,
            "gist": "A short internal email about a meeting.",
            "keywords": ["transportation"],
            "extraction": {
                "sender": "phillip.allen@enron.com",
                "recipient": "colleen.sullivan@enron.com",
                "communication_date": "2000-08-09",
                "subject_matter": "transportation model",
                "action_items": ["Keith Holst will attend"],
            },
        }
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(sample_correspondence_text, filename="email.md")
    assert out["primary_doc_class"] == "correspondence"
    assert out["extraction"]["sender"] == "phillip.allen@enron.com"
    assert out["extraction"]["recipient"] == "colleen.sullivan@enron.com"
    assert out["extraction"]["communication_date"] == "2000-08-09"
    assert out["extraction"]["action_items"] == ["Keith Holst will attend"]


def test_echo_renders_intake_triage_extraction(temp_base_dir):
    """The completion echo's INTAKE TRIAGE section renders key entities."""
    from pipeline import gmail_intake
    from pipeline.gmail_intake import build_echo_body

    body = build_echo_body(
        {
            "stage": "archived",
            "doc_type": "correspondence",
            "doc_subclass": "email",
            "classification_confidence": 0.98,
            "intake": {
                "sender": "phillip.allen@enron.com",
                "received_at": "2026-09-04T00:00:00Z",
                "triage": {
                    "primary_doc_class": "correspondence",
                    "doc_subclass": "email",
                    "confidence": 0.98,
                    "gist": "short email",
                    "keywords": ["transportation"],
                    "extraction": {
                        "sender": "phillip.allen@enron.com",
                        "recipient": "colleen.sullivan@enron.com",
                        "communication_date": "2000-08-09",
                        "action_items": ["attend Friday"],
                    },
                },
            },
        },
        [],
    )
    assert "EXTRACTED KEY ENTITIES (triage):" in body
    assert "sender: phillip.allen@enron.com" in body
    assert "recipient: colleen.sullivan@enron.com" in body
    assert "communication_date: 2000-08-09" in body
    assert "action_items: attend Friday" in body


def test_echo_renders_triage_debug_on_parse_failure(temp_base_dir):
    """When the free model's answer is unparseable the echo surfaces WHY and
    where the full I/O payloads live (human debugging directive)."""
    from pipeline.gmail_intake import build_echo_body

    body = build_echo_body(
        {
            "stage": "review",
            "doc_type": "unknown",
            "classification_confidence": None,
            "intake": {
                "sender": "axios337@gmail.com",
                "triage": {
                    "primary_doc_class": "unknown",
                    "confidence": 0.0,
                    "gist": "",
                    "keywords": [],
                    "debug": {
                        "model": "nvidia/nemotron-3.5-lightning:free",
                        "attempted_models": ["z-ai/glm-5.2:free"],
                        "parse_ok": False,
                        "parse_error": "no parseable json object in response",
                        "input_chars": 2803,
                        "response_chars": 512,
                        "debug_dir": "/tmp/base/debug/triage/20260904T000000Z_x",
                    },
                },
            },
        },
        [],
    )
    assert "triage debug:" in body
    assert "no parseable json object in response" in body
    assert "nvidia/nemotron-3.5-lightning:free" in body
    assert "/tmp/base/debug/triage/20260904T000000Z_x" in body


def test_triage_debug_reports_actual_serving_model(mock_openai_client, sample_insurance_claim_text):
    """Failover-aware debug capture: the request-layer wrapper records the
    model that ACTUALLY served the read — the agent-level self.model stays
    the primary even when the retry ladder rotated to another swarm member
    (live-verified 2026-09-04: logs said glm, nemotron served)."""
    from unittest.mock import MagicMock

    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = json.dumps(
        {"primary_doc_class": "insurance_claim", "confidence": 0.9, "gist": "FNOL"}
    )
    resp = mock_openai_client.chat.completions.create.return_value
    resp.choices = [choice]
    resp.model = "nvidia/nemotron-3.5-lightning:free"

    out = agent.triage(sample_insurance_claim_text, filename="claim.txt")
    assert out["primary_doc_class"] == "insurance_claim"
    # the response object's model (what actually served) wins over the
    # agent-level primary; the request was still ASKED for the conftest model
    assert out["debug"]["model"] == "nvidia/nemotron-3.5-lightning:free"
    assert out["debug"]["attempted_models"] == ["test-model"]
    assert out["debug"]["parse_ok"] is True


def test_triage_handles_real_enron_short_email_key_entities(mock_openai_client):
    """The free triage lane extracts key entities from the REAL short Enron
    email (allen-p/_sent_mail, 542 bytes — the human-cited 'short
    correspondence' shape) verbatim from its body, via the existing
    CorrespondenceExtraction schema (HUB-048)."""
    from unittest.mock import MagicMock

    enron_email = """# Re: TRANSPORTATION MODEL

From: phillip.allen@enron.com
To: colleen.sullivan@enron.com
Date: 2000-08-09T14:11:00+00:00

Colleen,

I am out of the office on Friday, but Keith Holst will attend. He has been
managing the Transport on the west desk.

Phillip
"""
    agent = GmailTriageAgent()
    choice = MagicMock()
    choice.message.content = json.dumps(
        {
            "primary_doc_class": "correspondence",
            "doc_subclass": "email",
            "confidence": 0.97,
            "gist": "A short email about Friday coverage.",
            "keywords": ["transportation", "Friday"],
            "extraction": {
                "sender": "phillip.allen@enron.com",
                "recipient": "colleen.sullivan@enron.com",
                "communication_date": "2000-08-09",
                "subject_matter": "transportation model",
                "action_items": ["Keith Holst will attend for Phillip"],
                "intent": "coordination",
            },
        }
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]

    out = agent.triage(enron_email, filename="email-allen-p-193.md")
    assert out["primary_doc_class"] == "correspondence"
    assert out["doc_subclass"] == "email"
    ext = out["extraction"]
    assert ext["sender"] == "phillip.allen@enron.com"
    assert ext["recipient"] == "colleen.sullivan@enron.com"
    assert ext["communication_date"] == "2000-08-09"
    assert ext["action_items"] == ["Keith Holst will attend for Phillip"]


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


def _gmail_inbox_file(temp_base_dir, name="fnol_triage.txt", route="triage"):
    from pipeline.bins import inbox_dir, write_inbox_meta

    inbox_file = inbox_dir() / name
    inbox_file.write_text("ACME INSURANCE COMPANY — FNOL hail damage to roof and garage")
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="MATTER-TR",
        message_id=f"<msg-{name}@example.com>",
        sender="client@firm.example",
        subject="FNOL [M:MATTER-TR]",
        route=route,
    )
    return inbox_file


def _terminal_manifest(temp_base_dir, filename):
    from pipeline.bins import manifests_dir
    from pathlib import Path

    for mf in manifests_dir().glob("*.json"):
        data = json.loads(mf.read_text())
        if data.get("original_filename") == filename and data.get("stage") in (
            "archived",
            "failed",
            "review",
        ):
            return data
    return None


def test_watcher_single_doc_gmail_runs_triage_lane(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    instance = fake_agent.return_value
    instance.triage.return_value = {
        "primary_doc_class": "insurance_claim",
        "doc_subclass": "other",
        "confidence": 0.9,
        "gist": "FNOL for hail damage",
        "keywords": ["hail"],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline")

    Watcher()._process_existing(inbox_file)

    # The triage lane runs; the paid pipeline is NEVER called.
    instance.triage.assert_called_once()
    spy.assert_not_called()

    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    assert manifest is not None
    assert manifest["stage"] == "archived"
    assert manifest["doc_type"] == "insurance_claim"
    assert manifest["intake"]["route"] == "triage"
    assert manifest["intake"]["triage"]["primary_doc_class"] == "insurance_claim"
    assert manifest["intake"]["triage"]["gist"] == "FNOL for hail damage"


def test_triage_lane_writes_own_audit_section(temp_base_dir, mocker):
    import asyncio

    from pipeline.watcher import Watcher
    from storage.audit_log import get_audit_chain

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol_audit.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.return_value = {
        "primary_doc_class": "insurance_claim",
        "doc_subclass": None,
        "confidence": 0.9,  # > taxonomy low (0.88) → archive terminal
        "gist": "FNOL",
        "keywords": ["hail"],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)

    Watcher()._process_existing(inbox_file)

    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    rows = asyncio.run(get_audit_chain(manifest["doc_id"]))
    events = [r["event"] for r in rows]
    assert events == ["triage_ingested", "triage_classified", "triage_archived"]
    assert all(e.startswith("triage_") for e in events)  # own section — never pipeline events
    assert rows[-1]["detail"]["archive_path"].endswith("fnol_audit.txt")
    assert rows[-1]["detail"]["file_sha256"]


# ── lane review routing (confidence/unknown gate) ─────────────────────────


def test_watcher_triage_low_confidence_routes_to_review(temp_base_dir, mocker):
    """Confidence below the taxonomy `low` (0.88) parks the doc for HUMAN
    REVIEW instead of archiving a possibly-wrong classification."""
    from pipeline.bins import review_dir
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol_lowconf.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.return_value = {
        "primary_doc_class": "insurance_claim",
        "doc_subclass": None,
        "confidence": 0.5,
        "gist": "FNOL?",
        "keywords": ["hail"],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline")

    Watcher()._process_existing(inbox_file)

    spy.assert_not_called()
    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    assert manifest is not None
    assert manifest["stage"] == "review"
    assert manifest["doc_type"] == "insurance_claim"
    assert "triage_low_confidence" in (manifest["escalation_reason"] or "")
    # The file is parked in the review bin.
    parked = [p for p in review_dir().iterdir() if p.name == inbox_file.name]
    assert len(parked) == 1


def test_watcher_triage_low_confidence_review_audit_and_echo(temp_base_dir, mocker):
    """The review terminal writes a triage_reviewed audit entry and the echo
    dispatches with the REVIEW stage (so the sender gets why/next-steps)."""
    import asyncio

    from pipeline.watcher import Watcher
    from storage.audit_log import get_audit_chain

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol_review_audit.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.return_value = {
        "primary_doc_class": "insurance_claim",
        "doc_subclass": None,
        "confidence": 0.4,
        "gist": "FNOL?",
        "keywords": [],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    echo_spy = mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)

    Watcher()._process_existing(inbox_file)

    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    rows = asyncio.run(get_audit_chain(manifest["doc_id"]))
    events = [r["event"] for r in rows]
    assert events == ["triage_ingested", "triage_classified", "triage_reviewed"]
    assert rows[-1]["detail"]["review_path"].endswith("fnol_review_audit.txt")
    echo_spy.assert_called_once()
    echoed = echo_spy.call_args.args[0]
    assert echoed["stage"] == "review"


def test_watcher_triage_unknown_class_routes_to_review(temp_base_dir, mocker):
    """An `unknown` class parks for human review (never archived as unknown)
    while keeping the best-effort extraction for the reviewer."""
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name="weird_doc.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.return_value = {
        "primary_doc_class": "unknown",
        "doc_subclass": None,
        "confidence": 0.3,
        "gist": "unclear",
        "keywords": [],
        "extraction": {"sender": "a@b.c", "recipient": "d@e.f"},
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)

    Watcher()._process_existing(inbox_file)

    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    assert manifest is not None
    assert manifest["stage"] == "review"
    assert manifest["doc_type"] == "unknown"
    assert "triage_unknown_class" in (manifest["escalation_reason"] or "")
    assert manifest["intake"]["triage"]["extraction"]["sender"] == "a@b.c"


def test_watcher_multi_doc_gmail_runs_full_pipeline_without_triage(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name="bundle_a.txt", route="pipeline")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    # Multi-document uploads: full paid pipeline, triage approach dropped.
    fake_agent.assert_not_called()
    assert spy.call_count == 1
    assert (spy.call_args.kwargs.get("intake_meta") or {}).get("route") == "pipeline"


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


def test_watcher_triage_disabled_falls_back_to_pipeline(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir)

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=False)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()
    assert spy.call_count == 1  # single-doc emails fall back to the full pipeline


def test_watcher_claim_dispatches_reaction_on_both_routes(temp_base_dir, mocker):
    """The ✅ reaction fires at claim time REGARDLESS of route — the triage
    lane (single-doc) and the full pipeline (multi-doc) both get it."""
    from pipeline.watcher import Watcher

    # Triage-lane claim (single-document Gmail).
    inbox_triage = _gmail_inbox_file(temp_base_dir, name="fnol_react_triage.txt", route="triage")
    mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.watcher._run_triage_lane", return_value={"doc_id": "t1"})
    react_spy = mocker.patch("pipeline.watcher._notify_intake_reaction")

    Watcher()._process_existing(inbox_triage)

    react_spy.assert_called_once()
    intake = react_spy.call_args.args[0]
    assert intake["source"] == "gmail"
    assert intake["route"] == "triage"

    # Full-pipeline claim (multi-document Gmail).
    react_spy.reset_mock()
    inbox_pipeline = _gmail_inbox_file(temp_base_dir, name="fnol_react_pipeline.txt", route="pipeline")
    mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "p1"})

    Watcher()._process_existing(inbox_pipeline)

    react_spy.assert_called_once()
    intake = react_spy.call_args.args[0]
    assert intake["source"] == "gmail"
    assert intake["route"] == "pipeline"


def test_triage_lane_failure_fails_soft(temp_base_dir, mocker):
    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol_fail.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.side_effect = RuntimeError("free model down")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline")

    Watcher()._process_existing(inbox_file)

    # A triage failure must never crash the intake: the document parks to
    # failed/ with a terminal manifest and the paid pipeline stays untouched.
    assert spy.call_count == 0
    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    assert manifest is not None and manifest["stage"] == "failed"


def test_triage_lane_llm_failure_parks_in_review(temp_base_dir, mocker):
    """A transient free-team failure (e.g. upstream 429 rate limits) is NOT a
    document defect: the unclassified doc parks in REVIEW with
    `triage_llm_unavailable` — never the failed bin (HUB-049)."""
    import asyncio

    from pipeline.bins import review_dir
    from pipeline.watcher import Watcher
    from storage.audit_log import get_audit_chain

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol_agentfail.txt")

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.side_effect = RuntimeError("rate limited")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    echo_spy = mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline")

    Watcher()._process_existing(inbox_file)

    # The paid pipeline stays untouched; the document parks in review.
    spy.assert_not_called()
    manifest = _terminal_manifest(temp_base_dir, inbox_file.name)
    assert manifest is not None
    assert manifest["stage"] == "review"
    assert manifest["doc_type"] == "unknown"
    assert (manifest["escalation_reason"] or "").startswith("triage_llm_unavailable")
    parked = [p for p in review_dir().iterdir() if p.name == inbox_file.name]
    assert len(parked) == 1
    # Audit: ingested + reviewed, and NEVER classified (the read never ran).
    rows = asyncio.run(get_audit_chain(manifest["doc_id"]))
    assert [r["event"] for r in rows] == ["triage_ingested", "triage_reviewed"]
    assert rows[-1]["detail"]["escalation_reason"].startswith("triage_llm_unavailable")
    # The sender gets the soft ⏸ echo, not ❌.
    echo_spy.assert_called_once()
    echoed = echo_spy.call_args.args[0]
    assert echoed["stage"] == "review"


# ── all document types through the triage lane (HUB-037) ─────────────────


@pytest.mark.parametrize(
    "fixture_name,doc_class",
    [
        ("sample_contract_text", "contract"),
        ("sample_contract_text", "merger_agreement"),
        ("sample_insurance_claim_text", "insurance_claim"),
        ("sample_corporate_text", "corporate_record"),
        ("sample_correspondence_text", "correspondence"),
        ("sample_compliance_text", "compliance_filing"),
    ],
)
def test_triage_lane_accepts_all_doc_types(temp_base_dir, mocker, request, fixture_name, doc_class):
    """The free triage team can process + accept EVERY canonical doc type —
    contracts, merger agreements, insurance claims, corporate records,
    correspondences, and compliance filings — as single-document Gmail
    inputs."""
    from pipeline.bins import inbox_dir, write_inbox_meta
    from pipeline.watcher import Watcher

    text = request.getfixturevalue(fixture_name)
    name = f"doc_{doc_class}.txt"
    inbox_file = inbox_dir() / name
    inbox_file.write_text(text)
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="M-ALL",
        message_id=f"<msg-{doc_class}@example.com>",
        sender="client@firm.example",
        route="triage",
    )

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    fake_agent.return_value.triage.return_value = {
        "primary_doc_class": doc_class,
        "doc_subclass": None,
        "confidence": 0.9,
        "gist": f"A {doc_class} document",
        "keywords": ["test"],
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline")

    Watcher()._process_existing(inbox_file)

    fake_agent.return_value.triage.assert_called_once()
    spy.assert_not_called()  # the free lane handles it; the paid pipeline is untouched
    manifest = _terminal_manifest(temp_base_dir, name)
    assert manifest is not None and manifest["stage"] == "archived"
    assert manifest["doc_type"] == doc_class
    assert manifest["intake"]["triage"]["primary_doc_class"] == doc_class
    assert manifest["intake"]["route"] == "triage"


# ── capability pre-check + honest handoff (HUB-037) ──────────────────────


def test_triage_handoff_when_document_exceeds_free_budget(temp_base_dir, mocker):
    """Documents longer than the free agent's input budget (e.g. merger
    agreements) are handed off to the full pipeline BEFORE a failed run."""
    from pipeline.bins import inbox_dir, write_inbox_meta
    from pipeline.watcher import Watcher

    inbox_file = inbox_dir() / "fnol_long.txt"
    inbox_file.write_text("A" * 20000)  # far beyond the 12000-char free budget
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="M-9",
        message_id="<msg-long@example.com>",
        sender="c@firm.example",
        route="triage",
    )

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()  # never starts a doomed free-model run
    assert spy.call_count == 1  # honestly handed off to the full pipeline
    intake = spy.call_args.kwargs.get("intake_meta") or {}
    assert intake["triage_handoff"].startswith("exceeds_free_budget:")


def test_triage_handoff_for_image_document(temp_base_dir, mocker):
    from pipeline.bins import inbox_dir, write_inbox_meta
    from pipeline.watcher import Watcher

    inbox_file = inbox_dir() / "scan.png"
    inbox_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="M-9",
        message_id="<msg-img@example.com>",
        sender="c@firm.example",
        route="triage",
    )

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()
    assert spy.call_count == 1  # vision-only input → the full pipeline
    intake = spy.call_args.kwargs.get("intake_meta") or {}
    assert intake["triage_handoff"] == "image_requires_vision"


def test_triage_handoff_for_scanned_pdf(temp_base_dir, mocker):
    from pipeline.bins import inbox_dir, write_inbox_meta
    from pipeline.watcher import Watcher

    inbox_file = inbox_dir() / "scan.pdf"
    inbox_file.write_bytes(b"%PDF-1.4\n% fake scanned pdf - no direct text\n")
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="M-9",
        message_id="<msg-scan@example.com>",
        sender="c@firm.example",
        route="triage",
    )

    fake_agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})

    Watcher()._process_existing(inbox_file)

    fake_agent.assert_not_called()
    assert spy.call_count == 1
    intake = spy.call_args.kwargs.get("intake_meta") or {}
    assert intake["triage_handoff"] == "scanned_pdf_requires_transcription"


def test_triage_capability_check_within_budget(temp_base_dir):
    from pipeline.bins import inbox_dir
    from pipeline.watcher import _triage_capability_check

    short = inbox_dir() / "short.txt"
    short.write_text("FNOL hail damage — fits the free budget")
    ok, reason = _triage_capability_check(short)
    assert ok is True and reason is None


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


def test_build_echo_body_renders_triage_handoff():
    manifest = {
        "doc_id": "d-echo-handoff",
        "matter_id": "M-1",
        "original_filename": "merger_agreement.pdf",
        "stage": "archived",
        "intake": {
            "source": "gmail",
            "message_id": "<echo-handoff@example.com>",
            "sender": "client@firm.example",
            "subject": "Merger [M:M-1]",
            "triage_handoff": "exceeds_free_budget:25000>12000",
        },
    }
    body = gmail_intake.build_echo_body(manifest, [], None)
    assert "triage handoff: exceeds_free_budget:25000>12000" in body
    assert "handled by the full pipeline" in body