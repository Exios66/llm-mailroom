"""Intake agent v2 (HUB-038) — triage + clean + prepare, sliding windows.

The no-truncation doctrine is the heart of this suite: documents past any
input budget are processed in overlapping sliding windows and merged — every
character is read by at least one window, and no window ever exceeds the
budget (so the vendored HEAD+TAIL truncation can never fire).
"""

import json

from unittest.mock import MagicMock

from agents.intake import (
    INTAKE_SCHEMA,
    IntakeAgent,
    format_intake_prior,
    should_llm_intake,
    sliding_windows,
    validate_intake,
)
from langchain_agents.mock import FakeLangChainLLM, is_classify_call, user_text_from_messages


def _intake_json(
    cls="contract",
    confidence=0.9,
    gist="A master services agreement.",
    cleaned_text=None,
    sections=None,
):
    return json.dumps(
        {
            "triage": {
                "primary_doc_class": cls,
                "doc_subclass": "msa" if cls == "contract" else None,
                "confidence": confidence,
                "gist": gist,
                "keywords": ["services", "term"],
            },
            "cleaned_text": cleaned_text,
            "changes_applied": [] if cleaned_text is None else ["joined run-together lines"],
            "sections": sections
            or [{"heading": "Agreement", "role": "other", "start_offset": 0, "end_offset": 40}],
        }
    )


def _queued_client(responses):
    """OpenAI-shaped mock whose completions.create returns queued contents."""
    mock_choice = MagicMock()
    mock_completion = MagicMock()
    mock_chat = MagicMock()

    def _create(**kwargs):
        content = responses.pop(0) if responses else "{}"
        mock_choice.message.content = content
        mock_completion.choices = [mock_choice]
        return mock_completion

    mock_chat.completions.create.side_effect = _create
    mock_client = MagicMock()
    mock_client.chat = mock_chat
    return mock_client


def _agent_with(mock_client):
    agent = IntakeAgent()
    agent.client = mock_client
    agent.model = "test-model"
    return agent


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_gate_skips_clean_short_text():
    text = "Hello world.\n\n1. Clause."
    assert should_llm_intake(text, {"messy": False}) is False


def test_gate_fires_for_messy_text():
    text = "garbled OCR line one\n" * 40
    assert should_llm_intake(text, {"messy": True}) is True


def test_gate_fires_for_over_budget_text():
    text = "A normal-looking paragraph. " * 1200
    assert should_llm_intake(text, {"messy": False}) is True


def test_gate_skips_empty_text():
    assert should_llm_intake("", {"messy": False}) is False
    assert should_llm_intake("   ", {"messy": False}) is False


# ---------------------------------------------------------------------------
# validate_intake
# ---------------------------------------------------------------------------


def test_validate_clamps_triage_vocabulary():
    out = validate_intake(
        {
            "triage": {
                "primary_doc_class": "Court Opinion",  # not in the live taxonomy
                "confidence": 5.0,
                "gist": "x",
            },
            "sections": [],
        },
        "some text",
    )
    assert out["triage"]["primary_doc_class"] == "unknown"
    assert out["triage"]["confidence"] == 1.0


def test_validate_drops_out_of_bounds_and_overlapping_sections():
    out = validate_intake(
        {
            "triage": {"primary_doc_class": "contract", "confidence": 0.9, "gist": "g"},
            "sections": [
                {"heading": "A", "role": "term", "start_offset": 0, "end_offset": 50},
                {"heading": "B", "role": "recitals", "start_offset": 25, "end_offset": 60},
                {"heading": "C", "role": "governing_law", "start_offset": 70, "end_offset": 9000},
                {"heading": "D", "role": "not-a-role", "start_offset": 5, "end_offset": -3},
                {"heading": "E", "role": "parties", "start_offset": 80, "end_offset": 90},
            ],
        },
        "text" * 30,
    )
    sections = out["sections"]
    assert len(sections) == 2  # B dropped as overlap, C out of bounds, D invalid; A + E kept
    assert [s["heading"] for s in sections] == ["A", "E"]
    assert sections[0]["role"] == "term"
    assert sections[1]["role"] == "parties"


