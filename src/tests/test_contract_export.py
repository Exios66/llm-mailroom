"""The committed display contract (contract/mailroom-contract.json) must match
the pipeline it describes. The-Mailroom is set to vendor it at a pinned commit
(ADR-001), so a stale file would hide drift from the visualizer."""

from __future__ import annotations

import json

import pytest

from scripts import export_contract
from observability.tracing import NODE_OBSERVATION_TYPES


@pytest.fixture(scope="module")
def contract():
    return export_contract.build_contract()


def test_committed_contract_is_current(contract):
    committed = export_contract.CONTRACT_PATH.read_text(encoding="utf-8")
    assert committed == export_contract.render(contract), (
        "contract/mailroom-contract.json is stale; run "
        "`PYTHONPATH=src python src/scripts/export_contract.py --write`"
    )


def test_render_is_deterministic(contract):
    assert export_contract.render(contract) == export_contract.render(
        export_contract.build_contract()
    )


def test_every_graph_node_has_a_typed_span(contract):
    for node in contract["graph"]["nodes"]:
        assert node["span"] in NODE_OBSERVATION_TYPES, node
        assert node["observation_type"] == NODE_OBSERVATION_TYPES[node["span"]]


def test_edges_connect_known_nodes(contract):
    graph = contract["graph"]
    known = {n["id"] for n in graph["nodes"]} | {graph["start"], graph["end"]}
    for edge in graph["edges"]:
        assert edge["source"] in known and edge["target"] in known, edge


def test_doc_class_specialists_are_agents(contract):
    agents = set(contract["agents"])
    for row in contract["doc_classes"]:
        assert row["specialist"] in agents, row
        assert row["fields"], row


def test_contract_is_json_serialisable(contract):
    json.loads(export_contract.render(contract))
