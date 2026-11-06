"""Export the pipeline's display contract for The-Mailroom.

The visualizer used to copy node names, span names, classes, agents, failure
classes, score names and thresholds by hand, and drifted. This script builds
one JSON document from the pipeline's own runtime constants so the visualizer
can vendor it at a pinned commit and fail its tests when it no longer matches
(The-Mailroom ADR-001).

    PYTHONPATH=src python src/scripts/export_contract.py --write   # regenerate
    PYTHONPATH=src python src/scripts/export_contract.py --check   # exit 1 if stale

The committed file is ``contract/mailroom-contract.json``. Bump
``CONTRACT_VERSION`` when a field is removed or changes meaning (major) or a
field is added (minor). Node, span, class or score changes do not need a
version bump; they are the data the contract carries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contract" / "mailroom-contract.json"
CONTRACT_VERSION = "1.0.0"
_START = "__start__"
_END = "__end__"


def _pipeline_version() -> str:
    """The package version from ``pyproject.toml``."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


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


def _graph_section() -> dict[str, Any]:
    """Nodes (with span and observation type) and edges of the compiled graph."""
    from graph.build_graph import build_graph

    graph = build_graph(checkpointer=None)
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
    return {"start": _START, "end": _END, "nodes": nodes, "edges": edges}


def _taxonomy() -> dict[str, Any]:
    """The parsed ``src/config/taxonomy.yaml``."""
    import yaml

    path = REPO_ROOT / "src" / "config" / "taxonomy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _doc_classes(taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
    """Document classes in taxonomy order, with fields and sorter subclasses."""
    from langchain_agents.doc_inventories import sorter_subclass_catalog

    rows = []
    for entry in taxonomy.get("doc_classes") or []:
        key = entry["key"]
        rows.append(
            {
                "key": key,
                "label": entry.get("label") or key,
                "schema": entry.get("schema"),
                "specialist": entry.get("specialist"),
                "fields": sorted((entry.get("field_types") or {}).keys()),
                "subclasses": list(sorter_subclass_catalog(key)),
            }
        )
    return rows


def _failure_classes() -> list[str]:
    """The ``failure_class`` values a run can be aborted with."""
    from pipeline import failures

    names = [
        "LLM_TIMEOUT", "LLM_AUTH", "LLM_RATE_LIMIT", "LLM_TRANSIENT",
        "IO_ERROR", "SCHEMA_ERROR", "RUN_BUDGET", "UNEXPECTED",
    ]
    return [getattr(failures, n) for n in names]


def _scores() -> dict[str, Any]:
    """Score configs (name, type, categories) and Langfuse name aliases."""
    from observability.scores import LANGFUSE_SCORE_NAME_ALIASES, SCORE_CONFIGS

    configs = []
    for cfg in SCORE_CONFIGS:
        row = {"name": cfg["name"], "data_type": cfg.get("data_type")}
        if cfg.get("categories"):
            row["categories"] = [c.get("label") for c in cfg["categories"]]
        configs.append(row)
    return {
        "configs": sorted(configs, key=lambda r: r["name"]),
        "langfuse_aliases": dict(sorted(LANGFUSE_SCORE_NAME_ALIASES.items())),
    }


def build_contract() -> dict[str, Any]:
    """The contract as a plain dict (deterministic ordering)."""
    os.environ.setdefault("OBSERVABILITY_PROVIDER", "none")
    from observability.tracing import NODE_OBSERVATION_TYPES

    taxonomy = _taxonomy()
    confidence = dict(taxonomy.get("confidence") or {})
    return {
        "contract_version": CONTRACT_VERSION,
        "pipeline_version": _pipeline_version(),
        "trace_name": "document-pipeline",
        "graph": _graph_section(),
        "spans": dict(sorted(NODE_OBSERVATION_TYPES.items())),
        "agents": sorted((taxonomy.get("agents") or {}).keys()),
        "doc_classes": _doc_classes(taxonomy),
        "unknown_doc_class": "unknown",
        "failure_classes": _failure_classes(),
        "scores": _scores(),
        "confidence": confidence,
    }


def render(contract: dict[str, Any]) -> str:
    """The contract as the exact text committed to ``CONTRACT_PATH``."""
    return json.dumps(contract, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: print the contract, ``--write`` it, or ``--check`` it is current."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate the committed file")
    mode.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = parser.parse_args(argv)

    text = render(build_contract())
    if args.write:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
        return 0
    if args.check:
        current = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.exists() else ""
        if current != text:
            print(
                "contract/mailroom-contract.json is stale; run "
                "`PYTHONPATH=src python src/scripts/export_contract.py --write`",
                file=sys.stderr,
            )
            return 1
        print("contract is current")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
