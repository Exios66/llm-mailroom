"""#253: the retry-extraction handoff is a bounded summary, never the previous values.

``retry_extract_node`` used to prime the specialist with
``Previous attempt found: {prev_extracted}`` -- the raw dict, ``_`` metadata and
``reasoning`` included, unbounded -- and ``extract_chunked`` prefixes that
handoff onto EVERY window, so each window of a long contract was told every
claim of the previous attempt and the list union kept them all. The handoff now
carries findings and field names only.
"""

from __future__ import annotations

from graph import build_graph as bg

SENTINEL = "ZZ-PREVIOUS-VALUE-ZZ"


def _huge_previous() -> dict:
    return {
        "parties": [f"{SENTINEL}-party-{i}" for i in range(4000)],
        "governing_law": f"{SENTINEL}-law",
        "reasoning": f"{SENTINEL}-reasoning " * 2000,
        "_report": {"summary": f"{SENTINEL}-report"},
        "_raw": f"{SENTINEL}-raw " * 2000,
        "_failed_chunks": 2,
        "effective_date": None,
        "term": "",
        "cuad_clauses": [],
    }


def _state(**overrides) -> dict:
    state = {
        "doc_id": "d-253",
        "matter_id": "DEFAULT",
        "doc_type": "contract",
        "doc_text": "Body of the agreement.\n\n" * 40,
        "extraction_attempts": 1,
        "extraction_confidence": 0.6,
        "extracted_data": _huge_previous(),
        "extraction_guardrail": ["extraction_schema_invalid"],
    }
    state.update(overrides)
    return state


def _capture_handoff(monkeypatch, state: dict) -> str:
    seen: list[str | None] = []

    def fake(doc_text, pages=None, handoff_context=None):
        seen.append(handoff_context)
        return {"parties": ["Acme"], "governing_law": "NY", "confidence": 0.93}

    monkeypatch.setattr(bg, "_extract_contracts", fake)
    bg.retry_extract_node(state)
    assert len(seen) == 1 and seen[0]
    return seen[0]


def test_retry_handoff_carries_no_previous_values(monkeypatch):
    handoff = _capture_handoff(monkeypatch, _state())
    assert SENTINEL not in handoff
    assert "Previous attempt found" not in handoff


def test_retry_handoff_has_no_metadata_keys_or_reasoning(monkeypatch):
    handoff = _capture_handoff(monkeypatch, _state())
    for leaked in ("_report", "_raw", "reasoning", "_failed_chunks"):
        assert leaked not in handoff


def test_retry_handoff_is_bounded_even_with_a_huge_previous_extraction(monkeypatch):
    small = _capture_handoff(monkeypatch, _state(extracted_data={"governing_law": "NY"}))
    huge = _capture_handoff(monkeypatch, _state())
    assert len(huge) < len(str(_huge_previous())) // 20
    # The huge previous extraction adds at most the capped summary, not its size.
    assert len(huge) - len(small) <= bg.RETRY_HANDOFF_MAX_CHARS


def test_retry_summary_is_capped_with_an_explicit_marker(monkeypatch):
    findings = [f"finding {i}: " + "x" * 400 for i in range(50)]
    summary = bg._retry_handoff_summary(_state(judge_findings=findings))
    assert len(summary) <= bg.RETRY_HANDOFF_MAX_CHARS
    assert summary.endswith(bg.RETRY_HANDOFF_TRUNCATION_MARKER)


def test_retry_summary_names_empty_fields_and_guardrail_issues(monkeypatch):
    summary = bg._retry_handoff_summary(_state(judge_findings=["governing_law contradicts source"]))
    assert "effective_date" in summary and "term" in summary and "cuad_clauses" in summary
    assert "parties" not in summary  # populated: its name is not a finding
    assert "extraction_schema_invalid" in summary
    assert "governing_law contradicts source" in summary


def test_retry_summary_with_nothing_to_report_is_still_a_valid_short_prefix():
    summary = bg._retry_handoff_summary(_state(extracted_data=None, extraction_guardrail=[]))
    assert summary
    assert len(summary) <= bg.RETRY_HANDOFF_MAX_CHARS


def test_arbiter_retry_keeps_its_fix_list_in_the_base_handoff(monkeypatch):
    state = _state(
        arbiter_retry_count=1,
        arbiter_fields_to_fix=["governing_law"],
        arbiter_handoff="Read the governing law clause again",
        judge_findings=["governing_law contradicts source"],
    )
    handoff = _capture_handoff(monkeypatch, state)
    assert "Read the governing law clause again" in handoff
    assert "governing_law" in handoff
    assert SENTINEL not in handoff