def test_validate_caps_sections_at_40_and_clamps_role():
    many = [
        {"heading": f"H{i}", "role": "other", "start_offset": i * 60, "end_offset": i * 60 + 40}
        for i in range(50)
    ]
    out = validate_intake(
        {"triage": {"primary_doc_class": "unknown", "confidence": 0.0, "gist": ""}, "sections": many},
        "text" * 1000,
    )
    assert len(out["sections"]) == 40


def test_validate_bounds_cleaned_text():
    out = validate_intake(
        {
            "triage": {"primary_doc_class": "unknown", "confidence": 0.0, "gist": ""},
            "cleaned_text": "x" * 5000,
            "sections": [],
        },
        "abc",
    )
    assert len(out["cleaned_text"]) <= 3 * 3 + 2000


# ---------------------------------------------------------------------------
# sliding_windows — the no-truncation guarantee
# ---------------------------------------------------------------------------


def test_sliding_windows_cover_every_character():
    text = "\n\n".join(f"Paragraph number {i} with some words." for i in range(200))
    windows = sliding_windows(text, 1200, 200)
    assert len(windows) > 1
    for window, base in windows:
        assert len(window) <= 1200 + 220  # window = chunk + overlap prefix
    joined = "".join(chunk for chunk, base in windows)
    for needle in ("Paragraph number 0", "Paragraph number 99", "Paragraph number 199"):
        assert needle in joined
    bases = [b for _, b in windows]
    assert bases == sorted(bases)
    assert bases[0] == 0
    # full coverage: the chunk spans (excluding re-sent overlap prefixes) are
    # contiguous and reach the end of the text — every character is read.
    last_end = 0
    for i, (chunk, base) in enumerate(windows):
        overlap_prefix = 0
        if i > 0:
            prev = windows[i - 1][0]
            tail = prev[-200:]
            if "\n\n" in tail:
                tail = tail[tail.find("\n\n") + 2:]
            overlap_prefix = len(tail) + 2
        span_end = base + len(chunk) - overlap_prefix
        assert span_end > last_end or i == 0
        last_end = span_end
    assert last_end >= len(text) - 5


def test_sliding_windows_single_window_when_within_budget():
    windows = sliding_windows("short text", 1200, 100)
    assert windows == [("short text", 0)]


def test_sliding_windows_hard_splits_pathological_paragraph():
    text = "A" * 5000
    windows = sliding_windows(text, 1200, 100)
    assert len(windows) >= 4
    for chunk, base in windows:
        assert len(chunk) <= 1200 + 102 + 5  # budget + overlap prefix + slack
    assert "".join(c for c, _ in windows).count("A") >= 5000


# ---------------------------------------------------------------------------
# IntakeAgent — single window (clean short path) and cleaning
# ---------------------------------------------------------------------------


