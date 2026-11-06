"""The committed display contract (contract/mailroom-contract.json) must match
the pipeline it describes. The-Mailroom is set to vendor it at a pinned commit
(ADR-001), so a stale file would hide drift from the visualizer."""

from __future__ import annotations

import json

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


def test_node_row_uses_the_as_type_override():
    """A node opened with ``as_type`` is listed with that type, not the default."""
    span = "classify-document"
    default = NODE_OBSERVATION_TYPES[span]
    override = "evaluator" if default != "evaluator" else "retriever"

    plain = traced_node(span)(lambda state: state)
    overridden = traced_node(span, as_type=override)(lambda state: state)

    assert export_contract.node_row("plain", plain)["observation_type"] == default
    row = export_contract.node_row("overridden", overridden)
    assert row == {"id": "overridden", "span": span, "observation_type": override}


def test_node_row_rejects_an_untraced_node():
    """A graph node without ``traced_node`` cannot be mapped to a span."""
    with pytest.raises(RuntimeError, match="not wrapped by traced_node"):
        export_contract.node_row("bare", lambda state: state)


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
