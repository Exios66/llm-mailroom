"""HITL review hardening: regressions for the PR-203 review findings.

Each test names the finding it pins (T-numbers are bot threads, CR-numbers are
local code-review findings). The API-level tests reuse the fixtures and park
helpers of ``test_hitl_resolution`` (mocked LLM, no network).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from pathlib import Path

import pytest

from tests.test_hitl_resolution import (  # noqa: F401  (fixtures + helpers)
    H,
    _events,
    _extraction_park,
    _forbid_llm,
    _resolve,
    _sorter_park,
    api,
    llm,
)


class _Stop(Exception):
    """Raised by a patched ``interrupt`` so a node test stops where it parks."""


# --------------------------------------------------------------------------
# CR1: the resolve lock outlives a cancelled request while the graph runs
# --------------------------------------------------------------------------


class _JsonRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_cancelled_resolve_keeps_the_lock_until_the_graph_thread_ends(api, temp_base_dir, llm, mocker):
    import api.main as api_main
    from pipeline.review_resolve import ResolveInProgress, doc_resolve_lock

    doc_id = _extraction_park(temp_base_dir, "cancel.txt")
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_resume(manifest, review_file, notes="", *, resolution=None):
        calls.append(1)
        started.set()
        release.wait(10)
        return {"stage": "review", "doc_type": manifest.doc_type}

    mocker.patch("graph.build_graph.resume_from_review", side_effect=slow_resume)

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            api_main.resolve_review(doc_id, _JsonRequest({"resolution": "approve_as_is"}))
        )
        assert await loop.run_in_executor(None, started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.05)
        # The request task is gone/cancelling but the graph thread still runs:
        # a second resolve must be refused, not run the document again.
        with pytest.raises(ResolveInProgress):
            with doc_resolve_lock(doc_id):
                pass
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        with doc_resolve_lock(doc_id):  # free again once the thread finished
            pass

    asyncio.run(scenario())
    assert calls == [1]


# --------------------------------------------------------------------------
# CR2 / CR3: archive_path and the audit event follow this run's outcome
# --------------------------------------------------------------------------


def _seed_old_archive_entry(doc_id):
    import api.main as api_main

    asyncio.run(
        api_main._write_review_audit_entry(
            doc_id, "M-HITL", "archived", "", detail={"archive_path": "/old/run/file.txt"}
        )
    )


def test_tail_that_did_not_archive_reports_no_archive_and_no_review_completed(
    api, temp_base_dir, llm, mocker
):
    doc_id = _extraction_park(temp_base_dir, "reparked.txt")
    _seed_old_archive_entry(doc_id)
    mocker.patch(
        "graph.build_graph.resume_from_review",
        return_value={"stage": "review", "doc_type": "contract", "extracted_data": {"parties": ["A"]}},
    )
    r = _resolve(api, doc_id, resolution="approve_as_is")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disposition"] == "complete"  # legacy key kept
    assert body["complete"]["stage"] == "review"
    assert "archive_path" not in body["complete"], body  # the OLD run's path must not leak
    assert "did not reach the archive" in body["warning"]
    events = _events(doc_id)
    assert "review_completed" not in events  # nothing was completed
    assert events[-1] == "review_approved"


def test_archive_path_comes_from_this_run_not_an_earlier_archive(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "oldpath.txt")
    _seed_old_archive_entry(doc_id)
    mocker.patch(
        "graph.build_graph.resume_from_review",
        return_value={"stage": "archived", "doc_type": "contract", "extracted_data": {"parties": ["A"]}},
    )
    r = _resolve(api, doc_id, resolution="approve_as_is")
    assert r.status_code == 200, r.text
    assert "archive_path" not in r.json()["complete"]  # this run wrote no archived entry
    assert "review_completed" in _events(doc_id)


# --------------------------------------------------------------------------
# CR10: a resolution does not silently drop posted extracted_data
# --------------------------------------------------------------------------


def test_resolution_with_complete_and_extracted_data_is_refused(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "lossy.txt")
    _forbid_llm(mocker, llm)
    r = _resolve(
        api, doc_id, resolution="approve_as_is", disposition="complete",
        extracted_data={"parties": ["Edited Co"], "effective_date": "2024-03-03"},
    )
    assert r.status_code == 400, r.text
    assert "field_corrections" in r.text
    from pipeline.bins import load_manifest

    assert load_manifest(doc_id).stage.value == "review"  # nothing ran


def test_resolution_with_a_disposition_it_cannot_use_names_both_valid_ones(api, temp_base_dir, llm):
    doc_id = _sorter_park(temp_base_dir, "disp.txt")
    r = _resolve(api, doc_id, resolution="approve_as_is", disposition="record")
    assert r.status_code == 400
    assert "resume" in r.text and "complete" in r.text


# --------------------------------------------------------------------------
# CR9: a non-dict extracted_data never crashes the queue
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [["a", "b"], "text", 7])
def test_queue_priority_survives_non_dict_extracted_data(bad):
    from pipeline.review_priority import priority, sort_queue

    now = dt.datetime.now(dt.timezone.utc)
    item = {"doc_id": "d", "doc_type": "contract", "extracted_data": bad}
    assert priority(item, now)["reasons"] == ["no_cause_recorded"]
    assert sort_queue([item], now)[0]["priority"]["tier"] in {"high", "normal", "low"}


# --------------------------------------------------------------------------
# CR5 / T68: queue age is measured from when the document entered review
# --------------------------------------------------------------------------


def test_age_uses_review_entry_time_not_updated_at():
    from pipeline.review_priority import age_hours

    now = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
    item = {
        "review_entered_at": "2026-01-08T00:00:00+00:00",  # 48h ago
        "updated_at": "2026-01-09T23:00:00+00:00",         # touched an hour ago
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    assert age_hours(item, now) == pytest.approx(48.0)
    # Fallback is unchanged when no entry time was recorded.
    assert age_hours({"updated_at": item["updated_at"]}, now) == pytest.approx(1.0)
    assert age_hours({"review_entered_at": "garbage", "created_at": item["created_at"]}, now) == pytest.approx(216.0)


def test_record_note_does_not_restart_the_review_clock(api, temp_base_dir, llm):
    from pipeline.bins import load_manifest

    doc_id = _sorter_park(temp_base_dir, "clock.txt")
    entered = load_manifest(doc_id).review_entered_at
    assert entered is not None
    r = _resolve(api, doc_id, decision="approved", disposition="record", notes="looked at it")
    assert r.status_code == 200, r.text
    after = load_manifest(doc_id)
    assert after.review_entered_at == entered
    assert after.updated_at > entered  # the note touched the manifest...
    queue = api.get("/review/queue", headers=H).json()
    item = next(d for d in queue["documents"] if d["doc_id"] == doc_id)
    assert item["review_entered_at"] == entered.isoformat()  # ...but the entry time is exposed


def test_replayed_human_review_node_keeps_the_original_entry_time(temp_base_dir, mocker):
    from graph.build_graph import human_review_node
    from pipeline.bins import save_manifest
    from schemas.manifest import DocumentManifest, PipelineStage

    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    save_manifest(DocumentManifest(
        doc_id="replay-1", matter_id="M", original_filename="r.txt",
        stage=PipelineStage.REVIEW, review_entered_at=old,
    ))
    seen = {}

    def fake_park(path, manifest):
        seen["manifest"] = manifest
        return Path(path), False

    mocker.patch("pipeline.bins.park_for_review", side_effect=fake_park)
    mocker.patch("graph.build_graph._catalog_upsert")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.relations.dispatch_relations_scan")
    mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):
        human_review_node({"doc_id": "replay-1", "matter_id": "M", "file_path": "r.txt",
                           "original_filename": "r.txt"})
    assert seen["manifest"].review_entered_at == old


# --------------------------------------------------------------------------
# CR8: replaying the node on resume does not re-send the echo / re-scan
# --------------------------------------------------------------------------


@pytest.mark.parametrize("newly_parked, expect_calls", [(True, 1), (False, 0)])
def test_echo_and_relations_scan_fire_only_when_newly_parked(temp_base_dir, mocker, newly_parked, expect_calls):
    from graph.build_graph import human_review_node

    mocker.patch("pipeline.bins.park_for_review", return_value=(Path("r.txt"), newly_parked))
    mocker.patch("graph.build_graph._catalog_upsert")
    mocker.patch("graph.build_graph._emit_stage_audit")
    echo = mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    scan = mocker.patch("pipeline.relations.dispatch_relations_scan")
    mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):
        human_review_node({"doc_id": "echo-1", "matter_id": "M", "file_path": "r.txt",
                           "original_filename": "r.txt"})
    assert echo.call_count == expect_calls
    assert scan.call_count == expect_calls


# --------------------------------------------------------------------------
# T76: the node test distinguishes reaching interrupt() from unrelated errors
# --------------------------------------------------------------------------


def test_human_review_node_without_file_path_reaches_interrupt_with_payload(mocker):
    from graph.build_graph import human_review_node

    spy = mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):
        human_review_node({"doc_id": "x", "matter_id": "M"})
    spy.assert_called_once()
    payload = spy.call_args.args[0]
    assert payload["action"] == "human_review"
    assert payload["doc_id"] == "x" and payload["matter_id"] == "M"


# --------------------------------------------------------------------------
# T40 / T58 / T60 / T59: the human_review node's direct Command(resume) path
# --------------------------------------------------------------------------


def test_direct_resume_with_schema_invalid_corrections_stays_in_review(mocker):
    from graph.build_graph import human_review_node

    answers = iter([
        {"decision": "approved", "resolution": "correct", "extracted_data": {"parties": 5}},
        {"decision": "approved", "resolution": "correct",
         "extracted_data": {"parties": ["Acme"], "effective_date": "2024-01-01"}},
    ])
    seen = []

    def fake_interrupt(payload):
        seen.append(payload)
        return next(answers)

    mocker.patch("graph.build_graph.interrupt", side_effect=fake_interrupt)
    out = human_review_node({
        "doc_id": "direct-1", "matter_id": "M", "doc_type": "contract",
        "extracted_data": {"parties": ["Acme"]},
    })
    assert [p["action"] for p in seen] == ["human_review", "human_review_invalid"]
    assert "contract schema" in seen[1]["detail"]
    assert out["extraction_source"] == "human"
    assert out["extracted_data"]["parties"] == ["Acme"]


def test_direct_resume_with_foreign_fields_is_refused(mocker):
    from graph.build_graph import human_review_node

    answers = iter([
        {"decision": "approved", "resolution": "correct", "extracted_data": {"sender": "x@y.z"}},
        {"decision": "rejected", "resolution": "reject", "reject_reason": "spam"},
    ])
    seen = []
    mocker.patch("graph.build_graph.interrupt", side_effect=lambda p: seen.append(p) or next(answers))
    mocker.patch("graph.build_graph._finalize_human_rejection", return_value={"file_path": ""})
    out = human_review_node({"doc_id": "direct-2", "matter_id": "M", "doc_type": "contract"})
    assert seen[1]["action"] == "human_review_invalid"
    assert out["review_decision"] == "rejected"


def test_reparked_human_fields_keep_their_provenance(temp_base_dir, mocker):
    from graph.build_graph import human_review_node

    seen = {}

    def fake_park(path, manifest):
        seen["manifest"] = manifest
        return Path(path), False

    mocker.patch("pipeline.bins.park_for_review", side_effect=fake_park)
    mocker.patch("graph.build_graph._catalog_upsert")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.relations.dispatch_relations_scan")
    mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):
        human_review_node({
            "doc_id": "repark-1", "matter_id": "M", "file_path": "r.txt", "original_filename": "r.txt",
            "extraction_source": "human", "human_label": {"resolution": "correct"},
            "extracted_data": {"parties": ["A"]},
        })
    assert seen["manifest"].extraction_source == "human"
    assert seen["manifest"].human_label == {"resolution": "correct"}


@pytest.mark.parametrize("resolution", ["reclassify", "approve_as_is"])
def test_fresh_extraction_after_a_human_apply_is_not_marked_human(mocker, resolution):
    from graph.build_graph import human_review_node

    mocker.patch(
        "graph.build_graph.interrupt",
        return_value={"decision": "approved", "resolution": resolution},  # no fields: re-extract
    )
    out = human_review_node({
        "doc_id": "reset-1", "matter_id": "M", "doc_type": "contract",
        "extraction_source": "human", "extracted_data": {"parties": ["A"]},
    })
    assert out["resume_extraction"] is True
    assert "extraction_source" in out and out["extraction_source"] is None


def test_resume_without_a_decision_is_re_asked(mocker):
    from graph.build_graph import human_review_node

    answers = iter([{"resolution": "reclassify"}, {"decision": "approved", "resolution": "reclassify"}])
    seen = []
    mocker.patch("graph.build_graph.interrupt", side_effect=lambda p: seen.append(p) or next(answers))
    out = human_review_node({"doc_id": "nodecision-1", "matter_id": "M", "doc_type": "contract"})
    assert [p["action"] for p in seen] == ["human_review", "human_review_invalid"]
    assert out["review_decision"] == "approved"


# --------------------------------------------------------------------------
# T61: a rejection keeps the catalog's subtype / subclass
# --------------------------------------------------------------------------


def test_finalize_rejection_passes_subtype_and_subclass_to_the_catalog(temp_base_dir, mocker):
    from graph.build_graph import _finalize_human_rejection

    upsert = mocker.patch("graph.build_graph._catalog_upsert")
    mocker.patch("graph.build_graph._maybe_export_warehouse")
    _finalize_human_rejection(
        doc_id="rej-1", matter_id="M", original_filename="x.txt", doc_type="contract",
        classification_confidence=0.9, extraction_confidence=None, trace_id=None,
        esc_reason="r", catalog_extracted=None, dest=None,
        contract_subtype="franchise", doc_subclass="franchise",
    )
    record = upsert.call_args.args[0]
    assert record["contract_subtype"] == "franchise" and record["doc_subclass"] == "franchise"


def test_stateless_rejection_keeps_catalog_subtype(api, temp_base_dir, llm):
    from graph.build_graph import reset_compiled_graph
    from pipeline.bins import load_manifest, save_manifest
    from storage.catalog import lookup_document

    doc_id = _extraction_park(temp_base_dir, "subtype.txt")
    manifest = load_manifest(doc_id)
    manifest.contract_subtype = "franchise"
    manifest.doc_subclass = "franchise"
    save_manifest(manifest)
    reset_compiled_graph()  # no live checkpoint: the stateless rejection path
    r = _resolve(api, doc_id, resolution="reject", reject_reason="spam")
    assert r.status_code == 200, r.text
    row = asyncio.run(lookup_document(doc_id=doc_id))
    assert row.stage == "failed"
    assert row.contract_subtype == "franchise" and row.doc_subclass == "franchise"


# --------------------------------------------------------------------------
# T72: an aborted human-applied run keeps its provenance
# --------------------------------------------------------------------------


def test_aborted_human_applied_run_keeps_label_and_source(temp_base_dir, mocker):
    from graph.build_graph import _finalize_aborted
    from pipeline.bins import load_manifest

    mocker.patch("graph.build_graph._write_catalog_record")
    mocker.patch("graph.build_graph._emit_stage_audit")
    mocker.patch("graph.build_graph._maybe_export_warehouse")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.relations.dispatch_relations_scan")
    label = {"resolution": "correct", "reviewer": "pat"}
    _finalize_aborted(
        {"doc_id": "abort-1", "matter_id": "M", "original_filename": "a.txt",
         "extraction_source": "human", "human_label": label},
        "boom",
    )
    m = load_manifest("abort-1")
    assert m.stage.value == "failed"
    assert m.extraction_source == "human" and m.human_label == label


def test_live_resume_hands_the_label_to_the_abort_path(mocker):
    from graph.build_graph import _live_resume
    from schemas.manifest import DocumentManifest

    run = mocker.patch("graph.build_graph._execute_run", return_value={})
    manifest = DocumentManifest(doc_id="live-1", matter_id="M", original_filename="l.txt", doc_type="contract")
    label = {"resolution": "correct"}
    _live_resume(manifest, "thread-1", {
        "decision": "approved", "resolution": "correct",
        "extracted_data": {"parties": ["A"]}, "human_label": label,
    })
    state = run.call_args.args[0]
    assert state["extraction_source"] == "human" and state["human_label"] == label
    # A re-extraction is model output: no human provenance on the run state.
    run.reset_mock()
    _live_resume(manifest, "thread-1", {"decision": "approved", "resolution": "reclassify", "human_label": label})
    state = run.call_args.args[0]
    assert "extraction_source" not in state and state["human_label"] == label


# --------------------------------------------------------------------------
# T62: scores of a human-applied run describe the MODEL's extraction
# --------------------------------------------------------------------------


def test_scoring_view_of_a_human_run_uses_the_model_snapshot():
    from graph.build_graph import _model_scoring_view

    result = {
        "extraction_source": "human", "doc_type": "contract",
        "extracted_data": {"parties": ["Fixed Co"]},
        "human_label": {"model_snapshot": {"extracted_data": {"parties": 5}, "parse_error": False}},
    }
    view = _model_scoring_view(result)
    assert view["extracted_data"] == {"parties": 5}
    assert result["extracted_data"] == {"parties": ["Fixed Co"]}  # the result itself is untouched
    parse_failed = {**result, "human_label": {"model_snapshot": {"extracted_data": None, "parse_error": True}}}
    assert _model_scoring_view(parse_failed)["extracted_data"] == {"_parse_error": True}
    # Model output, or a human run with no snapshot (direct graph client): unchanged.
    model = {"extraction_source": None, "extracted_data": {"parties": ["A"]}}
    assert _model_scoring_view(model) is model
    no_label = {"extraction_source": "human", "extracted_data": {"parties": ["A"]}}
    assert _model_scoring_view(no_label) is no_label


def test_correct_scores_the_model_extraction_not_the_repair(api, temp_base_dir, llm, mocker):
    from observability import scores as pipeline_scores

    doc_id = _extraction_park(temp_base_dir, "scored.txt")
    _forbid_llm(mocker, llm)
    spy = mocker.spy(pipeline_scores, "emit_pipeline_scores")
    r = _resolve(api, doc_id, resolution="correct", field_corrections={"parties": ["Acme Corp", "Beta LLC"]})
    assert r.status_code == 200, r.text
    scored_state = spy.call_args.args[0]
    assert scored_state["extracted_data"]["parties"] == ["Acme Corp"]  # what the model said


# --------------------------------------------------------------------------
# T39: filler removal never merges distinct jurisdictions
# --------------------------------------------------------------------------


def test_jurisdiction_key_keeps_equivalent_names_together_and_distinct_ones_apart():
    from graph.build_graph import _jurisdiction_key, _normalized_compare

    assert _jurisdiction_key("State of Delaware") == _jurisdiction_key("Delaware")
    assert _jurisdiction_key("the laws of the State of New York") == _jurisdiction_key("New York")
    assert _jurisdiction_key("West Virginia") != _jurisdiction_key("Virginia")
    assert _jurisdiction_key("State of Mexico") != _jurisdiction_key("Mexico")
    assert _jurisdiction_key("laws of the State of Mexico") == _jurisdiction_key("State of Mexico")
    assert _jurisdiction_key("New Mexico") != _jurisdiction_key("Mexico")
    assert not _normalized_compare("Mexico", "State of Mexico", "governing_law")
    assert _normalized_compare("Delaware", "State of Delaware", "governing_law")


# --------------------------------------------------------------------------
# T69 / T70 / T71: plan_resolution
# --------------------------------------------------------------------------


def _plan_inputs(**after):
    from schemas.manifest import DocumentManifest, PipelineStage

    before = DocumentManifest(
        doc_id="plan-1", matter_id="M", original_filename="p.txt", stage=PipelineStage.REVIEW,
        doc_type="contract", contract_subtype="franchise", doc_subclass="franchise",
        extracted_data={"parties": ["Acme"], "effective_date": "2024-01-01", "confidence": 0.9},
    )
    manifest = before.model_copy(deep=True)
    for key, value in after.items():
        setattr(manifest, key, value)
    return before, manifest


@pytest.mark.parametrize("resolution, extra", [
    ("approve_as_is", {}),
    ("correct", {"field_corrections": {"parties": ["Acme", "Beta"]}}),
])
def test_subtype_change_with_parked_fields_requires_reclassify(resolution, extra):
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs(contract_subtype="license", doc_subclass="license")
    with pytest.raises(ValueError, match="reclassify"):
        plan_resolution(
            before=before, manifest=manifest,
            class_override={"contract_subtype": "license", "doc_subclass": "license"},
            decision="approved", resolution=resolution, **extra,
        )


def test_subtype_change_without_parked_fields_keeps_the_class_confirm_behaviour():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs(contract_subtype="license", extracted_data=None)
    before.extracted_data = None
    plan = plan_resolution(
        before=before, manifest=manifest, class_override={"contract_subtype": "license"},
        decision="approved", resolution="approve_as_is",
    )
    assert plan.route == "reextract" and plan.resolution == "approve_as_is"


def test_reclassify_accepts_an_explicit_clear_of_subtype_and_subclass():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs(contract_subtype=None, doc_subclass=None)
    plan = plan_resolution(
        before=before, manifest=manifest, class_override={},  # a clear is not in the applied dict
        decision="approved", resolution="reclassify",
    )
    assert plan.route == "reextract" and plan.resolution == "reclassify"


def test_reclassify_with_nothing_changed_is_still_refused():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs()
    with pytest.raises(ValueError, match="requires"):
        plan_resolution(before=before, manifest=manifest, class_override={},
                        decision="approved", resolution="reclassify")


def test_clearing_subclass_through_the_api_reaches_reextraction(api, temp_base_dir, llm):
    from pipeline.bins import load_manifest, save_manifest

    doc_id = _sorter_park(temp_base_dir, "clear.txt")
    manifest = load_manifest(doc_id)
    manifest.contract_subtype = "franchise"
    manifest.doc_subclass = "franchise"
    save_manifest(manifest)
    llm.chat.completions.create.side_effect = None
    r = _resolve(api, doc_id, resolution="reclassify", contract_subtype="", doc_subclass="")
    assert r.status_code == 200, r.text
    assert r.json()["resolution"] == "reclassify" and "resume" in r.json()


def test_legacy_complete_that_drops_a_parked_field_is_recorded_as_a_correction():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs()
    # The full replacement omits effective_date; every kept value is unchanged.
    plan = plan_resolution(
        before=before, manifest=manifest, class_override={}, decision="approved",
        disposition="complete", submitted_extracted={"parties": ["Acme"]},
    )
    assert plan.resolution == "correct"
    assert plan.label["removed_fields"] == ["effective_date"]
    assert plan.extracted_data == {"parties": ["Acme"]}


def test_legacy_complete_of_identical_fields_is_still_approve_as_is():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _plan_inputs()
    # ``confidence`` is bookkeeping, not a field: leaving it out removes nothing.
    plan = plan_resolution(
        before=before, manifest=manifest, class_override={}, decision="approved",
        disposition="complete",
        submitted_extracted={"parties": ["Acme"], "effective_date": "2024-01-01"},
    )
    assert plan.resolution == "approve_as_is"
    assert "removed_fields" not in plan.label
