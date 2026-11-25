"""#254: every extraction attempt is recorded; keeping the best one is opt-in.

``retry_extract_node`` overwrites ``extracted_data`` and nothing compared judge
verdicts across attempts, so the last attempt won even when the judge rated it
worse. Attempts are now always recorded in ``extraction_attempt_history`` (judge
verdict filled in by ``judge_verify_node``) and exposed in the result and the
manifest. Selecting the best-ranked attempt is the taxonomy knob
``keep_best_attempt`` (off by default, per-class overridable): ``complete`` >
``partial`` > ``incomplete``, then higher confidence, ties keep the latest, and
an attempt that failed the schema/quality gate is never chosen over one that
passed.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import graph.routing as routing
from graph import build_graph as bg
from graph.routing import HUMAN_APPLIED

FIELDS = {
    "document_name": "Service Agreement",
    "parties": ["Acme", "Beta"],
    "governing_law": "New York",
    "contract_value": "$1,250.00",
}
FIRST = {**deepcopy(FIELDS), "governing_law": "New Jersey"}
CLEAN = "Service Agreement\n\nAcme and Beta agreement.\n\nGoverning law: New York."


def attempts_module():
    # Looked up at call time so these tests fail by assertion, not at import, without the module.
    from graph import attempts

    return attempts


def _entry(n, verdict=None, conf=0.95, gate_ok=True, **extra):
    return {
        "attempt_no": n,
        "kind": "extract" if n == 1 else "retry_extract",
        "extracted_data": {"governing_law": f"law-{n}"},
        "extraction_confidence": conf,
        "guardrail": [] if gate_ok else ["extraction_schema_invalid"],
        "schema_valid": gate_ok,
        "gate_ok": gate_ok,
        "judge_verdict": verdict,
        "judge_score": None if verdict is None else {"complete": 1.0, "partial": 0.6, "incomplete": 0.2}[verdict],
        **extra,
    }


def _state(*entries, **overrides):
    latest = entries[-1]
    state = {
        "doc_id": "d-254",
        "doc_type": "contract",
        "extracted_data": deepcopy(latest["extracted_data"]),
        "extraction_confidence": latest["extraction_confidence"],
        "extraction_attempts": latest["attempt_no"],
        "extraction_attempt_history": [deepcopy(e) for e in entries],
        "judge_verdict": latest["judge_verdict"],
    }
    state.update(overrides)
    return state


def _select(state, enabled=True):
    return attempts_module().select_attempt(state, enabled=enabled)


# --- selection rule (pure) ---------------------------------------------------


def test_better_verdict_wins_over_a_later_worse_one():
    updates = _select(_state(_entry(1, "partial", 0.95), _entry(2, "incomplete", 0.99)))
    assert updates["selected_attempt"] == 1
    assert updates["extracted_data"] == {"governing_law": "law-1"}
    assert updates["extraction_confidence"] == 0.95
    assert updates["judge_verdict"] == "partial"
    assert updates["judge_score"] == 0.6
    assert updates["extraction_guardrail"] == []
    assert updates["selected_attempt_reason"]


@pytest.mark.parametrize("worse, better", [("incomplete", "partial"), ("partial", "complete"), ("incomplete", "complete")])
def test_rank_order_is_complete_then_partial_then_incomplete(worse, better):
    assert _select(_state(_entry(1, better), _entry(2, worse)))["selected_attempt"] == 1
    assert _select(_state(_entry(1, worse), _entry(2, better)))["selected_attempt"] == 2


def test_equal_rank_prefers_the_higher_confidence():
    assert _select(_state(_entry(1, "partial", 0.97), _entry(2, "partial", 0.92)))["selected_attempt"] == 1
    assert _select(_state(_entry(1, "partial", 0.92), _entry(2, "partial", 0.97)))["selected_attempt"] == 2


def test_a_tie_keeps_the_latest_attempt():
    updates = _select(_state(_entry(1, "partial", 0.95), _entry(2, "partial", 0.95), _entry(3, "partial", 0.95)))
    assert updates["selected_attempt"] == 3
    assert "extracted_data" not in updates  # latest already is the state


def test_a_gate_failed_attempt_is_never_selected_over_one_that_passed():
    failed_early = _entry(1, "complete", 0.99, gate_ok=False)
    assert _select(_state(failed_early, _entry(2, "incomplete", 0.5)))["selected_attempt"] == 2
    failed_late = _entry(2, "complete", 0.99, gate_ok=False)
    updates = _select(_state(_entry(1, "incomplete", 0.5), failed_late))
    assert updates["selected_attempt"] == 1
    assert updates["extracted_data"] == {"governing_law": "law-1"}


def test_only_judged_attempts_are_compared():
    # Attempt 1 judged, the retry was not: nothing says the retry is worse.
    updates = _select(_state(_entry(1, "partial", 0.99), _entry(2, None, 0.5)))
    assert updates["selected_attempt"] == 2
    # Middle attempt unjudged is skipped; the judged pair is compared.
    updates = _select(_state(_entry(1, "complete", 0.95), _entry(2, None, 0.99), _entry(3, "incomplete", 0.99)))
    assert updates["selected_attempt"] == 1


def test_no_judged_attempt_keeps_the_latest():
    assert _select(_state(_entry(1, None), _entry(2, None)))["selected_attempt"] == 2


def test_knob_off_records_the_latest_and_restores_nothing():
    updates = _select(_state(_entry(1, "complete"), _entry(2, "incomplete")), enabled=False)
    assert updates["selected_attempt"] == 2
    assert set(updates) == {"selected_attempt", "selected_attempt_reason"}


def test_a_matter_conflict_pins_the_latest_attempt_the_boss_ruled_on():
    state = _state(_entry(1, "complete"), _entry(2, "incomplete"), conflict_detected=True)
    updates = _select(state)
    assert updates["selected_attempt"] == 2
    assert "extracted_data" not in updates


def test_reviewer_fields_are_never_replaced():
    state = _state(_entry(1, "complete"), _entry(2, "incomplete"), review_decision=HUMAN_APPLIED, extraction_source="human")
    assert _select(state) == {}


def test_no_history_selects_nothing():
    assert _select({"extracted_data": {"a": 1}}) == {}
    assert _select({"extracted_data": {"a": 1}, "extraction_attempt_history": []}) == {}


def test_selection_is_idempotent():
    state = _state(_entry(1, "partial", 0.95), _entry(2, "incomplete", 0.99))
    once = _select(state)
    again = _select({**state, **once})
    assert again["selected_attempt"] == once["selected_attempt"] == 1
    assert again["extracted_data"] == once["extracted_data"]


def test_the_restored_extraction_is_a_copy():
    state = _state(_entry(1, "partial"), _entry(2, "incomplete"))
    updates = _select(state)
    updates["extracted_data"]["governing_law"] = "mutated"
    assert state["extraction_attempt_history"][0]["extracted_data"] == {"governing_law": "law-1"}


# --- the knob ---------------------------------------------------------------


def _patch_thresholds(monkeypatch, by_doc_type, default):
    monkeypatch.setattr(
        routing,
        "get_confidence_thresholds",
        lambda doc_type=None: dict(by_doc_type.get(doc_type, default)),
    )


def test_knob_reader_and_per_class_override(monkeypatch):
    _patch_thresholds(
        monkeypatch,
        {"contract": {"keep_best_attempt": True}},
        {"keep_best_attempt": False},
    )
    assert routing.keep_best_attempt_enabled({"doc_type": "contract"}) is True
    assert routing.keep_best_attempt_enabled({"doc_type": "correspondence"}) is False


def test_absent_knob_is_off(monkeypatch):
    _patch_thresholds(monkeypatch, {}, {"low": 0.88})
    assert routing.keep_best_attempt_enabled({"doc_type": "contract"}) is False


def test_shipped_taxonomy_ships_the_knob_off():
    """Until the judge is calibrated (#242), ranking by its verdict stays opt-in."""
    from pipeline.config import get_confidence_thresholds

    assert get_confidence_thresholds("contract").get("keep_best_attempt") is False
    assert get_confidence_thresholds().get("keep_best_attempt") is False


# --- recording --------------------------------------------------------------


def _base_state(**overrides):
    state = {
        "doc_id": "d-rec",
        "matter_id": "DEFAULT",
        "original_filename": "x.txt",
        "doc_text": "Service agreement body.",
        "doc_type": "contract",
        "stage": "classified",
    }
    state.update(overrides)
    return state


def test_extract_and_retry_record_every_attempt(monkeypatch):
    monkeypatch.setattr(bg, "_extract_contracts", lambda *a, **k: {**deepcopy(FIRST), "confidence": 0.95})
    first = bg.extract_node(_base_state())
    history = first["extraction_attempt_history"]
    assert [h["attempt_no"] for h in history] == [1]
    assert history[0]["kind"] == "extract"
    assert history[0]["extracted_data"]["governing_law"] == "New Jersey"
    assert history[0]["extraction_confidence"] == 0.95
    assert history[0]["guardrail"] == []
    assert history[0]["gate_ok"] is True
    assert history[0]["judge_verdict"] is None

    monkeypatch.setattr(bg, "_extract_contracts", lambda *a, **k: {**deepcopy(FIELDS), "confidence": 0.99})
    second = bg.retry_extract_node(_base_state(**first))
    history = second["extraction_attempt_history"]
    assert [h["attempt_no"] for h in history] == [1, 2]
    assert history[1]["kind"] == "retry_extract"
    assert history[1]["extracted_data"]["governing_law"] == "New York"
    assert history[0]["extracted_data"]["governing_law"] == "New Jersey"  # earlier attempt untouched


def test_a_gate_failed_attempt_is_recorded_as_such(monkeypatch):
    monkeypatch.setattr(bg, "_extract_contracts", lambda *a, **k: {"parties": "invalid list shape", "confidence": 0.99})
    entry = bg.extract_node(_base_state())["extraction_attempt_history"][0]
    assert entry["gate_ok"] is False
    assert entry["schema_valid"] is False
    assert entry["guardrail"]


def test_a_transient_failure_records_no_attempt(monkeypatch):
    import openai

    def boom(*a, **k):
        raise openai.APIConnectionError(request=None)

    monkeypatch.setattr(bg, "_extract_contracts", boom)
    assert "extraction_attempt_history" not in bg.extract_node(_base_state())
    assert "extraction_attempt_history" not in bg.retry_extract_node(_base_state())


def test_a_large_raw_reply_is_bounded_in_the_history(monkeypatch):
    monkeypatch.setattr(
        bg, "_extract_contracts", lambda *a, **k: {"_parse_error": True, "_raw": "x" * 500_000, "confidence": 0.0}
    )
    entry = bg.extract_node(_base_state())["extraction_attempt_history"][0]
    assert len(entry["extracted_data"]["_raw"]) < 5_000
    assert entry["extracted_data"]["_parse_error"] is True


def test_judge_verify_fills_the_latest_attempt(monkeypatch, mocker):
    mocker.patch("agents.judge.CompletenessJudge.__init__", return_value=None)
    mocker.patch(
        "agents.judge.CompletenessJudge.judge_completeness",
        return_value={"completeness_label": "partial", "completeness": 0.6, "reasoning": "law differs"},
    )
    history = [_entry(1), _entry(2)]
    state = _base_state(
        extracted_data={"governing_law": "law-2"},
        extraction_confidence=0.95,
        extraction_attempts=2,
        extraction_attempt_history=history,
    )
    updates = bg.judge_verify_node(state)
    filled = updates["extraction_attempt_history"]
    assert filled[0]["judge_verdict"] is None
    assert filled[1]["judge_verdict"] == "partial"
    assert filled[1]["judge_score"] == 0.6
    assert filled[1]["judge_findings"] == ["law differs"]
    assert history[1]["judge_verdict"] is None  # the input state is not mutated


def test_a_skipped_judge_records_no_verdict():
    state = _base_state(
        extracted_data={"governing_law": "law-1"},
        extraction_confidence=0.99,
        extraction_attempts=1,
        extraction_attempt_history=[_entry(1, conf=0.99)],
    )
    updates = bg.judge_verify_node(state)
    assert updates["judge_verdict"] == "skipped"
    assert "extraction_attempt_history" not in updates


# --- compile_report / review handoff ----------------------------------------


def test_compile_report_archives_the_selected_attempt(monkeypatch):
    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {"keep_best_attempt": True})
    state = _base_state(**_state(_entry(1, "partial", 0.95), _entry(2, "incomplete", 0.99)))
    update = bg.compile_report_node(state)
    assert update["selected_attempt"] == 1
    assert update["extracted_data"]["governing_law"] == "law-1"
    assert update["extraction_confidence"] == 0.95
    assert update["judge_verdict"] == "partial"
    assert update["extracted_data"]["_report"]["extracted_data"]["governing_law"] == "law-1"


