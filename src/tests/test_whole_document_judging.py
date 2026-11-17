"""#277: the judge and the Lane B arbiter read the whole document, window by window.

Network-free. The judge's ``_call_structured`` is replaced by a scripted model; the window
size is set through ``_configured_max_input_chars`` so a short fixture needs several windows.
"""

import pytest

import agents.arbiter as arbiter_mod
import agents.judge as judge_mod
import graph.build_graph as bg
from agents.source_windows import source_windows, windows_for_budget
from graph.routing import after_arbiter, after_judge
from pipeline import limits

WINDOW_CHARS = 4000


def long_text(paragraphs: int = 24) -> str:
    return "\n\n".join(f"Paragraph {i}. " + "clause " * 120 for i in range(paragraphs))


CLEAN_COMPLETE = {"completeness": 1.0, "completeness_label": "complete", "reasoning": ""}
CLEAN_ACCURATE = {"extraction_correctness": 1.0, "extraction_correctness_label": "accurate", "reasoning": ""}


class ScriptedJudge:
    """Patch point: answers each judge call from ``script`` and records the user messages."""

    def __init__(self, monkeypatch, *, completeness=None, correctness=None, window_chars=WINDOW_CHARS):
        self.completeness = completeness or (lambda i: CLEAN_COMPLETE)
        self.correctness = correctness or (lambda i: CLEAN_ACCURATE)
        self.messages: list[str] = []
        self.calls = {"completeness": 0, "correctness": 0}
        outer = self

        def call(self_, message, json_schema, temperature=0.1, system_prompt=None, **kw):
            kind = "completeness" if "completeness" in json_schema["properties"] else "correctness"
            outer.calls[kind] += 1
            outer.messages.append(message)
            reply = (outer.completeness if kind == "completeness" else outer.correctness)(outer.calls[kind])
            if isinstance(reply, Exception):
                raise reply
            limits.record_usage({"prompt_tokens": 100, "completion_tokens": 10}, "m", agent="judge")
            return dict(reply)

        monkeypatch.setattr(judge_mod.CompletenessJudge, "_call_structured", call)
        monkeypatch.setattr(judge_mod.CompletenessJudge, "__init__", lambda self: None)
        monkeypatch.setattr(judge_mod.CompletenessJudge, "_configured_max_input_chars", lambda self: window_chars)
        limits.reset_run_usage()


def run_node(text, **state):
    return bg.judge_verify_node(
        {"doc_id": "d1", "doc_type": "contract", "extraction_confidence": 0.3, "doc_text": text,
         "extracted_data": {"parties": ["A", "B"]}, **state}
    )


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "on")


# -- windows ---------------------------------------------------------------------------------


def test_every_character_of_a_long_document_reaches_some_window():
    text = long_text()
    windows = windows_for_budget(text, WINDOW_CHARS)
    assert len(windows) > 1 and all(len(w) <= WINDOW_CHARS for w in windows)
    joined = "\n".join(windows)
    assert all(f"Paragraph {i}." in joined for i in range(24))


def test_a_document_inside_the_budget_is_one_whole_window_and_empty_is_one_empty_window():
    assert windows_for_budget("short", WINDOW_CHARS) == ["short"]
    assert windows_for_budget("", WINDOW_CHARS) == [""]


def test_window_size_comes_from_the_taxonomy_not_a_constant():
    from pipeline.config import get_agent_config

    judge = judge_mod.CompletenessJudge.__new__(judge_mod.CompletenessJudge)
    budget = int(get_agent_config("judge")["max_input_chars"])
    text = "x" * (budget * 3)
    windows = source_windows(judge, text)
    assert len(windows) >= 3 and max(len(w) for w in windows) <= budget


# -- aggregation -----------------------------------------------------------------------------


def test_completeness_takes_the_worst_label_the_lowest_score_and_every_finding():
    out = judge_mod.aggregate_completeness([
        {"completeness": 1.0, "completeness_label": "complete", "reasoning": ""},
        {"completeness": 0.6, "completeness_label": "partial", "reasoning": "dates omitted"},
        {"completeness": 0.2, "completeness_label": "incomplete", "reasoning": "parties omitted"},
        {"completeness": 0.97, "completeness_label": "complete", "reasoning": "fine"},
    ])
    assert out["completeness_label"] == "incomplete" and out["completeness"] == 0.2
    assert "[window 2 of 4] dates omitted" in out["reasoning"]
    assert "[window 3 of 4] parties omitted" in out["reasoning"]
    assert "fine" not in out["reasoning"]  # a clean window adds no filler


