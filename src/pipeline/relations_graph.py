"""Relations knowledge graphs (HUB-040).

Projections of the relations ledger into lawyer's-research views:

- **Matter graph** — documents as typed nodes + RELATED-MATTER bridge nodes
  (cross-matter edges aggregated to the bridge).
- **Global matter graph** — matters as nodes; document-level edges collapsed
  to matter-pair weights (count x mean score).
- **Document ego-graph** — one document's neighborhood (depth 1).

Formats (``MAILROOM_BASE_DIR/relations/graphs/`` — gitignored heavy-asset
rule, like ``warehouse/``): GraphJSON + GraphML always (stdlib — the
Gmail-channel no-new-deps precedent); interactive Plotly HTML and static PNG
only when the optional libs are installed (the warehouse-export
``auto`` pattern — graceful skip, never a hard dependency).

Every render logs ``relations_graph_rendered`` to the hash-chained relations
ledger — the ledger stays the single longitudinal record.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

import structlog

from .env import load_env

logger = structlog.get_logger(__name__)


def graphs_dir() -> Path:
    from pipeline.bins import get_base_dir

    path = get_base_dir() / "relations" / "graphs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def graphs_enabled() -> bool:
    from pipeline.relations import relations_config, relations_enabled

    return relations_enabled() and bool(relations_config().get("graphs", True))


# ---------------------------------------------------------------------------
# Projections


def _all_edges() -> list[dict]:
    import asyncio

    from storage import relations as R

    return asyncio.run(R.all_edges())


def _catalog_meta() -> dict[str, dict]:
    import asyncio

    from sqlalchemy import select

    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    async def _q():
        ensure_schema()
        async with async_session() as session:
            rows = (await session.execute(select(DocumentRecord))).scalars().all()
            return {
                r.doc_id: {
                    "matter_id": r.matter_id,
                    "doc_type": r.doc_type or "unknown",
                    "original_filename": r.original_filename,
                }
                for r in rows
            }

    return asyncio.run(_q())


def graph_data(
    *,
    matter_id: str | None = None,
    global_view: bool = False,
    ego_doc_id: str | None = None,
) -> dict:
    """Build the GraphJSON payload ({nodes, links}) for one projection."""
    edges = _all_edges()
    meta = _catalog_meta()

    if global_view:
        return _global_graph(edges, meta)
    if ego_doc_id:
        return _ego_graph(edges, meta, ego_doc_id)
    return _matter_graph(edges, meta, matter_id or "DEFAULT")


def _global_graph(edges: list[dict], meta: dict[str, dict]) -> dict:
    """Matter-level aggregation: matter nodes, document edges collapsed to
    matter-pair weights (count x mean score)."""
    pair_scores: dict[tuple[str, str], list[float]] = {}
    for e in edges:
        sm = (meta.get(e["source_doc_id"]) or {}).get("matter_id") or e.get("source_matter_id") or "?"
        tm = (meta.get(e["target_doc_id"]) or {}).get("matter_id") or e.get("target_matter_id") or "?"
        if sm == tm:
            continue  # intra-matter edges are the matter graphs' job — the
            # global view is the INTER-matter picture (no self-loops)
        a, b = (sm, tm) if sm <= tm else (tm, sm)
        pair_scores.setdefault((a, b), []).append(float(e.get("score") or 0.0))
    matter_docs: dict[str, int] = {}
    for doc_id, m in meta.items():
        matter_docs[m.get("matter_id") or "?"] = matter_docs.get(m.get("matter_id") or "?", 0) + 1
    nodes = [
        {"id": m, "label": m, "kind": "matter", "group": "matter", "docs": count}
        for m, count in sorted(matter_docs.items())
    ]
    links = [
        {
            "source": a,
            "target": b,
            "relation_type": "matter_relation",
            "weight": round(len(scores) * (sum(scores) / len(scores)), 4),
            "edges": len(scores),
            "mean_score": round(sum(scores) / len(scores), 4),
        }
        for (a, b), scores in sorted(pair_scores.items())
    ]
    return {"directed": False, "multigraph": False, "graph": {"view": "global"}, "nodes": nodes, "links": links}


def _matter_graph(edges: list[dict], meta: dict[str, dict], matter_id: str) -> dict:
    """Documents of one matter as typed nodes; cross-matter edges collapse to
    RELATED-MATTER bridge nodes (the lawyer's cross-file picture)."""
    docs_in_matter = {
        doc_id: m for doc_id, m in meta.items() if (m.get("matter_id") == matter_id)
    }
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    for e in edges:
        src, dst = e["source_doc_id"], e["target_doc_id"]
        src_m = (meta.get(src) or {}).get("matter_id")
        dst_m = (meta.get(dst) or {}).get("matter_id")
        score = float(e.get("score") or 0.0)
        if src in docs_in_matter and dst in docs_in_matter:
            for doc_id in (src, dst):
                m = meta[doc_id]
                nodes[doc_id] = {
                    "id": doc_id,
                    "label": m.get("original_filename") or doc_id,
                    "kind": "doc",
                    "group": m.get("doc_type") or "unknown",
                }
            links.append(
                {
                    "source": src,
                    "target": dst,
                    "relation_type": e["relation_type"],
                    "weight": score,
                    "method": e.get("method"),
                }
            )
        elif src in docs_in_matter or dst in docs_in_matter:
            # Cross-matter edge: the outer side collapses to a RELATED-MATTER
            # bridge node (the lawyer's cross-file picture).
            inner, outer = (src, dst) if src in docs_in_matter else (dst, src)
            outer_matter = dst_m if outer == dst else src_m
            bridge = f"matter::{outer_matter or '?'}"
            inner_meta = meta.get(inner) or {}
            nodes[inner] = {
                "id": inner,
                "label": inner_meta.get("original_filename") or inner,
                "kind": "doc",
                "group": inner_meta.get("doc_type") or "unknown",
            }
            nodes[bridge] = {
                "id": bridge,
                "label": f"matter {outer_matter or '?'}",
                "kind": "matter_bridge",
                "group": "matter",
            }
            links.append(
                {
                    "source": inner,
                    "target": bridge,
                    "relation_type": "cross_matter_link",
                    "weight": score,
                }
            )
    return {
        "directed": False,
        "multigraph": False,
        "graph": {"view": "matter", "matter_id": matter_id},
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "links": links,
    }


def _ego_graph(edges: list[dict], meta: dict[str, dict], doc_id: str) -> dict:
    nodes: dict[str, dict] = {}
    m = meta.get(doc_id) or {}
    nodes[doc_id] = {
        "id": doc_id,
        "label": m.get("original_filename") or doc_id,
        "kind": "doc",
        "group": m.get("doc_type") or "unknown",
    }
    links = []
    for e in edges:
        if doc_id in (e["source_doc_id"], e["target_doc_id"]):
            other = e["target_doc_id"] if e["source_doc_id"] == doc_id else e["source_doc_id"]
            om = meta.get(other) or {}
            nodes[other] = {
                "id": other,
                "label": om.get("original_filename") or other,
                "kind": "doc",
                "group": om.get("doc_type") or "unknown",
            }
            links.append(
                {
                    "source": e["source_doc_id"],
                    "target": e["target_doc_id"],
                    "relation_type": e["relation_type"],
                    "weight": float(e.get("score") or 0.0),
                    "method": e.get("method"),
                }
            )
    return {
        "directed": False,
        "multigraph": False,
        "graph": {"view": "ego", "doc_id": doc_id},
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "links": links,
    }


# ---------------------------------------------------------------------------
# Renderers


def write_json(data: dict, path: Path) -> Path:
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def write_graphml(data: dict, path: Path) -> Path:
    """GraphML (stdlib XML) — importable into Gephi / Neo4j / yWorks."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
        'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">',
        '<key id="d0" for="node" attr.name="label" attr.type="string"/>',
        '<key id="d1" for="node" attr.name="kind" attr.type="string"/>',
        '<key id="d2" for="node" attr.name="group" attr.type="string"/>',
        '<key id="d3" for="edge" attr.name="relation_type" attr.type="string"/>',
        '<key id="d4" for="edge" attr.name="weight" attr.type="double"/>',
        f'<graph id="G" edgedefault="undirected">',
    ]
    for n in data.get("nodes", []):
        parts.append(
            f'  <node id="{escape(str(n["id"]))}">'
            f'<data key="d0">{escape(str(n.get("label", n["id"])))}</data>'
            f'<data key="d1">{escape(str(n.get("kind", "doc")))}</data>'
            f'<data key="d2">{escape(str(n.get("group", "")))}</data>'
            "</node>"
        )
    for i, l in enumerate(data.get("links", [])):
        parts.append(
            f'  <edge id="e{i}" source="{escape(str(l["source"]))}" target="{escape(str(l["target"]))}">'
            f'<data key="d3">{escape(str(l.get("relation_type", "")))}</data>'
            f'<data key="d4">{float(l.get("weight", 0.0))}</data>'
            "</edge>"
        )
    parts.append("</graph>")
    parts.append("</graphml>")
    path.write_text("\n".join(parts))
    return path


def _circular_positions(nodes: list[dict]) -> dict[str, tuple[float, float]]:
    """Deterministic circular layout (no networkx dependency)."""
    n = len(nodes)
    if n == 0:
        return {}
    positions = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / n
        positions[str(node["id"])] = (math.cos(angle), math.sin(angle))
    return positions


def write_html(data: dict, path: Path) -> Path | None:
    """Interactive Plotly HTML — None (graceful skip) when plotly is absent."""
    try:
        import plotly.graph_objects as go  # noqa: F401
    except Exception:
        logger.info("relations_graph_html_skipped", reason="plotly not installed")
        return None
    import plotly.graph_objects as go

    positions = _circular_positions(data.get("nodes", []))
    edge_x, edge_y = [], []
    for l in data.get("links", []):
        src = positions.get(str(l["source"]))
        dst = positions.get(str(l["target"]))
        if src and dst:
            edge_x += [src[0], dst[0], None]
            edge_y += [src[1], dst[1], None]
    edge_trace = go.Scatter(
        x=edge_x, y=edge_y, mode="lines", line={"width": 0.8, "color": "#888"},
        hoverinfo="none", showlegend=False,
    )
    node_x, node_y, labels, colors, sizes = [], [], [], [], []
    palette = {"matter": "#d29922", "contract": "#818cf8", "insurance_claim": "#3fb950",
               "correspondence": "#58a6ff", "corporate_record": "#bc8cff", "unknown": "#8b949e"}
    for n in data.get("nodes", []):
        pos = positions.get(str(n["id"]))
        if not pos:
            continue
        node_x.append(pos[0])
        node_y.append(pos[1])
        labels.append(f"{n.get('label', n['id'])}<br>{n.get('kind', '')}")
        colors.append(palette.get(str(n.get("group")), "#8b949e"))
        sizes.append(18 if n.get("kind") == "matter" or n.get("kind") == "matter_bridge" else 11)
    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text", textposition="top center",
        marker={"size": sizes, "color": colors, "line": {"width": 1, "color": "#30363d"}},
        text=[n.get("label", "")[:40] for n in data.get("nodes", [])],
        hovertext=labels, hoverinfo="text", showlegend=False,
    )
    view = (data.get("graph") or {}).get("view", "graph")
    title = (data.get("graph") or {}).get("matter_id") or view
    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        title=f"Mailroom relations — {view} ({title})",
        showlegend=False, hovermode="closest",
        xaxis={"visible": False}, yaxis={"visible": False},
        template=None, margin={"l": 20, "r": 20, "t": 50, "b": 20},
    )
    fig.write_html(str(path), include_plotlyjs="cdn")
    return path


def write_png(data: dict, path: Path) -> Path | None:
    """Static render — None (graceful skip) when matplotlib is absent."""
    try:
        import matplotlib
    except Exception:
        logger.info("relations_graph_png_skipped", reason="matplotlib not installed")
        return None
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    positions = _circular_positions(data.get("nodes", []))
    fig, ax = plt.subplots(figsize=(10, 8))
    for l in data.get("links", []):
        src = positions.get(str(l["source"]))
        dst = positions.get(str(l["target"]))
        if src and dst:
            ax.plot([src[0], dst[0]], [src[1], dst[1]], color="#aaaaaa", linewidth=0.8, zorder=1)
    for n in data.get("nodes", []):
        pos = positions.get(str(n["id"]))
        if not pos:
            continue
        ax.scatter(*pos, s=140 if "matter" in str(n.get("kind")) else 40, zorder=2)
        ax.annotate(str(n.get("label", ""))[:28], pos, fontsize=6, xytext=(4, 4), textcoords="offset points")
    view = (data.get("graph") or {}).get("view", "graph")
    ax.set_title(f"Mailroom relations — {view}")
    ax.set_axis_off()
    fig.savefig(str(path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Refresh + CLI


def _log_render(event_detail: dict) -> None:
    try:
        import asyncio

        from storage import relations as R

        asyncio.run(R.write_relation_log_entry("relations_graph_rendered", event_detail))
    except Exception:
        logger.debug("relations_graph_render_log_failed")


def refresh_graphs(matter_ids: list[str] | None = None) -> dict:
    """(Re)render graphs — the touched matters + the global view. Called at
    scan completion (incremental) and by the CLI."""
    if not graphs_enabled():
        return {"skipped": "disabled"}
    out_dir = graphs_dir()
    rendered: list[str] = []

    def _render(name: str, data: dict) -> None:
        write_json(data, out_dir / f"{name}.json")
        write_graphml(data, out_dir / f"{name}.graphml")
        if write_html(data, out_dir / f"{name}.html"):
            rendered.append(f"{name}.html")
        if write_png(data, out_dir / f"{name}.png"):
            rendered.append(f"{name}.png")
        rendered.extend([f"{name}.json", f"{name}.graphml"])
        _log_render(
            {
                "graph": name,
                "nodes": len(data.get("nodes", [])),
                "links": len(data.get("links", [])),
            }
        )

    meta = _catalog_meta()
    matters = set(matter_ids or [])
    if not matters:
        matters = {m.get("matter_id") or "?" for m in meta.values()}
    for matter in sorted(matters):
        if matter == "?":
            continue
        _render(f"matter-{matter}", graph_data(matter_id=matter))
    _render("global", graph_data(global_view=True))
    return {"rendered": rendered, "matters": sorted(m for m in matters if m != "?")}


def main(argv: list[str] | None = None) -> int:
    """CLI: ``PYTHONPATH=src python -m pipeline.relations_graph [--matter M]
    [--global] [--ego DOC_ID] [--format all|json|graphml|html|png]``"""
    import argparse

    load_env()
    parser = argparse.ArgumentParser(description="Render relations knowledge graphs")
    parser.add_argument("--matter", help="matter id to render")
    parser.add_argument("--global", dest="global_view", action="store_true")
    parser.add_argument("--ego", help="document id for an ego-graph")
    parser.add_argument("--format", default="all", choices=["all", "json", "graphml", "html", "png"])
    args = parser.parse_args(argv)

    if not graphs_enabled():
        print("relations graphs disabled (relations.enabled/graphs or MAILROOM_RELATIONS)")
        return 1
    data = graph_data(
        matter_id=args.matter, global_view=args.global_view, ego_doc_id=args.ego
    )
    out_dir = graphs_dir()
    base = (
        f"matter-{args.matter}"
        if args.matter
        else ("global" if args.global_view else f"ego-{args.ego}")
    )
    renderers = {"json": write_json, "graphml": write_graphml, "html": write_html, "png": write_png}
    formats = list(renderers) if args.format == "all" else [args.format]
    written = []
    for fmt in formats:
        result = renderers[fmt](data, out_dir / f"{base}.{fmt}")
        if result is not None:
            written.append(str(result))
    _log_render({"graph": base, "nodes": len(data.get("nodes", [])), "links": len(data.get("links", [])), "cli": True})
    print(f"nodes={len(data['nodes'])} links={len(data['links'])}")
    for path in written:
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
