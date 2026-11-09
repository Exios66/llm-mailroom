import { describe, expect, it } from "vitest";

import { buildViewModel, filterConnections, matchesQuery, START_ID, END_ID } from "./model";
import type { GraphPayload } from "./types";

const graph: GraphPayload = {
  start: START_ID,
  end: END_ID,
  nodes: [
    { id: "intake", span: "ingest-and-prepare", observation_type: "agent" },
    { id: "classify", span: "classify-document", observation_type: "agent" },
    { id: "retry_classify", span: "classify-document", observation_type: "agent" },
    { id: "extract", span: "extract-fields", observation_type: "agent" },
    { id: "archive", span: "archive-document", observation_type: "span" },
  ],
  edges: [
    { source: START_ID, target: "intake", conditional: true, label: null },
    { source: "intake", target: "classify", conditional: false, label: null },
    { source: "classify", target: "extract", conditional: true, label: null },
    { source: "classify", target: "retry_classify", conditional: true, label: null },
    { source: "classify", target: "retry_classify", conditional: true, label: "retry" },
    { source: "retry_classify", target: "retry_classify", conditional: true, label: null },
    { source: "extract", target: "archive", conditional: false, label: null },
    { source: "archive", target: END_ID, conditional: false, label: null },
  ],
};

describe("buildViewModel", () => {
  it("derives a deterministic layered layout from wiring", () => {
    const first = buildViewModel(graph);
    const second = buildViewModel(graph);
    expect(first.nodes.map((node) => [node.id, node.position])).toEqual(
      second.nodes.map((node) => [node.id, node.position]),
    );
    expect(first.depths[START_ID]).toBe(0);
    expect(first.depths.intake).toBe(1);
    expect(first.depths.classify).toBe(2);
    expect(first.depths.retry_classify).toBe(3);
    expect(first.depths.archive).toBe(4);
    expect(first.depths[END_ID]).toBe(5);
  });

  it("adds boundary nodes for the declared start and end", () => {
    const model = buildViewModel(graph);
    const boundaries = model.nodes.filter((node) => node.data.boundary !== null);
    expect(boundaries.map((node) => node.data.boundary)).toEqual(["start", "end"]);
  });

  it("keeps self-loops and duplicate endpoint pairs as distinct connections", () => {
    const model = buildViewModel(graph);
    const retryRows = model.connections.filter(
      (row) => row.source === "classify" && row.target === "retry_classify",
    );
    expect(retryRows).toHaveLength(2);
    expect(new Set(retryRows.map((row) => row.id)).size).toBe(2);
    const selfLoop = model.connections.find((row) => row.selfLoop);
    expect(selfLoop?.source).toBe("retry_classify");
  });

  it("preserves the route label exported for a connection", () => {
    const model = buildViewModel(graph);
    const labelled = model.connections.find((row) => row.label === "retry");
    expect(labelled?.conditional).toBe(true);
  });

  it("reflects an added or removed edge without a frontend inventory", () => {
    const base = buildViewModel(graph);
    const extra = { source: "archive", target: "intake", conditional: true, label: null };
    const added = buildViewModel({ ...graph, edges: [...graph.edges, extra] });
    expect(added.connections).toHaveLength(base.connections.length + 1);
    expect(added.connections.some((row) => row.source === "archive" && row.target === "intake")).toBe(true);

    const removed = buildViewModel({
      ...graph,
      edges: graph.edges.filter((edge) => edge.source !== "extract"),
    });
    expect(removed.connections).toHaveLength(base.connections.length - 1);
    expect(removed.connections.some((row) => row.source === "extract")).toBe(false);
  });

  it("does not invent a connection for an edge with an unknown endpoint", () => {
    const model = buildViewModel({
      ...graph,
      edges: [...graph.edges, { source: "ghost", target: "archive", conditional: true }],
    });
    expect(model.connections.some((row) => row.source === "ghost")).toBe(false);
  });
});

describe("search", () => {
  it("matches node ids and spans, not unrelated text", () => {
    expect(matchesQuery({ id: "judge_verify", span: "verify-verdict" }, "judge")).toBe(true);
    expect(matchesQuery({ id: "judge_verify", span: "verify-verdict" }, "VERDICT")).toBe(true);
    expect(matchesQuery({ id: "judge_verify", span: "verify-verdict" }, "archive")).toBe(false);
  });

  it("filters the connection list by endpoint, id and label", () => {
    const rows = buildViewModel(graph).connections;
    expect(filterConnections(rows, "retry").length).toBeGreaterThan(0);
    expect(filterConnections(rows, "retry").every((row) => row.target.includes("retry"))).toBe(
      true,
    );
    expect(filterConnections(rows, "missing")).toHaveLength(0);
  });
});