def test_compile_report_keeps_the_latest_when_the_knob_is_off(monkeypatch):
    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {})
    state = _base_state(**_state(_entry(1, "partial", 0.95), _entry(2, "incomplete", 0.99)))
    update = bg.compile_report_node(state)
    assert update["selected_attempt"] == 2
    assert update["extracted_data"]["governing_law"] == "law-2"
    assert "extraction_confidence" not in update


def test_report_does_not_restore_an_earlier_attempt_that_still_requires_review(monkeypatch):
    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {"keep_best_attempt": True, "low": 0.7})
    state = _base_state(**_state(_entry(1, "partial", 0.1), _entry(2, "incomplete", 0.95)))
    update = bg.compile_report_node(state)
    assert update["selected_attempt"] == 2
    assert update["extracted_data"]["governing_law"] == "law-2"


def test_report_selection_preserves_the_oracle_coverage_gate(monkeypatch):
    from pipeline.config import oracle_routing_override

    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {"keep_best_attempt": True})
    first, latest = _entry(1, "partial"), _entry(2, "incomplete")
    latest["extracted_data"] = deepcopy(FIELDS)
    state = _base_state(**_state(first, latest), ground_truth={"expected_fields": deepcopy(FIELDS)})
    with oracle_routing_override(True):
        update = bg.compile_report_node(state)
    assert update["selected_attempt"] == 2
    assert update["extracted_data"]["parties"] == FIELDS["parties"]


