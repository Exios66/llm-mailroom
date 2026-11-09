import { Handle, Position, type NodeProps } from "@xyflow/react";

import type { PipelineNodeData } from "../model";

export function PipelineNodeCard({ data, selected }: NodeProps) {
  const node = data as PipelineNodeData;
  const classes = [
    "node-card",
    node.boundary ? `is-${node.boundary}` : "",
    !node.boundary && node.observationType === "agent" ? "is-agent" : "",
    node.dimmed ? "is-dimmed" : "",
    node.runOrder ? "is-run-visited" : "",
    node.runCurrent ? "is-run-current" : "",
    selected ? "is-selected" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes} title={`${node.label} — ${node.span}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      {node.runOrder ? (
        <span className="node-card__run" aria-label={`Run step ${node.runOrder}`}>
          {node.runOrder}
        </span>
      ) : null}
      <span className="node-card__type">{node.observationType}</span>
      <strong className="node-card__label">{node.label}</strong>
      <span className="node-card__span">{node.span}</span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