def test_every_chunk_window_gets_the_same_bounded_handoff(monkeypatch):
    """No window is primed with another window's (or the previous attempt's) claims."""
    from agents.contracts_specialist import ContractsSpecialist

    windows: list[str] = []

    class Agent(ContractsSpecialist):
        def __init__(self, handoff_context=None):  # no client, no LLM
            self.handoff_context = handoff_context
            self._last_usage = None

        def _call_structured(self, user_message, json_schema=None, **kwargs):
            windows.append(user_message)
            return {
                "parties": [f"party-from-window-{len(windows)}"],
                "governing_law": "NY",
                "confidence": 0.9,
            }

    monkeypatch.setattr(
        bg, "_chunk_config", lambda: {"enabled": True, "chunk_chars": 400, "overlap_chars": 40}
    )
    monkeypatch.setattr(bg, "_specialist_input_budget", lambda agent: 4_000)

    def chunked(doc_text, pages=None, handoff_context=None):
        return bg._run_chunked_extraction(Agent, doc_text, pages, handoff_context)

    monkeypatch.setattr(bg, "_extract_contracts", chunked)
    bg.retry_extract_node(_state(doc_text="Clause paragraph about the agreement.\n\n" * 80))

    assert len(windows) > 2
    prefixes = {w.split("EXTRACTION CHUNK")[0] for w in windows}
    assert len(prefixes) == 1, "every window must carry the identical handoff"
    for window in windows:
        assert SENTINEL not in window
        assert "party-from-window" not in window
    only = next(iter(prefixes))
    assert len(only) < len(str(_huge_previous())) // 20


def test_conflict_detection_still_reads_the_previous_extraction_from_state(monkeypatch):
    """The prompt no longer shows the previous values; ``_detect_conflict`` never read the prompt."""
    seen: list[tuple[dict, dict]] = []

    def spy(state, extracted):
        seen.append((state, extracted))
        return False, []

    monkeypatch.setattr(bg, "_detect_conflict", spy)
    monkeypatch.setattr(
        bg,
        "_extract_contracts",
        lambda *a, **k: {"parties": ["Acme"], "governing_law": "NY", "confidence": 0.93},
    )
    state = _state()
    bg.retry_extract_node(state)
    assert len(seen) == 1
    assert seen[0][0] is state or seen[0][0]["extracted_data"] is state["extracted_data"]
    assert seen[0][1]["parties"] == ["Acme"]


# --- the findings must describe the extraction being retried -----------------

OLD_FINDING = "OLD finding about the pre-review extraction"


def _attempt(n: int, verdict: str | None, findings: list[str] | None = None) -> dict:
    return {
        "attempt_no": n,
        "kind": "extract",
        "extracted_data": {},
        "extraction_confidence": 0.6,
        "guardrail": [],
        "schema_valid": True,
        "gate_ok": True,
        "judge_verdict": verdict,
        "judge_score": 0.5 if verdict else None,
        "judge_findings": findings,
        "judge_source_verified": None,
    }


def test_judge_findings_of_an_unjudged_attempt_are_not_handed_to_the_retry():
    """The latest attempt has no judge verdict, so ``judge_findings`` are some earlier attempt's."""
    state = _state(
        arbiter_retry_count=0,
        judge_findings=[OLD_FINDING],
        extraction_attempt_history=[_attempt(1, None)],
    )
    assert OLD_FINDING not in bg._retry_handoff_summary(state)


def test_judge_findings_of_the_judged_latest_attempt_are_still_handed_over():
    state = _state(
        arbiter_retry_count=0,
        judge_findings=["governing_law contradicts source"],
        extraction_attempt_history=[_attempt(1, "partial", ["governing_law contradicts source"])],
    )
    assert "governing_law contradicts source" in bg._retry_handoff_summary(state)


def test_findings_survive_a_human_approval_for_audit_but_not_for_the_re_extraction_retry(monkeypatch):
    """After a human re-extract the old judge_* fields stay on the state (archive trail), but the
    retry of the NEW extraction must not be told the pre-review findings."""
    from unittest.mock import patch

    state = _state(
        arbiter_retry_count=1,
        judge_verdict="partial",
        judge_findings=[OLD_FINDING],
        extraction_attempt_history=[_attempt(1, "partial", [OLD_FINDING])],
        review_causes=["low_confidence"],
    )
    with patch("graph.build_graph.interrupt", return_value={"decision": "approved", "resolution": "reclassify"}):
        state.update(bg.human_review_node(state))
    # Audit fields the human path deliberately keeps.
    assert state["judge_findings"] == [OLD_FINDING] and state["judge_verdict"] == "partial"
    assert state["arbiter_retry_count"] == 0 and state["extraction_attempt_history"] == []

    monkeypatch.setattr(
        bg, "_extract_contracts", lambda *a, **k: {"parties": [], "governing_law": None, "confidence": 0.3}
    )
    state.update(bg.extract_node(state))
    assert [e["attempt_no"] for e in state["extraction_attempt_history"]] == [1]

    assert OLD_FINDING not in bg._retry_handoff_summary(state)
