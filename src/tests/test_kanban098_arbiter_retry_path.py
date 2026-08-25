"""KANBAN-098 — Lane B composed path: an arbiter-approved re-extraction fires.

Regression pin for the trap demonstrated in ``notebooks/03_review_lanes.ipynb``:
``arbiter_node`` increments ``arbiter_retry_count`` AT APPROVAL TIME (the
retrying extract node keys off that count to weave the fix-list into its
prompt), so the FIRST approval already arrives at ``after_arbiter`` carrying
``count == 1``. The old router demanded ``count < 1`` and silently converted
every approved retry into a human escalation — each half was unit-green while
the composition dead-ended. Fix: the bound is approval-INCLUSIVE (``<= 1``);
only a SECOND arbitration demanding another retry finds the budget spent.

These pins drive the REAL graph through the network-free sandbox seam:
approve -> retry_extract -> re-judge -> compile -> archive, plus the
still-bounded second-demand escalation. Network-free by construction.
"""

from __future__ import annotations

import pytest

from notebooks import pipeline_lab as lab

# Medium-band extraction (0.80): inside the judge gate [low, 0.85), so every
# extraction lands on Lane B — same fuel notebook 03 uses for this scenario.
X80 = {**lab.EXTRACT_HIGH, "confidence": 0.80}


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
        extraction=X80,
        filename="kanban098_approved_retry.txt",
    )


def test_first_approved_retry_dispatches_not_escalates(lab_env) -> None:
    """The composed approve -> re-extract -> re-judge path actually runs."""
    r = _approved_retry_run(lab_env)
    final = r["final"]
    nodes = [s["node"] for s in r["steps"]]

    assert "arbitrate-verdict" in nodes, nodes
    # "extract" and "retry_extract" share the traced name "extract-fields";
    # the one that matters here is the occurrence AFTER the arbiter.
    assert "route-for-review" not in nodes, (
        f"approved retry escalated to humans anyway ({' -> '.join(nodes)})"
    )
    arb_at = nodes.index("arbitrate-verdict")
    assert "extract-fields" in nodes[arb_at + 1:], (
        "arbiter-approved retry dead-ended instead of firing retry_extract "
        f"(path: {' -> '.join(nodes)})"
    )
    # The scripted second judge pass scored the RETRIED extraction complete,
    # proving the graph came back through judge_verify after the retry.
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


def test_second_retry_demand_past_bound_still_escalates(lab_env) -> None:
    """The bound survives the fix: retry does NOT repair the extraction ->
    second arbitration demanding another retry escalates to human review."""
    lab.script_client(
        lab_env["client"],
        judge=[lab.JUDGE_PARTIAL, lab.JUDGE_PARTIAL],  # retried extraction still partial
        arbiter=lab.ARBITER_RETRY,
    )
    r = lab.run_document(
        lab_env,
        lab.DOC_CONTRACT,
        classification=lab.CLASSIFY_CONTRACT_HIGH,
        extraction=X80,
        filename="kanban098_second_demand.txt",
    )
    final = r["final"]
    nodes = [s["node"] for s in r["steps"]]

    # Exactly ONE retry fired (initial extract + one retry = 2 occurrences of
    # the shared "extract-fields" trace name), then the spent budget forced
    # escalation.
    assert nodes.count("extract-fields") == 2, nodes
    assert final.get("arbiter_retry_count") == 2, final.get("arbiter_retry_count")
    assert "route-for-review" in nodes, nodes
