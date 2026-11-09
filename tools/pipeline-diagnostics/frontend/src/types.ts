export interface GraphNode {
  id: string;
  span?: string | null;
  observation_type?: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  conditional: boolean;
  label?: string | null;
}

export interface GraphPayload {
  start: string;
  end: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Provenance {
  kind: string;
  active_worker: boolean;
  pipeline_version?: string | null;
  contract_version?: string | null;
  git_sha?: string | null;
  git_dirty?: boolean | null;
  artifact_sha256?: string | null;
  artifact_bytes?: number | null;
  tracing: string;
  config_source: string;
  hashes: Record<string, string>;
  missing_inputs: string[];
}

export interface Snapshot {
  schema_version: string;
  tool_version: string;
  provenance: Provenance;
  graph: GraphPayload;
  graph_hash: string;
  thresholds: Record<string, unknown>;
}

export type Severity = "error" | "warning" | "info";
export type ProofStatus = "confirmed" | "inconclusive" | "unavailable";

export interface Finding {
  id: string;
  rule: string;
  rule_version: string;
  category: string;
  severity: Severity;
  proof_status: ProofStatus;
  message: string;
  affected: Record<string, unknown>;
  witness: Record<string, unknown>;
}

export interface CheckReport {
  schema_version: string;
  tool_version: string;
  mode: string;
  provenance: Provenance;
  findings: Finding[];
  exit_code: number;
}

export interface ConnectionRow {
  id: string;
  source: string;
  target: string;
  label: string | null;
  conditional: boolean;
  selfLoop: boolean;
}
