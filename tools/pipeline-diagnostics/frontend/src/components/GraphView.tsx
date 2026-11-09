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

import { matchesQuery, type ViewModel } from "../model";
import type { Selection } from "../selection";
import { PipelineNodeCard } from "./NodeCard";

const nodeTypes = { pipeline: PipelineNodeCard };

interface GraphViewProps {
  model: ViewModel;
  selection: Selection | null;
  query: string;
  reducedMotion: boolean;
  onSelect: (selection: Selection | null) => void;
}

export function GraphView({
  model,
  selection,
  query,
  reducedMotion,
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
    const nodes: Node[] = model.nodes.map((node) => ({
      ...node,
      selected: selection?.kind === "node" && selection.id === node.id,
      data: {
        ...node.data,
        dimmed: query.trim().length > 0 && !matchesQuery({ id: node.id, span: node.data.span }, query),
      },
    }));
    const edges: Edge[] = model.edges.map((edge) => {
      const isSelected = selection?.kind === "edge" && selection.id === edge.id;
      const isRelated = related.has(edge.id);
      const dimmed =
        query.trim().length > 0 &&
        !matchesQuery({ id: edge.source, span: null }, query) &&
        !matchesQuery({ id: edge.target, span: null }, query);
      return {
        ...edge,
        selected: isSelected,
        animated: !reducedMotion && (isSelected || isRelated),
        style: { opacity: dimmed ? 0.18 : isRelated || isSelected ? 1 : 0.75 },
      };
    });
    return { nodes, edges };
  }, [model, selection, query, reducedMotion]);

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