def test_correctness_takes_the_worst_label_and_the_lowest_score():
    out = judge_mod.aggregate_correctness([
        {"extraction_correctness": 1.0, "extraction_correctness_label": "accurate", "reasoning": ""},
        {"extraction_correctness": 0.5, "extraction_correctness_label": "partial", "reasoning": "wrong date"},
    ])
    assert out["extraction_correctness_label"] == "partial" and out["extraction_correctness"] == 0.5
    assert "wrong date" in out["reasoning"]


def test_one_window_is_returned_as_the_model_said_it():
    one = {"completeness": 0.7, "completeness_label": "partial", "reasoning": "gap"}
    assert judge_mod.aggregate_completeness([one]) == one


def test_every_window_of_a_long_document_is_judged_and_the_worst_wins(monkeypatch):
    fake = ScriptedJudge(
        monkeypatch,
        completeness=lambda i: CLEAN_COMPLETE if i != 3 else
        {"completeness": 0.55, "completeness_label": "partial", "reasoning": "missed the term"},
    )
    judge = judge_mod.CompletenessJudge()
    text = long_text()
    out = judge.judge_completeness("contract", {"parties": ["A"]}, text)
    n = len(windows_for_budget(text, WINDOW_CHARS))
    assert fake.calls["completeness"] == n > 2
    assert out["completeness_label"] == "partial" and "missed the term" in out["reasoning"]
    assert out["coverage"]["windows_total"] == out["coverage"]["windows_checked"] == n
    assert out["coverage"]["calls"] == n and out["coverage"]["prompt_tokens"] == 100 * n
    assert all("WINDOW" in m for m in fake.messages)
    assert all(f"Paragraph {i}." in "".join(fake.messages) for i in range(24))


def test_a_short_document_is_one_call_with_no_window_note(monkeypatch):
    fake = ScriptedJudge(monkeypatch, window_chars=100_000)
    out = judge_mod.CompletenessJudge().judge_completeness("contract", {"parties": ["A"]}, "y" * 20_000)
    assert fake.calls["completeness"] == 1 and "WINDOW" not in fake.messages[0]
    assert "truncated" not in fake.messages[0] and "y" * 20_000 in fake.messages[0]
    assert out["coverage"]["windows_total"] == 1


# -- the node: coverage, source_verified, fail closed ------------------------------------------


def test_an_unsure_document_over_16000_chars_archives_after_every_window_checks_clean(monkeypatch):
    """Acceptance 1: a long, still-unsure extraction clears only because every window was read."""
    fake = ScriptedJudge(monkeypatch)
    text = long_text()
    assert len(text) > 16_000
    update = run_node(text)
    n = len(windows_for_budget(text, WINDOW_CHARS))
    assert update["judge_verdict"] == "complete" and update["judge_source_verified"] is True
    assert update["judge_coverage"]["windows_total"] == update["judge_coverage"]["windows_checked"] == n
    assert update["judge_coverage"]["calls"] == 2 * n  # completeness and correctness, one per window
    assert update["judge_coverage"]["prompt_tokens"] == 100 * 2 * n
    assert fake.calls == {"completeness": n, "correctness": n}
    state = {"doc_type": "contract", "extraction_confidence": 0.3, **update}
    assert after_judge(state) == "compile_report"


def test_a_document_between_16000_chars_and_the_window_is_judged_whole(monkeypatch):
    ScriptedJudge(monkeypatch, window_chars=100_000)
    update = run_node("z" * 30_000)
    assert update["judge_source_verified"] is True and update["judge_coverage"]["windows_total"] == 1


