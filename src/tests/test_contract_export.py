"""The committed display contract (contract/mailroom-contract.json) must match
the pipeline it describes. The-Mailroom is set to vendor it at a pinned commit
(ADR-001), so a stale file would hide drift from the visualizer."""

from __future__ import annotations

import json
import os

import pytest

from scripts import export_contract
from observability.tracing import NODE_OBSERVATION_TYPES, traced_node


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
    """Each node names a known span and the type its wrapper opens it as."""
    from graph.build_graph import build_graph

    from langgraph.checkpoint.memory import MemorySaver

    builder_nodes = build_graph(checkpointer=MemorySaver()).builder.nodes
    for node in contract["graph"]["nodes"]:
        assert node["span"] in NODE_OBSERVATION_TYPES, node
        func = builder_nodes[node["id"]].runnable.func
        assert node["observation_type"] == func.mailroom_observation_type, node


def test_node_row_rejects_decorator_drift():
    """A valid type on the wrong graph node cannot rewrite the contract."""
    plain = traced_node("classify-document")(lambda state: state)
    with pytest.raises(ValueError, match="must bind"):
        export_contract.node_row("extract", plain)
    with pytest.raises(ValueError, match="must be"):
        traced_node("classify-document", as_type="evaluator")


def test_node_row_rejects_an_untraced_node():
    """A graph node without ``traced_node`` cannot be mapped to a span."""
    with pytest.raises(RuntimeError, match="not wrapped by traced_node"):
        export_contract.node_row("bare", lambda state: state)


def test_contract_is_provider_neutral(contract):
    assert contract["contract_version"] == "2.0.0"
    assert "langfuse_aliases" not in contract["scores"]
    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)
    assert not {"provider", "model", "sdk", "langfuse_aliases"} & set(keys(contract))
    scores = {row["name"]: row for row in contract["scores"]["configs"]}
    assert scores["classification_confidence"]["min_value"] == 0.0
    assert scores["classification_confidence"]["max_value"] == 1.0
    assert scores["deterministic_verdict"]["categories"] == [
        {"label": "CORRECT", "value": 1.0},
        {"label": "PARTIAL", "value": 0.5},
        {"label": "MISS", "value": 0.0},
    ]


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


def test_export_does_not_open_the_configured_checkpointer(monkeypatch, tmp_path):
    """Reading the graph's shape must not create the SQLite checkpoint DB."""
    import graph.build_graph as bg

    monkeypatch.setenv("MAILROOM_CHECKPOINTER", "sqlite")
    monkeypatch.setattr(bg, "_build_checkpointer",
                        lambda: pytest.fail("exporter opened the configured checkpointer"))
    export_contract.build_contract()


def test_build_contract_leaves_the_tracing_provider_alone(monkeypatch):
    """Assembling the contract must not switch tracing off for the caller."""
    monkeypatch.delenv("OBSERVABILITY_PROVIDER", raising=False)
    export_contract.build_contract()
    assert "OBSERVABILITY_PROVIDER" not in os.environ
