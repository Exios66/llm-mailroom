# llm-mailroom-graph

**An interactive knowledge graph of the pipeline's code, for answering "what calls what" questions.**

| | |
| :--- | :--- |
| Repository | [Exios66/llm-mailroom-graph](https://github.com/Exios66/llm-mailroom-graph) |
| Live site | [exios66.github.io/llm-mailroom-graph](https://exios66.github.io/llm-mailroom-graph/) |
| Monorepo path | `packages/llm-mailroom-graph` (virtual member, static site) |
| Built with | [graphify](https://github.com/Graphify-Labs/graphify), AST-only, no LLM tokens |

## What it does

The site maps llm-mailroom's production code (agents, graph, pipeline, API, storage, observability, LLM layer, schemas, scripts) as a walkable graph: about 1,900 symbols and 4,500 edges grouped into communities. Tests, notebooks and docs are excluded.

| Page | What it shows |
| :--- | :--- |
| Architecture map (`index.html`) | Layers, then modules, then symbols, with a strip that jumps to the 13 pipeline nodes |
| Module tree (`tree.html`) | The same graph as a collapsible file tree |
| Report (`report.html`) | Central nodes, communities, bridges, suggested questions |
| Classic view (`graph.html`) | graphify's force-directed canvas |

## When to use it

Use it to find structure quickly: where a function is called from, which modules a node depends on. It is a build artifact, so when it disagrees with the source code or the board, the source and the board win.

## Rebuilding

The README has the exact commands: clone llm-mailroom, run `graphify extract` and `graphify cluster-only`, then `scripts/regenerate_graph_site.py` to rebuild `index.html` and `report.html`.

## Related

- [mailroom-dev-graph](https://exios66.github.io/mailroom-dev-graph/) maps an earlier monorepo the same way.
- [llm-entity-extraction-graph](https://exios66.github.io/llm-entity-extraction-graph/) maps the prompt experiment loop.

## Its documentation

- [README](https://github.com/Exios66/llm-mailroom-graph/blob/main/README.md)
- [GRAPH_REPORT.md](https://github.com/Exios66/llm-mailroom-graph/blob/main/GRAPH_REPORT.md) — the machine-written audit of the last build