def test_an_error_in_the_last_window_blocks_the_archive(monkeypatch):
    """Acceptance 2: every earlier window is clean, the last raises, and the document cannot archive."""
    text = long_text()
    n = len(windows_for_budget(text, WINDOW_CHARS))
    ScriptedJudge(monkeypatch, completeness=lambda i: RuntimeError("provider 500") if i == n else CLEAN_COMPLETE)
    update = run_node(text)
    assert update["judge_verdict"] == "judge_error" and "judge_source_verified" not in update
    state = {"doc_type": "contract", "extraction_confidence": 0.3, **update}
    assert after_judge(state) == "human_review"


def test_an_unparseable_last_window_blocks_the_archive(monkeypatch):
    text = long_text()
    n = len(windows_for_budget(text, WINDOW_CHARS))
    ScriptedJudge(monkeypatch, completeness=lambda i: {"_parse_error": True} if i == n else CLEAN_COMPLETE)
    update = run_node(text)
    assert update["judge_verdict"] == "judge_error"
    assert f"window {n} of {n}" in update["escalation_reason"]
    assert update["judge_coverage"]["windows_checked"] == n - 1
    assert after_judge({"doc_type": "contract", "extraction_confidence": 0.99, **update}) == "human_review"


def test_an_unparseable_correctness_window_blocks_the_archive(monkeypatch):
    ScriptedJudge(monkeypatch, correctness=lambda i: {"_parse_error": True} if i == 2 else CLEAN_ACCURATE)
    update = run_node(long_text())
    assert update["judge_verdict"] == "judge_error"
    assert after_judge({"doc_type": "contract", "extraction_confidence": 0.3, **update}) == "human_review"


def test_a_transient_error_in_a_window_retries_the_node_not_the_archive(monkeypatch):
    import httpx
    from openai import APIConnectionError

    from llm.retry import is_transient_error

    err = APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
    assert is_transient_error(err)  # guard: the fixture error really is transient
    ScriptedJudge(monkeypatch, completeness=lambda i: err if i == 2 else CLEAN_COMPLETE)
    update = run_node(long_text())
    assert update["transient_error"] is True and "judge_source_verified" not in update


def test_a_bad_window_makes_the_document_unverified_even_when_the_others_are_clean(monkeypatch):
    ScriptedJudge(
        monkeypatch,
        correctness=lambda i: CLEAN_ACCURATE if i != 2 else
        {"extraction_correctness": 0.4, "extraction_correctness_label": "inaccurate", "reasoning": "wrong date"},
    )
    update = run_node(long_text())
    assert update["judge_verdict"] == "incomplete" and update["judge_source_verified"] is False
    assert any("wrong date" in f for f in update["judge_findings"])


def test_coverage_reaches_the_manifest():
    from schemas.manifest import DocumentManifest

    cov = {"windows_total": 3, "windows_checked": 3, "calls": 6, "prompt_tokens": 600, "completion_tokens": 60}
    fields = bg._lane_b_manifest_fields({"judge_coverage": cov, "arbiter_coverage": cov})
    manifest = DocumentManifest(doc_id="d1", matter_id="M", original_filename="f.txt", **fields)
    assert manifest.judge_coverage == cov and manifest.arbiter_coverage == cov
    assert bg._lane_b_manifest_fields({})["judge_coverage"] is None


# -- the Lane B arbiter -----------------------------------------------------------------------


