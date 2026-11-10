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

export interface RunSummary {
  execution_id: string;
  document: {
    filename?: string | null;
    matter_id?: string | null;
    manifest_id?: string | null;
    source?: string | null;
  };
  outcome: {
    stage?: string | null;
    doc_type?: string | null;
    review: boolean;
    failed: boolean;
  };
  started_at: string | null;
  finished_at: string | null;
  node_count: number;
}

export interface RunEvent {
  type: "node_started" | "node_completed" | "node_errored";
  sequence: number;
  node: string;
  ts: number;
  duration_ms?: number;
  output?: Record<string, unknown>;
  error_class?: string;
}

export interface RunJournal {
  schema_version: string;
  execution_id: string;
  document: RunSummary["document"] & {
    doc_id?: string | null;
    content_sha256?: string | null;
    hub_revision?: string | null;
  };
  ground_truth?: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  events: RunEvent[];
  node_sequence: string[];
  transitions: Array<{ source: string; target: string }>;
  outcome: RunSummary["outcome"] & { run_aborted?: boolean; node_count?: number };
}

export interface ExperimentSummary {
  experiment_id: string;
  generated_at: string | null;
  mode: string | null;
  corpus: { name?: string | null; revision?: unknown };
  metrics: Record<string, unknown>;
  runs: number;
}

export interface ExperimentRun {
  execution_id: string;
  manifest_id?: string;
  filename?: string;
  expected?: string;
  predicted?: string | null;
  stage?: string | null;
  node_sequence?: string[];
  checks?: Record<string, boolean>;
  ok?: boolean;
}

export type ExperimentDetail = Omit<ExperimentSummary, "runs"> & {
  runs: ExperimentRun[];
};
