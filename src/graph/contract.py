"""The compiled pipeline graph, as the display contract describes it.

One source of truth for the shipped wiring: ``src/scripts/export_contract.py``
writes ``contract/mailroom-contract.json`` from this helper and the native
pipeline diagnostics tool (``tools/pipeline-diagnostics``) reads the same
helper, so their node/edge inventories cannot drift.

Importing this module does not build the graph or load configuration; the
graph package still installs its Dojo import fallback. The graph is built
only when requested, with an explicit in-memory checkpointer.
"""

from __future__ import annotations

from typing import Any

START = "__start__"
END = "__end__"


def node_row(node_id: str, func: Any) -> dict[str, str]:
    """One graph node as the contract lists it.

    ``func`` is the node's callable. It must be wrapped by
    ``observability.tracing.traced_node``, which records the span name and the
    observation type the span is opened as. That type includes an ``as_type``
    override, so the contract shows what a trace backend actually receives,
    not only the ``NODE_OBSERVATION_TYPES`` default for the span name.
    """
    span = getattr(func, "mailroom_span_name", None)
    if not span:
        raise RuntimeError(f"graph node {node_id!r} is not wrapped by traced_node")
    obs_type = getattr(func, "mailroom_observation_type", None)
    if not obs_type:
        from observability.tracing import observation_type_for

        obs_type = observation_type_for(span)
    return {"id": node_id, "span": span, "observation_type": obs_type}


def compiled_graph():
    """Build the pipeline graph with an explicit in-memory saver.

    ``None`` would let ``build_graph`` open the configured checkpointer, which
    with ``MAILROOM_CHECKPOINTER=sqlite`` creates the on-disk database just to
    read the graph's shape.
    """
    from graph.build_graph import build_graph
    from langgraph.checkpoint.memory import MemorySaver

    return build_graph(checkpointer=MemorySaver())


def graph_section(graph: Any | None = None) -> dict[str, Any]:
    """Nodes (with span and observation type) and edges of the compiled graph."""
    if graph is None:
        graph = compiled_graph()
    drawn = graph.get_graph()
    nodes = [
        node_row(node_id, getattr(spec.runnable, "func", None))
        for node_id, spec in sorted(graph.builder.nodes.items())
    ]
    edges = sorted(
        (
            {
                "source": e.source,
                "target": e.target,
                "conditional": bool(e.conditional),
                "label": e.data if isinstance(e.data, str) else None,
            }
            for e in drawn.edges
        ),
        key=lambda e: (e["source"], e["target"], e["label"] or ""),
    )
    return {"start": START, "end": END, "nodes": nodes, "edges": edges}
