"""KANBAN-098 — Lane B composed path: an arbiter-approved re-extraction fires.

Regression pin for the trap demonstrated in ``notebooks/03_review_lanes.ipynb``:
``arbiter_node`` increments ``arbiter_retry_count`` AT APPROVAL TIME (the
retrying extract node keys off that count to weave the fix-list into its
prompt), so the FIRST approval already arrives at ``after_arbiter`` carrying
``count == 1``. The bound is approval-INCLUSIVE (``<= arbiter_retry_max``);
with ``arbiter_retry_max: 2``, the third demand escalates.

These pins drive the REAL graph through the network-free sandbox seam:
approve -> retry_extract -> re-judge -> compile -> archive, plus the
still-bounded third-demand escalation. Network-free by construction.
"""

from __future__ import annotations

import pytest

from notebooks import pipeline_lab as lab

# Contract severity judge band: [0.90, 0.97). Land every extraction on Lane B.
X93 = {**lab.EXTRACT_HIGH, "confidence": 0.93}


@pytest.fixture()
def lab_env():
    with lab.lab_sandbox() as env:
        yield env


def _approved_retry_run(env: dict) -> dict:
    """Judge fails once, arbiter approves ONE retry, the retry judge passes."""
    lab.script_client(
        env["client"],
        judge=[lab.JUDGE_PARTIAL, lab.JUDGE_COMPLETE],
        arbiter=lab.ARBITER_RETRY,
    )
    return lab.run_document(
        env,
        lab.DOC_CONTRACT,
        classification=lab.CLASSIFY_CONTRACT_HIGH,
        extraction=X93,
        filename="kanban098_approved_retry.txt",
    )


def test_first_approved_retry_dispatches_not_escalates(lab_env) -> None:
    """The composed approve -> re-extract -> re-judge path actually runs."""
    r = _approved_retry_run(lab_env)
    final = r["final"]
    nodes = [s["node"] for s in r["steps"]]

    assert "arbitrate-verdict" in nodes, nodes
    assert "route-for-review" not in nodes, (
        f"approved retry escalated to humans anyway ({' -> '.join(nodes)})"
    )
    arb_at = nodes.index("arbitrate-verdict")
    assert "extract-fields" in nodes[arb_at + 1:], (
        "arbiter-approved retry dead-ended instead of firing retry_extract "
        f"(path: {' -> '.join(nodes)})"
    )
    assert final.get("judge_verdict") == "complete", final.get("judge_verdict")


def test_first_approved_retry_archives_clean(lab_env) -> None:
    r = _approved_retry_run(lab_env)
    final = r["final"]

    assert final.get("arbiter_decision") == "retry_extraction"
    assert final.get("arbiter_retry_count") == 1
    assert final.get("stage") == "archived", (
        f"expected archived, got {final.get('stage')!r} "
        f"(escalation: {final.get('escalation_reason')!r})"
    )


def test_third_retry_demand_past_bound_still_escalates(lab_env) -> None:
    """Lane B budgets force escalation: two arbiter-approved retries, then
    judge_max_passes / arbiter_retry_max stop further ping-pong."""
    lab.script_client(
        lab_env["client"],
        judge=[lab.JUDGE_PARTIAL, lab.JUDGE_PARTIAL, lab.JUDGE_PARTIAL],
        arbiter=lab.ARBITER_RETRY,
    )
    r = lab.run_document(
        lab_env,
        lab.DOC_CONTRACT,
        classification=lab.CLASSIFY_CONTRACT_HIGH,
        extraction=X93,
        filename="kanban098_third_demand.txt",
    )
    final = r["final"]
    nodes = [s["node"] for s in r["steps"]]

    # initial extract + two arbiter-approved retries = 3 extract-fields
    assert nodes.count("extract-fields") == 3, nodes
    # Two arbiter approvals; the third incomplete judge pass hits
    # judge_max_passes and escalates without a third arbiter increment.
    assert final.get("arbiter_retry_count") == 2, final.get("arbiter_retry_count")
    assert final.get("judge_pass_count", 0) >= 3 or "route-for-review" in nodes
    assert "route-for-review" in nodes, nodes