@pytest.mark.parametrize("resolution", ["approve_as_is", "correct"])
def test_live_human_apply_retains_the_selected_attempt_evidence(monkeypatch, temp_base_dir, resolution):
    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {"keep_best_attempt": True})
    monkeypatch.setattr(bg, "_restart_review_clock", lambda doc_id: None)
    state = _base_state(**_state(_entry(1, "complete", 0.8), _entry(2, "partial", 0.95)))
    fields = {"governing_law": "New York"}
    monkeypatch.setattr(bg, "interrupt", lambda payload: {
        "decision": "approved", "resolution": resolution, "extracted_data": fields,
    })
    update = bg.human_review_node(state)
    assert update["extracted_data"] == fields
    assert update["selected_attempt"] == 1
    assert update["extraction_confidence"] == 0.8
    assert update["judge_verdict"] == "complete"


def test_the_review_handoff_gets_the_selected_attempt(monkeypatch):
    monkeypatch.setattr(routing, "get_confidence_thresholds", lambda doc_type=None: {"keep_best_attempt": True})
    state = _base_state(**_state(_entry(1, "partial", 0.95), _entry(2, "incomplete", 0.99)))
    selected = bg._with_selected_attempt(state)
    assert selected["selected_attempt"] == 1
    assert selected["extracted_data"]["governing_law"] == "law-1"
    assert state["extracted_data"]["governing_law"] == "law-2"  # the caller's state is not mutated


