"""Structural checks: stable findings, minimal witnesses, no dead-edge claims."""

from __future__ import annotations

from mailroom_diagnostics.checks import run_checks
from mailroom_diagnostics.snapshot import build_workspace_snapshot, load_artifact


def _findings(path):
    return run_checks(load_artifact(path)).findings


def test_committed_contract_has_no_structural_findings(contract_path):
    assert _findings(contract_path) == []


def test_dangling_edge_is_reported_not_dropped(mutated_contract):
    def add_edge(data):
        data["graph"]["edges"].append(
            {"source": "archive", "target": "ghost", "conditional": False, "label": None}
        )

    findings = _findings(mutated_contract(add_edge))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "dangling-edge"
    assert finding.severity == "error"
    assert finding.proof_status == "confirmed"
    assert finding.affected == {
        "edge": {
            "source": "archive",
            "target": "ghost",
            "conditional": False,
            "label": None,
        }
    }
    assert finding.witness["missing_endpoints"] == ["target"]


def test_finding_ids_are_stable_across_runs(mutated_contract):
    def add_edge(data):
        data["graph"]["edges"].append(
            {"source": "__start__", "target": "ghost", "conditional": True, "label": None}
        )

    path = mutated_contract(add_edge)
    first = [finding.id for finding in _findings(path)]
    second = [finding.id for finding in _findings(path)]
    assert first == second
    assert first and first[0].startswith("dangling-edge.")


def test_removing_the_injected_edge_clears_the_finding(mutated_contract):
    def add_edge(data):
        data["graph"]["edges"].append(
            {"source": "archive", "target": "ghost", "conditional": False, "label": None}
        )

    assert _findings(mutated_contract(add_edge))
    assert _findings(mutated_contract(lambda data: None)) == []


def test_unreachable_node_has_a_minimal_witness(mutated_contract):
    def add_orphan(data):
        data["graph"]["nodes"].append(
            {"id": "orphan", "span": "orphan-span", "observation_type": "span"}
        )

    findings = _findings(mutated_contract(add_orphan))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "unreachable-node"
    assert finding.affected == {"node": "orphan"}
    assert finding.witness == {
        "node": "orphan",
        "entry": "__start__",
        "incoming_edges": [],
    }


def test_a_self_loop_does_not_make_a_node_reachable(mutated_contract):
    def add_island(data):
        data["graph"]["nodes"].append(
            {"id": "island", "span": "island-span", "observation_type": "agent"}
        )
        data["graph"]["edges"].append(
            {"source": "island", "target": "island", "conditional": True, "label": "retry"}
        )

    findings = _findings(mutated_contract(add_island))
    assert [finding.rule for finding in findings] == ["unreachable-node"]
    assert findings[0].witness["incoming_edges"][0]["source"] == "island"


def test_committed_retry_self_loops_are_not_findings(contract_path):
    """Transient self-loops are real wiring, not structural defects."""
    snapshot = load_artifact(contract_path)
    self_loops = [edge for edge in snapshot.graph.edges if edge.source == edge.target]
    assert self_loops, "expected the committed graph to keep its retry self-loops"
    assert run_checks(snapshot).findings == []


def test_shared_spans_stay_distinct_nodes():
    """Legacy span reuse must not collapse classify / retry / review identity."""
    snapshot = build_workspace_snapshot()
    by_id = {node.id: node for node in snapshot.graph.nodes}
    shared = {name: by_id[name].span for name in ("classify", "retry_classify", "review_classify")}
    assert len(set(shared.values())) == 1
    assert len({node.id for node in snapshot.graph.nodes}) == len(snapshot.graph.nodes)
    assert run_checks(snapshot).findings == []


def test_a_future_revision_graph_needs_no_code_change(minimal_contract):
    """No hardcoded node/edge/case inventory: a 2-node contract checks clean."""
    findings = _findings(minimal_contract)
    assert findings == []
    snapshot = load_artifact(minimal_contract)
    assert {node.id for node in snapshot.graph.nodes} == {"intake", "archive"}


def test_graph_hash_changes_with_topology_and_is_deterministic(minimal_contract):
    first = load_artifact(minimal_contract)
    second = load_artifact(minimal_contract)
    assert first.graph_hash == second.graph_hash

    workspace = build_workspace_snapshot()
    assert workspace.graph_hash != first.graph_hash