def arbiter_with(monkeypatch, replies, window_chars=WINDOW_CHARS):
    agent = arbiter_mod.ArbiterAgent.__new__(arbiter_mod.ArbiterAgent)
    seen: list[str] = []

    def call(message, schema):
        seen.append(message)
        reply = replies(len(seen)) if callable(replies) else replies[min(len(seen), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        limits.record_usage({"prompt_tokens": 50, "completion_tokens": 5}, "m", agent="arbiter")
        return dict(reply)

    monkeypatch.setattr(agent, "_call_structured", call, raising=False)
    monkeypatch.setattr(agent, "_configured_max_input_chars", lambda: window_chars, raising=False)
    limits.reset_run_usage()
    return agent, seen


ACCEPT = {"decision": "accept_with_caveats", "fields_to_fix": [], "reasoning": "fine", "handoff_summary": "ok"}


def arbitrate(agent, text):
    return agent.arbitrate("contract", {"parties": ["A"]}, "partial", ["gap"], 0.6, text)


def test_the_arbiter_reads_every_window_and_accepts_only_when_all_accept(monkeypatch):
    agent, seen = arbiter_with(monkeypatch, [ACCEPT])
    text = long_text()
    out = arbitrate(agent, text)
    n = len(windows_for_budget(text, WINDOW_CHARS))
    assert len(seen) == n > 2 and out["decision"] == "accept_with_caveats"
    assert all(f"Paragraph {i}." in "".join(seen) for i in range(24))
    assert out["coverage"] == {"windows_total": n, "windows_checked": n, "calls": n,
                               "prompt_tokens": 50 * n, "completion_tokens": 5 * n}


def test_any_retry_window_wins_over_accepts_and_unions_the_fix_lists(monkeypatch):
    def reply(i):
        if i == 2:
            return {"decision": "retry_extraction", "fields_to_fix": ["term", "parties"],
                    "reasoning": "r2", "handoff_summary": "h2"}
        if i == 4:
            return {"decision": "retry_extraction", "fields_to_fix": ["parties", "fees"],
                    "reasoning": "r4", "handoff_summary": "h4"}
        return ACCEPT

    agent, _ = arbiter_with(monkeypatch, reply)
    out = arbitrate(agent, long_text())
    assert out["decision"] == "retry_extraction" and out["fields_to_fix"] == ["term", "parties", "fees"]


def test_a_human_review_window_wins_and_stops_the_read(monkeypatch):
    human = {"decision": "human_review", "fields_to_fix": [], "reasoning": "r", "handoff_summary": "look"}
    agent, seen = arbiter_with(monkeypatch, lambda i: human if i == 2 else ACCEPT)
    out = arbitrate(agent, long_text())
    assert out["decision"] == "human_review" and len(seen) == 2
    assert out["coverage"]["windows_checked"] == 2 < out["coverage"]["windows_total"]


def test_an_off_schema_window_decision_becomes_human_review(monkeypatch):
    agent, _ = arbiter_with(monkeypatch, lambda i: {"decision": "archive"} if i == 2 else ACCEPT)
    assert arbitrate(agent, long_text())["decision"] == "human_review"


def test_an_error_in_the_last_window_propagates_so_the_node_escalates(monkeypatch, temp_base_dir):
    text = long_text()
    n = len(windows_for_budget(text, WINDOW_CHARS))
    agent, _ = arbiter_with(monkeypatch, lambda i: RuntimeError("down") if i == n else ACCEPT)
    with pytest.raises(RuntimeError):
        arbitrate(agent, text)
    node_agent, _ = arbiter_with(monkeypatch, lambda i: RuntimeError("down") if i == n else ACCEPT)
    monkeypatch.setattr(arbiter_mod, "ArbiterAgent", lambda: node_agent)
    update = bg.arbiter_node({"doc_id": "d1", "doc_type": "contract", "doc_text": text,
                              "judge_verdict": "partial", "extracted_data": {"parties": ["A"]}})
    assert update["arbiter_decision"] == "human_review"
    assert after_arbiter({"doc_type": "contract", "extraction_confidence": 0.99, **update}) == "human_review"


def test_a_short_document_is_one_arbiter_call_with_the_whole_source(monkeypatch):
    agent, seen = arbiter_with(monkeypatch, [ACCEPT], window_chars=100_000)
    out = arbitrate(agent, "s" * 20_000)
    assert len(seen) == 1 and "s" * 20_000 in seen[0] and "truncated" not in seen[0] and "WINDOW" not in seen[0]
    assert out["coverage"]["windows_total"] == 1


def test_arbiter_node_records_the_coverage(monkeypatch, temp_base_dir):
    agent, _ = arbiter_with(monkeypatch, [ACCEPT])
    monkeypatch.setattr(arbiter_mod, "ArbiterAgent", lambda: agent)
    update = bg.arbiter_node({"doc_id": "d1", "doc_type": "contract", "doc_text": long_text(),
                              "judge_verdict": "partial", "extracted_data": {"parties": ["A"]}})
    assert update["arbiter_coverage"]["windows_total"] == update["arbiter_coverage"]["windows_checked"] > 2