# --- through the compiled graph ---------------------------------------------


@pytest.fixture
def run_graph(temp_base_dir, monkeypatch, mocker):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    mocker.patch(
        "agents.sorter.SorterAgent.classify_json",
        return_value={"doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "x"},
    )
    extractor = mocker.patch.object(
        bg,
        "_extract_contracts",
        side_effect=[{**deepcopy(FIRST), "confidence": 0.95}, {**deepcopy(FIELDS), "confidence": 0.99}],
    )
    mocker.patch("agents.judge.CompletenessJudge.__init__", return_value=None)
    mocker.patch("agents.arbiter.ArbiterAgent.__init__", return_value=None)
    mocker.patch(
        "agents.arbiter.ArbiterAgent.arbitrate",
        side_effect=[
            {"decision": "retry_extraction", "reasoning": "fix", "handoff_summary": "reread the law", "fields_to_fix": ["governing_law"]},
            {"decision": "accept_with_caveats", "reasoning": "good enough", "handoff_summary": ""},
        ],
    )

    def run(*, keep_best: bool, second_label: str, second_score: float = 0.2, second_conf: float | None = None):
        real = routing.get_confidence_thresholds
        mocker.patch.object(
            routing,
            "get_confidence_thresholds",
            lambda doc_type=None: {**real(doc_type), "judge_rejudge_retry": True, "keep_best_attempt": keep_best},
        )
        if second_conf is not None:
            extractor.side_effect = [
                {**deepcopy(FIRST), "confidence": 0.95},
                {**deepcopy(FIELDS), "confidence": second_conf},
            ]
        mocker.patch(
            "agents.judge.CompletenessJudge.judge_completeness",
            side_effect=[
                {"completeness_label": "partial", "completeness": 0.6, "reasoning": "governing_law contradicts source"},
                {"completeness_label": second_label, "completeness": second_score, "reasoning": "second look"},
            ],
        )
        mocker.patch(
            "agents.judge.CompletenessJudge.judge_extraction_correctness",
            return_value={"extraction_correctness_label": "accurate", "extraction_correctness": 1.0, "reasoning": ""},
        )
        source = temp_base_dir / "pipeline" / "inbox" / "agreement.txt"
        source.write_text("# **Service Agreement**\n\nAcme and Beta agreement.\n\nGoverning law: New York.\n")
        graph = bg.build_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "keep-best-test"}}
        order = []
        for event in graph.stream(
            {"file_path": str(source), "matter_id": "KEEP-BEST", "messages": []}, config, stream_mode="updates"
        ):
            order.extend(event)
        return order, graph.get_state(config).values

    run.base = temp_base_dir
    return run


