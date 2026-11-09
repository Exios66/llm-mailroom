"""Deterministic structural checks over a snapshot.

Scope is deliberately narrow: endpoint consistency and reachability from the
declared entry. Nothing here invokes a router, reads an execution record, or
claims that a branch is dead or that a policy is safe.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import __version__
from .models import CheckReport, Coverage, Finding, GraphPayload, Snapshot

CHECKS = ("endpoint-consistency", "entry-reachability")
LIMITATIONS = (
    "structural only: no router invocation, execution, or observation evidence",
    "zero observations do not prove an edge is dead",
    "no claim is made about policy, thresholds, or runtime safety",
)


def run_checks(snapshot: Snapshot) -> CheckReport:
    graph = snapshot.graph
    # Stable IDs make duplicate edge rows collapse to one finding instead of
    # reporting the same defect twice with the same identity.
    findings = {finding.id: finding for finding in _endpoint_consistency(graph)}
    for finding in _entry_reachability(graph):
        findings.setdefault(finding.id, finding)
    ordered = sorted(findings.values(), key=lambda finding: (finding.rule, finding.id))
    return CheckReport(
        tool_version=__version__,
        provenance=snapshot.provenance,
        graph_hash=snapshot.graph_hash,
        findings=ordered,
        coverage=Coverage(checks=list(CHECKS), limitations=list(LIMITATIONS)),
    )


def _endpoint_consistency(graph: GraphPayload) -> list[Finding]:
    """Every edge must land on a declared node or a declared boundary."""
    known = {node.id for node in graph.nodes} | {graph.start, graph.end}
    findings: list[Finding] = []
    for edge in graph.edges:
        missing = [
            side
            for side, value in (("source", edge.source), ("target", edge.target))
            if value not in known
        ]
        if not missing:
            continue
        row = edge.model_dump(mode="json")
        findings.append(
            _finding(
                "dangling-edge",
                "error",
                f"edge {edge.source} -> {edge.target} has an unknown {', '.join(missing)}",
                {"edge": row},
                {"edge": row, "missing_endpoints": missing, "declared_endpoints": len(known)},
            )
        )
    return findings


def _entry_reachability(graph: GraphPayload) -> list[Finding]:
    """Every declared node must be reachable from the declared entry."""
    adjacency: dict[str, list[str]] = {}
    incoming_by_node: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
        incoming_by_node.setdefault(edge.target, []).append(edge.model_dump(mode="json"))
    reachable: set[str] = set()
    stack = [graph.start]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(adjacency.get(current, ()))

    findings: list[Finding] = []
    for node in graph.nodes:
        if node.id in reachable:
            continue
        incoming = incoming_by_node.get(node.id, [])
        findings.append(
            _finding(
                "unreachable-node",
                "error",
                f"node {node.id} is not reachable from {graph.start}",
                {"node": node.id},
                {"node": node.id, "entry": graph.start, "incoming_edges": incoming},
            )
        )
    return findings


def _finding(
    rule: str,
    severity: str,
    message: str,
    affected: dict[str, Any],
    witness: dict[str, Any],
) -> Finding:
    return Finding(
        id=_stable_id(rule, affected),
        rule=rule,
        severity=severity,
        proof_status="confirmed",
        message=message,
        affected=affected,
        witness=witness,
    )


def _stable_id(rule: str, affected: dict[str, Any]) -> str:
    """Rule + affected subject, hashed: same defect, same ID, every run."""
    blob = json.dumps({"rule": rule, "affected": affected}, sort_keys=True, separators=(",", ":"))
    return f"{rule}.{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:12]}"
