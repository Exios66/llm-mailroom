# Mailroom contract and trace adapters

`src/mailroom_contract/` defines the pipeline's node-to-observation bindings,
semantic observation types, and emitted score names, types and limits. It imports
no trace SDK, graph, configuration or environment. `build_graph()` checks every
node wrapper against these bindings before wiring edges. Edge topology remains in
`graph/route_spec.py`; document classes, thresholds and agent mappings remain in
`config/taxonomy.yaml`. The Dojo metric registry remains authoritative for scoring
rules. `deterministic_verdict` is an existing Mailroom score whose label is
computed by Dojo's `pipeline_verdict` helper; it is declared separately from
Dojo's metric registry.

`observability/tracing.py` applies the same root and node semantics to Langfuse,
Braintrust, Phoenix and disabled tracing. Braintrust translates `chain`/`agent`
to `task`, `generation` to `llm`, `evaluator` to `eval`, `retriever` to `tool`,
and `span` to `function`, retaining `mailroom.semantic_type` in metadata.
Phoenix uses the existing OpenTelemetry provider and exporter, translates
types to `openinference.span.kind` (`span` becomes `CHAIN`), and retains
`mailroom.semantic_type`. Curated input/output also use OpenInference's
`input.value` and `output.value` attributes. Local audit visits are captured by `traced_node`
for every backend, including disabled tracing. Backend setup, update or export
failures are logged and do not replay a node body. An external span ID is optional
provenance; the local document and audit IDs do not depend on one.

`contract/mailroom-contract.json` is generated from the core, the compiled graph
and taxonomy with `src/scripts/export_contract.py --write`. Schema 2.0.0 removes
the Langfuse-specific `scores.langfuse_aliases` key. Langfuse's 35-character
transport alias is held in `observability/langfuse_score_adapter.py`; neither it
nor a provider/model/SDK setting appears in the exported contract. The native
diagnostics reader accepts both historical 1.x and current 2.x contracts.

The-Mailroom currently consumes Langfuse traces and must refresh its pinned
contract parser for the 2.0.0 JSON shape. The local-mailroom-sandbox needs a
vendored snapshot refresh to receive this runtime behavior. This change has
only offline adapter and mock-pipeline evidence; no hosted sink run is claimed.

Current-context scores reach Langfuse as configured scores, Braintrust as
numeric scores (categories use their declared numeric values; text stays in
metadata), and Phoenix as `mailroom.score.*` span attributes. Offline scores
addressed by a trace ID are supported by the Langfuse adapter only.