def _manifest(run_graph, final):
    return json.loads((run_graph.base / "manifests" / f"{final['doc_id']}.json").read_text())


FULL_PATH = ["intake", "classify", "extract", "judge_verify", "arbiter", "retry_extract", "judge_verify", "arbiter", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]


def test_knob_on_archives_the_better_judged_first_attempt(run_graph, approvals_archive):
    order, final = run_graph(keep_best=True, second_label="incomplete")
    assert order == FULL_PATH
    assert final["selected_attempt"] == 1
    assert final["extracted_data"]["governing_law"] == "New Jersey"
    assert final["extracted_data"]["_report"]["extracted_data"]["governing_law"] == "New Jersey"
    assert final["extraction_confidence"] == 0.95
    assert final["judge_verdict"] == "partial"
    assert [h["judge_verdict"] for h in final["extraction_attempt_history"]] == ["partial", "incomplete"]
    manifest = _manifest(run_graph, final)
    assert manifest["stage"] == "archived"
    assert manifest["extracted_data"]["governing_law"] == "New Jersey"
    assert manifest["selected_attempt"] == 1
    assert manifest["selected_attempt_reason"]
    assert [h["attempt_no"] for h in manifest["extraction_attempt_history"]] == [1, 2]
    assert manifest["judge_verdict"] == "partial"


def test_knob_off_keeps_the_last_attempt_and_still_records_the_history(run_graph, approvals_archive):
    order, final = run_graph(keep_best=False, second_label="incomplete")
    assert order == FULL_PATH
    assert final["selected_attempt"] == 2
    assert final["extracted_data"]["governing_law"] == "New York"
    assert final["judge_verdict"] == "incomplete"
    assert [h["judge_verdict"] for h in final["extraction_attempt_history"]] == ["partial", "incomplete"]
    assert [h["extracted_data"]["governing_law"] for h in final["extraction_attempt_history"]] == ["New Jersey", "New York"]
    manifest = _manifest(run_graph, final)
    assert manifest["extracted_data"]["governing_law"] == "New York"
    assert manifest["selected_attempt"] == 2
    assert [h["attempt_no"] for h in manifest["extraction_attempt_history"]] == [1, 2]


def test_knob_on_with_a_tie_keeps_the_latest_attempt(run_graph, approvals_archive):
    _, final = run_graph(keep_best=True, second_label="partial", second_score=0.6, second_conf=0.95)
    assert final["selected_attempt"] == 2
    assert final["extracted_data"]["governing_law"] == "New York"


def test_knob_on_keeps_a_better_second_attempt(run_graph, approvals_archive):
    _, final = run_graph(keep_best=True, second_label="complete", second_score=1.0)
    assert final["selected_attempt"] == 2
    assert final["extracted_data"]["governing_law"] == "New York"


# --- the history survives the restart fallback paths -------------------------


