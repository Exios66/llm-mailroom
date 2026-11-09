import { MarkerType, type Edge, type Node } from "@xyflow/react";

import type { ConnectionRow, GraphNode, GraphPayload } from "./types";

export const START_ID = "__start__";
export const END_ID = "__end__";
export const NODE_WIDTH = 196;
export const NODE_HEIGHT = 74;
const COLUMN_GAP = 236;
const ROW_GAP = 118;

export interface PipelineNodeData extends Record<string, unknown> {
  label: string;
  span: string;
  observationType: string;
  boundary: "start" | "end" | null;
  dimmed: boolean;
}

export type PipelineNode = Node<PipelineNodeData>;

export interface ViewModel {
  nodes: PipelineNode[];
  edges: Edge[];
  connections: ConnectionRow[];
  depths: Record<string, number>;
  nodeIndex: Record<string, GraphNode | undefined>;
}

/**
 * Convert the exported graph into a deterministic, layered view.
 *
 * Layout is derived from the wiring, never from a frontend inventory: the
 * depth of a node is its shortest distance from the declared start, and ties
 * are ordered by id. Self-loops and back edges are preserved as connections
 * but do not change the layered position, so cyclic retries stay visible.
 */
export function buildViewModel(graph: GraphPayload): ViewModel {
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const ids = new Set<string>([START_ID, END_ID, ...nodesById.keys()]);
  const adjacency = new Map<string, string[]>();
  for (const id of ids) adjacency.set(id, []);
  for (const edge of graph.edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
    adjacency.get(edge.source)?.push(edge.target);
  }
  for (const targets of adjacency.values()) targets.sort();

  const depth = new Map<string, number>([[START_ID, 0]]);
  const queue: string[] = [START_ID];
  while (queue.length > 0) {
    const current = queue.shift();
    if (current === undefined) break;
    const nextDepth = (depth.get(current) ?? 0) + 1;
    for (const target of adjacency.get(current) ?? []) {
      if (!depth.has(target)) {
        depth.set(target, nextDepth);
        queue.push(target);
      }
    }
  }
  const maxDepth = Math.max(0, ...depth.values());
  for (const id of [...ids].sort()) {
    if (!depth.has(id)) depth.set(id, maxDepth + 1);
  }

  const columns = new Map<number, string[]>();
  for (const id of [...ids].sort()) {
    const level = depth.get(id) ?? maxDepth + 1;
    const column = columns.get(level) ?? [];
    column.push(id);
    columns.set(level, column);
  }

  const nodes: PipelineNode[] = [];
  for (const [level, column] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    column.sort();
    column.forEach((id, index) => {
      const boundary = id === START_ID ? "start" : id === END_ID ? "end" : null;
      const graphNode = nodesById.get(id);
      const label = boundary ?? id;
      nodes.push({
        id,
        type: "pipeline",
        position: { x: level * COLUMN_GAP, y: index * ROW_GAP },
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        draggable: false,
        connectable: false,
        data: {
          label,
          span: graphNode?.span ?? "—",
          observationType: graphNode?.observation_type ?? "boundary",
          boundary,
          dimmed: false,
        },
        ariaLabel: `${label}${graphNode?.span ? `, ${graphNode.span}` : ""}`,
      });
    });
  }

  const counts = new Map<string, number>();
  const connections: ConnectionRow[] = [];
  const edges: Edge[] = graph.edges
    .filter((edge) => ids.has(edge.source) && ids.has(edge.target))
    .map((edge) => {
    const base = `${edge.source}->${edge.target}`;
    const count = (counts.get(base) ?? 0) + 1;
    counts.set(base, count);
    const id = `${base}#${count}`;
    const selfLoop = edge.source === edge.target;
    connections.push({
      id,
      source: edge.source,
      target: edge.target,
      label: edge.label ?? null,
      conditional: edge.conditional,
      selfLoop,
    });
    return {
      id,
      source: edge.source,
      target: edge.target,
      type: "smoothstep",
      label: edge.label ?? undefined,
      className: edge.conditional ? "edge--conditional" : "edge--fixed",
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      data: { conditional: edge.conditional, selfLoop },
    };
  });
  connections.sort(
    (a, b) =>
      a.source.localeCompare(b.source) ||
      a.target.localeCompare(b.target) ||
      a.id.localeCompare(b.id),
  );

  return {
    nodes,
    edges,
    connections,
    depths: Object.fromEntries(depth),
    nodeIndex: Object.fromEntries(nodesById),
  };
}

export function matchesQuery(
  node: { id: string; span?: string | null },
  query: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    node.id.toLowerCase().includes(needle) ||
    (node.span ?? "").toLowerCase().includes(needle)
  );
}

export function edgeMatchesQuery(
  edge: { id: string; source: string; target: string; label?: string | null },
  matchedNodeIds: Set<string>,
  query: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    matchedNodeIds.has(edge.source) ||
    matchedNodeIds.has(edge.target) ||
    edge.id.toLowerCase().includes(needle) ||
    (edge.label ?? "").toLowerCase().includes(needle)
  );
}

export function filterConnections(rows: ConnectionRow[], query: string): ConnectionRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter(
    (row) =>
      row.source.toLowerCase().includes(needle) ||
      row.target.toLowerCase().includes(needle) ||
      row.id.toLowerCase().includes(needle) ||
      (row.label ?? "").toLowerCase().includes(needle),
  );
}
