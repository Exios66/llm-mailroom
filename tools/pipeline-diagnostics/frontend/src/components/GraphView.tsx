import { useCallback, useMemo } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react";

import { edgeMatchesQuery, matchesQuery, type ViewModel } from "../model";
import { edgeKey, type RunOverlay } from "../runModel";
import type { Selection } from "../selection";
import { PipelineNodeCard } from "./NodeCard";

const nodeTypes = { pipeline: PipelineNodeCard };

interface GraphViewProps {
  model: ViewModel;
  selection: Selection | null;
  query: string;
  reducedMotion: boolean;
  runOverlay: RunOverlay | null;
  onSelect: (selection: Selection | null) => void;
}

export function GraphView({
  model,
  selection,
  query,
  reducedMotion,
  runOverlay,
  onSelect,
}: GraphViewProps) {
  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === "select" && change.selected) {
          onSelect({ kind: "node", id: change.id });
        }
      }
    },
    [onSelect],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      for (const change of changes) {
        if (change.type === "select" && change.selected) {
          onSelect({ kind: "edge", id: change.id });
        }
      }
    },
    [onSelect],
  );

  const decorated = useMemo(() => {
    const related = new Set<string>();
    if (selection?.kind === "node") {
      for (const edge of model.edges) {
        if (edge.source === selection.id || edge.target === selection.id) {
          related.add(edge.id);
        }
      }
    }
    const matchedNodes = new Set(
      model.nodes
        .filter((node) => matchesQuery({ id: node.id, span: node.data.span }, query))
        .map((node) => node.id),
    );
    const currentNode =
      runOverlay && runOverlay.currentIndex !== null
        ? runOverlay.sequence[runOverlay.currentIndex]
        : null;
    const nodes: Node[] = model.nodes.map((node) => ({
      ...node,
      selected: selection?.kind === "node" && selection.id === node.id,
      data: {
        ...node.data,
        dimmed: query.trim().length > 0 && !matchesQuery({ id: node.id, span: node.data.span }, query),
        runOrder: runOverlay?.visitOrder[node.id],
        runCurrent: currentNode === node.id,
      },
    }));
    const edges: Edge[] = model.edges.map((edge) => {
      const isSelected = selection?.kind === "edge" && selection.id === edge.id;
      const isRelated = related.has(edge.id);
      const dimmed = !edgeMatchesQuery(
        { id: edge.id, source: edge.source, target: edge.target, label: edge.label as string | undefined },
        matchedNodes,
        query,
      );
      const key = edgeKey(edge.source, edge.target);
      const runVisited = runOverlay?.edgeKeys.has(key) ?? false;
      const runCurrent = runOverlay?.currentEdgeKey === key;
      const classes = [
        edge.className,
        runVisited ? "edge--run" : "",
        runCurrent ? "edge--run-current" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return {
        ...edge,
        selected: isSelected,
        className: classes,
        animated: !reducedMotion && (isSelected || isRelated || runCurrent),
        style: { opacity: dimmed ? 0.18 : isRelated || isSelected || runVisited ? 1 : 0.75 },
      };
    });
    return { nodes, edges };
  }, [model, selection, query, reducedMotion, runOverlay]);

  return (
    <div
      className="graph"
      aria-label="Pipeline connection graph. The connection list is the keyboard-readable equivalent."
    >
      <ReactFlow
        nodes={decorated.nodes}
        edges={decorated.edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => onSelect({ kind: "node", id: node.id })}
        onEdgeClick={(_event, edge) => onSelect({ kind: "edge", id: edge.id })}
        onPaneClick={() => onSelect(null)}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        nodesFocusable
        edgesFocusable={false}
        fitView
        fitViewOptions={{ padding: 0.06, minZoom: 0.72, maxZoom: 1.15 }}
        minZoom={0.2}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={24} size={1} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable ariaLabel="Pipeline graph minimap" />
      </ReactFlow>
    </div>
  );
}