def _parked_with_history(temp_base_dir):
    """A manifest parked in the review bin that carries an attempt history, as after a restart."""
    from pipeline.bins import review_dir, save_manifest
    from schemas.manifest import DocumentManifest, PipelineStage

    history = [
        {"attempt_no": 1, "kind": "extract", "extracted_data": deepcopy(FIRST), "extraction_confidence": 0.95,
         "guardrail": [], "schema_valid": True, "gate_ok": True, "judge_verdict": "partial",
         "judge_score": 0.6, "judge_findings": ["governing_law contradicts source"], "judge_source_verified": True},
        {"attempt_no": 2, "kind": "retry", "extracted_data": deepcopy(FIELDS), "extraction_confidence": 0.7,
         "guardrail": [], "schema_valid": True, "gate_ok": True, "judge_verdict": "incomplete",
         "judge_score": 0.2, "judge_findings": ["still thin"], "judge_source_verified": True},
    ]
    manifest = DocumentManifest(
        doc_id="parked-hist-1",
        matter_id="KEEP-BEST",
        original_filename="parked.txt",
        stage=PipelineStage.REVIEW,
        doc_type="contract",
        classification_confidence=0.99,
        extracted_data=deepcopy(FIELDS),
        extraction_confidence=0.7,
        escalation_reason="judge incomplete",
        extraction_attempts=2,
        extraction_attempt_history=history,
        selected_attempt=2,
        selected_attempt_reason="keep_best_attempt_off",
    )
    review_file = review_dir() / "parked.txt"
    review_file.write_text(CLEAN)
    save_manifest(manifest)
    return manifest, review_file, history


@pytest.mark.parametrize("resolution", ["approve_as_is", "correct"])
def test_attempt_history_survives_a_stateless_human_applied_resume(temp_base_dir, monkeypatch, resolution):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    manifest, review_file, history = _parked_with_history(temp_base_dir)
    bg.reset_compiled_graph()  # the MemorySaver is lost, as after a restart

    fields = {**deepcopy(FIELDS), "governing_law": "Delaware"} if resolution == "correct" else deepcopy(FIELDS)
    out = bg.resume_from_review(
        manifest,
        review_file,
        resolution={"decision": "approved", "resolution": resolution, "extracted_data": fields},
    )
    assert out["stage"] == "archived"
    saved = json.loads((temp_base_dir / "manifests" / "parked-hist-1.json").read_text())
    assert saved["stage"] == "archived" and saved["extraction_source"] == "human"
    assert saved["extraction_attempt_history"] == history
    assert saved["selected_attempt"] == 2
    assert saved["selected_attempt_reason"] == "keep_best_attempt_off"
    # The reviewer's fields, not an attempt's, are what was archived.
    assert saved["extracted_data"]["governing_law"] == fields["governing_law"]


def test_a_stateless_re_extraction_still_starts_a_new_history(temp_base_dir, mocker):
    """Plain approval / reclassify re-extracts, so it must not inherit the pre-review attempts
    (selection could otherwise restore a pre-review attempt into the corrected run)."""
    manifest, review_file, _ = _parked_with_history(temp_base_dir)
    bg.reset_compiled_graph()
    run = mocker.patch.object(bg, "_execute_run", return_value={})
    bg.resume_from_review(manifest, review_file, resolution={"decision": "approved", "resolution": "reclassify"})
    state = run.call_args.args[0]
    assert not state.get("extraction_attempt_history")
    assert not state.get("selected_attempt")


def test_an_aborted_run_keeps_the_attempt_history_it_carries(temp_base_dir, mocker):
    from pipeline.bins import load_manifest

    mocker.patch.object(bg, "_write_catalog_record")
    mocker.patch.object(bg, "_maybe_export_warehouse")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.relations.dispatch_relations_scan")
    history = [_entry(1, "partial"), _entry(2, "incomplete")]
    bg._finalize_aborted(
        {"doc_id": "abort-hist-1", "matter_id": "M", "original_filename": "a.txt",
         "extraction_attempt_history": history, "selected_attempt": 2,
         "selected_attempt_reason": "keep_best_attempt_off"},
        "boom",
    )
    m = load_manifest("abort-hist-1")
    assert m.stage.value == "failed"
    assert [e["attempt_no"] for e in m.extraction_attempt_history] == [1, 2]
    assert m.selected_attempt == 2 and m.selected_attempt_reason == "keep_best_attempt_off"


def test_an_aborted_live_resume_keeps_the_parked_history(temp_base_dir, mocker):
    """The live-resume abort state is minimal; the history is on the parked manifest it replaces."""
    from pipeline.bins import load_manifest

    manifest, review_file, history = _parked_with_history(temp_base_dir)
    mocker.patch.object(bg, "_write_catalog_record")
    mocker.patch.object(bg, "_maybe_export_warehouse")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.relations.dispatch_relations_scan")
    bg._finalize_aborted(
        {"doc_id": manifest.doc_id, "matter_id": "KEEP-BEST", "original_filename": "parked.txt",
         "file_path": str(review_file)},
        "boom",
    )
    m = load_manifest(manifest.doc_id)
    assert m.stage.value == "failed"
    assert m.extraction_attempt_history == history
    assert m.selected_attempt == 2
