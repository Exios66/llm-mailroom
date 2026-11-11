"""Roadmap M7: every terminal path writes exactly one unified audit row (spec §4.1, §5.1, §5.3)."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_audit_trace import run_graph  # noqa: F401  (fixture)
from tests.test_hitl_resolution import _extraction_park, _resolve, _sorter_park, api, llm  # noqa: F401

# Every terminal path writes the unified row, so the database must be migrated
# first (an explicit operator step in this storage layer, never automatic).
pytestmark = pytest.mark.usefixtures("migrated_audit_db")


def _row(doc_id):
    from audit.writer import get_row

    return asyncio.run(get_row(doc_id))


def _single(row) -> bool:
    """One terminal write: ``doc_id`` is unique, so a second one would show up as a ``resumed`` addendum."""
    return row is not None and not [a for a in row["addenda"] if a["kind"] == "resumed"]


def test_success_fixture_writes_one_row_intake_to_archive(run_graph):
    from audit.row import ROW_DETAIL_KEYS

    order, final = run_graph()
    row = _row(final["doc_id"])
    assert row["event"] == "archived" and row["actor"] == "archivist"
    detail = row["detail"]
    assert tuple(detail) == ROW_DETAIL_KEYS
    assert detail["nodes_visited"][0] == "intake" and detail["nodes_visited"][-1] == "archive"
    assert detail["nodes_visited"] == order
    assert detail["pipeline_success"] is True and detail["stage"] == "archived"
    assert detail["refs"]["archive_path"] and detail["refs"]["file_sha256"] == final["file_sha256"]
    assert {s["stage"] for s in row["sub_rows"]} == {"classification", "extraction"}
    assert _single(_row(final["doc_id"]))


def test_judge_stop_writes_failed_row_naming_node(api, temp_base_dir, llm):
    # The judge says incomplete, the arbiter sends it to a person, who rejects it.
    doc_id = _extraction_park(temp_base_dir, "judge-stop.txt")
    assert _row(doc_id) is None  # a park is not terminal
    assert _resolve(api, doc_id, resolution="reject", reject_reason="spam").status_code == 200
    row = _row(doc_id)
    assert row["event"] == "failed"
    nodes = row["detail"]["nodes_visited"]
    assert nodes[-2:] == ["human_review", "audit_failed"]  # the stopping node, then the terminal node
    assert "judge_verify" in nodes and "arbiter" in nodes
    assert row["detail"]["pipeline_success"] is False and row["detail"]["stage"] == "failed"
    assert row["detail"]["review_decision"] == "rejected"
    assert _single(row)


def test_tray_park_then_fail_writes_failed_row(api, temp_base_dir, llm):
    doc_id = _sorter_park(temp_base_dir, "tray.txt")
    assert _row(doc_id) is None
    assert _resolve(api, doc_id, resolution="reject", reject_reason="spam").status_code == 200
    row = _row(doc_id)
    assert row["event"] == "failed"
    nodes = row["detail"]["nodes_visited"]
    assert nodes[-2:] == ["human_review", "audit_failed"] and "extract" not in nodes
    assert _single(row)


@pytest.mark.parametrize("confidence,event,stage", [(0.95, "triage_archived", "archived"),
                                                     (0.2, "triage_reviewed", "review")])
def test_triage_lane_writes_one_row(temp_base_dir, mocker, confidence, event, stage):
    from tests.test_gmail_triage import _gmail_inbox_file, _terminal_manifest

    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name=f"fnol-{stage}.txt")
    agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    agent.return_value.triage.return_value = {
        "primary_doc_class": "insurance_claim", "doc_subclass": None, "confidence": confidence,
        "gist": "FNOL", "keywords": ["hail"], "extraction": {"claimant": "Acme"},
    }
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    Watcher()._process_existing(inbox_file)
    doc_id = _terminal_manifest(temp_base_dir, inbox_file.name)["doc_id"]
    row = _row(doc_id)
    assert (row["event"], row["actor"], row["detail"]["stage"]) == (event, "triage", stage)
    assert row["detail"]["doc_type"] == "insurance_claim"
    assert row["detail"]["refs"]["file_sha256"] and row["detail"]["refs"]["manifest_path"]
    assert row["detail"]["pipeline_success"] is (event == "triage_archived")
    assert _single(row)


def test_triage_llm_unavailable_writes_reviewed_row(temp_base_dir, mocker):
    from tests.test_gmail_triage import _gmail_inbox_file, _terminal_manifest

    from pipeline.watcher import Watcher

    inbox_file = _gmail_inbox_file(temp_base_dir, name="fnol-429.txt")
    agent = mocker.patch("agents.gmail_triage.GmailTriageAgent")
    agent.return_value.triage.side_effect = RuntimeError("429 upstream")
    mocker.patch("pipeline.watcher._notify_intake_reaction")
    mocker.patch("pipeline.gmail_intake.dispatch_intake_echo")
    mocker.patch("pipeline.gmail_intake.triage_enabled", return_value=True)
    Watcher()._process_existing(inbox_file)
    doc_id = _terminal_manifest(temp_base_dir, inbox_file.name)["doc_id"]
    row = _row(doc_id)
    assert row["event"] == "triage_reviewed" and row["detail"]["stage"] == "review"
    assert row["detail"]["escalation_reason"].startswith("triage_llm_unavailable")


def test_watcher_abort_writes_failed_row(run_graph, temp_base_dir, mocker):
    from graph import build_graph as bg
    from pipeline.limits import RunBudgetExceeded

    def heartbeat(state):
        if state.get("doc_type"):  # the hard limit trips at the node after classification
            raise RunBudgetExceeded(9999, 10)

    mocker.patch.object(bg, "_touch_heartbeat", side_effect=heartbeat)
    source = temp_base_dir / "pipeline" / "inbox" / "abort.txt"
    source.write_text("Service Agreement between Acme and Beta.")
    result = bg.run_pipeline(source, "M-ABORT")
    assert result["stage"] == "failed"
    row = _row(result["doc_id"])
    assert row["event"] == "failed" and row["actor"] == "pipeline"
    detail = row["detail"]
    assert detail["pipeline_success"] is False and detail["error_message"]
    # The checkpoint's trace up to the failing node is kept.
    assert detail["nodes_visited"][:2] == ["intake", "classify"]
    assert _single(_row(result["doc_id"]))