def test_intake_agent_single_window_triage(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_INTAKE", raising=False)
    agent = _agent_with(_queued_client([_intake_json()]))
    prep = agent.intake_run("An agreement between A and B.\n\nIt has a term of 2 years.")
    assert prep["windows"] == 1
    assert prep["triage"]["primary_doc_class"] == "contract"
    assert prep["sections"][0]["start_offset"] == 0
    assert "cleaned" not in prep


def test_intake_agent_applies_cleaning_when_repaired(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_INTAKE", raising=False)
    repaired = "RUN-TOGETHER\nline one\n\nline two\n\nClean paragraph."
    agent = _agent_with(_queued_client([_intake_json(cleaned_text=repaired)]))
    prep = agent.intake_run("RUN-TOGETHER\nline one\nline two\n\nClean paragraph.")
    assert prep["cleaned"] == repaired
    assert prep["changed"] is True


def test_intake_agent_fails_soft_on_parse_error(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_INTAKE", raising=False)
    agent = _agent_with(_queued_client(["not json at all"]))
    prep = agent.intake_run("Some text.")
    assert prep["triage"].get("primary_doc_class") == "unknown"
    assert prep["sections"] == []
    assert "cleaned" not in prep


# ---------------------------------------------------------------------------
# IntakeAgent — sliding windows merge (no truncation)
# ---------------------------------------------------------------------------


def test_intake_agent_slides_and_merges_windows(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_INTAKE", raising=False)
    text = "\n\n".join(f"Section paragraph number {i}." for i in range(4000))
    window_2 = _intake_json(
        cls="correspondence",
        confidence=0.95,
        gist="A letter from the middle of the document.",
        sections=[
            {"heading": "Mid", "role": "other", "start_offset": 10, "end_offset": 40},
            {"heading": "Tail", "role": "signatures", "start_offset": 50, "end_offset": 70},
        ],
    )
    responses = [_intake_json()] + [window_2] * 10
    agent = _agent_with(_queued_client(responses))
    prep = agent.intake_run(text)
    assert prep["windows"] > 1
    # plurality: contract appears once, correspondence many → correspondence
    assert prep["triage"]["primary_doc_class"] == "correspondence"
    assert prep["cleaned_text"] is None  # partial windows never splice
    assert "cleaned" not in prep
    # translated offsets: the mid-window section sits inside the 2nd+ window
    starts = [s["start_offset"] for s in prep["sections"]]
    assert any(start > 0 for start in starts)


# ---------------------------------------------------------------------------
# format_intake_prior
# ---------------------------------------------------------------------------


def test_format_intake_prior_renders_advisory_block():
    prior = format_intake_prior(
        {
            "triage": {
                "primary_doc_class": "contract",
                "doc_subclass": "msa",
                "confidence": 0.9,
                "gist": "Master services agreement.",
                "keywords": ["services"],
            }
        }
    )
    assert "advisory read by the intake clerk" in prior
    assert "primary class: contract" in prior
    assert "subclass: msa" in prior
    assert "gist: Master services agreement." in prior


def test_format_intake_prior_empty_without_triage():
    assert format_intake_prior(None) == ""
    assert format_intake_prior({"sections": []}) == ""


# ---------------------------------------------------------------------------
# Sorter — sliding-window classification merge (no truncation)
# ---------------------------------------------------------------------------


class _QueuedFakeLLM(FakeLangChainLLM):
    def __init__(self, classifications):
        super().__init__()
        self._queue = list(classifications)

    def _run(self, messages):
        self.calls += 1
        text = user_text_from_messages(messages)
        if is_classify_call(text) and self._queue:
            parsed = dict(self._queue.pop(0))
        else:
            parsed = dict(self.classification if is_classify_call(text) else self.extraction)
        if self.on_call:
            self.on_call(text, parsed)
        return self._make_message(parsed)


def test_sorter_slides_over_budget_and_merges(mocker):
    from agents.sorter import SorterAgent

    seen_texts = []

    def _on_call(text, parsed):
        seen_texts.append(text)

    fake = _QueuedFakeLLM(
        [
            {"doc_type": "contract", "contract_subtype": "license", "confidence": 0.9, "reasoning": "head window"},
            {"doc_type": "contract", "contract_subtype": "license", "confidence": 0.85, "reasoning": "middle window"},
            {"doc_type": "unknown", "contract_subtype": None, "confidence": 0.2, "reasoning": "sparse window"},
        ]
        + [
            {"doc_type": "contract", "contract_subtype": "license", "confidence": 0.99, "reasoning": "tail window"}
        ]
        * 6
    )
    fake.on_call = _on_call
    mocker.patch("langchain_agents.base_agent.BaseAgent.llm", new=lambda self: fake)

    text = "\n\n".join(f"Clause paragraph {i} about obligations and terms." for i in range(900))
    marker = "CRITICAL-GOVERNING-LAW-TOKEN"
    text = text[:15000] + marker + text[15000:]
    sorter = SorterAgent()
    result = sorter.classify_json(text)
    assert result["doc_type"] == "contract"
    assert result["contract_subtype"] == "license"
    assert 0.8 < result["confidence"] <= 0.99  # mean of the agreeing windows
    assert sorter._last_windows >= 3
    assert sorter._last_truncated is False  # vendored HEAD+TAIL never fired
    # the no-truncation proof: the middle-of-document marker was actually read
    assert any(marker in seen for seen in seen_texts)


def test_sorter_single_call_within_budget_unchanged(mocker):
    from agents.sorter import SorterAgent

    fake = _QueuedFakeLLM(
        [{"doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "mock"}]
    )
    mocker.patch("langchain_agents.base_agent.BaseAgent.llm", new=lambda self: fake)
    sorter = SorterAgent()
    result = sorter.classify_json("Small agreement with a term of one year.")
    assert result["doc_type"] == "contract"
    assert sorter._last_windows == 1


def test_sorter_prior_and_prefix_reach_every_window(mocker):
    from agents.sorter import SorterAgent

    seen_texts = []

    def _on_call(text, parsed):
        seen_texts.append(text)

    fake = _QueuedFakeLLM(
        [
            {"doc_type": "contract", "contract_subtype": None, "confidence": 0.7, "reasoning": "w1"},
            {"doc_type": "contract", "contract_subtype": None, "confidence": 0.7, "reasoning": "w2"},
        ]
    )
    fake.on_call = _on_call
    mocker.patch("langchain_agents.base_agent.BaseAgent.llm", new=lambda self: fake)

    text = "\n\n".join(f"Paragraph {i} with contractual language." for i in range(600))
    sorter = SorterAgent()
    sorter.classify_json(
        text,
        intake_prior="[intake prior — advisory] primary class: contract",
        prefix="RE-EVALUATION REQUESTED - previous classification was 'contract'",
    )
    assert len(seen_texts) >= 2
    for seen in seen_texts:
        assert "advisory" in seen
        assert "RE-EVALUATION REQUESTED" in seen


# ---------------------------------------------------------------------------
# ingest wiring (HUB-038): gate + manifest triage + state
# ---------------------------------------------------------------------------


def test_intake_node_llm_intake_merges_triage_into_manifest(temp_base_dir, mocker):
    mocker.patch("agents.intake.should_llm_intake", return_value=True)
    mocker.patch("agents.intake.llm_intake_enabled", return_value=True)

    from graph.build_graph import _ensure_dirs, intake_node
    from graph.state import DocumentState

    _ensure_dirs()
    inbox = temp_base_dir / "pipeline" / "inbox"
    test_file = inbox / "intake_v2_merge.txt"
    test_file.write_text("AGREEMENT\n\nParties: A and B.\n\nTerm: two years.\n\nSignatures.")
    from agents.intake import IntakeAgent

    mocker.patch.object(IntakeAgent, "intake_run", return_value={
        "triage": {"primary_doc_class": "contract", "doc_subclass": "msa", "confidence": 0.9,
                   "gist": "Agreement.", "keywords": ["term"]},
        "cleaned_text": None,
        "changes_applied": [],
        "sections": [{"heading": "Agreement", "role": "other", "start_offset": 0, "end_offset": 50}],
        "windows": 1,
    })
    state: DocumentState = {
        "doc_id": "",
        "matter_id": "TEST",
        "original_filename": "intake_v2_merge.txt",
        "stage": "inbox",
        "file_path": str(test_file),
        "doc_text": "",
        "messages": [],
        "intake_meta": {"source": "upload", "upload_id": "u-1"},
    }
    expected = test_file.read_text()  # claimed (moved) by ingest — read first
    result = intake_node(state)
    assert result["intake_prep"]["triage"]["primary_doc_class"] == "contract"
    assert "triage" in result["intake_prep"]
    assert result["doc_text"] == expected  # no cleaning when prep has none
    assert INTAKE_SCHEMA["required"] == ["triage", "sections"]


def test_intake_node_llm_intake_disabled_stays_deterministic(temp_base_dir, mocker):
    # conftest forces MAILROOM_LLM_INTAKE=0 → no IntakeAgent construction
    import agents.intake as intake_mod

    mocker.patch("agents.intake.should_llm_intake", return_value=True)
    from graph.build_graph import _ensure_dirs, intake_node
    from graph.state import DocumentState

    _ensure_dirs()
    inbox = temp_base_dir / "pipeline" / "inbox"
    test_file = inbox / "intake_v2_off.txt"
    test_file.write_text("agree-\nment\n\n\n\nNext paragraph.")
    state: DocumentState = {
        "doc_id": "",
        "matter_id": "TEST",
        "original_filename": "intake_v2_off.txt",
        "stage": "inbox",
        "file_path": str(test_file),
        "doc_text": "",
        "messages": [],
    }
    result = intake_node(state)
    assert result["intake_prep"] is None
    assert "agreement" in result["doc_text"]
    assert intake_mod.llm_intake_enabled() is False