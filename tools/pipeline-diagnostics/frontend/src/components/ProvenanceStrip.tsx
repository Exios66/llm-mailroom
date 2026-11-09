import type { Snapshot } from "../types";

export type SourceStatus = "loading" | "ready" | "error";

interface ProvenanceStripProps {
  snapshot: Snapshot | null;
  status: SourceStatus;
  updatedAt: Date | null;
}

export function ProvenanceStrip({ snapshot, status, updatedAt }: ProvenanceStripProps) {
  if (!snapshot) {
    return (
      <section className="provenance" aria-label="Snapshot provenance">
        <span className={`chip chip--${status}`}>{status === "loading" ? "loading" : "no snapshot"}</span>
        <span className="muted">Waiting for the diagnostics API.</span>
      </section>
    );
  }

  const provenance = snapshot.provenance;
  const hashes = Object.entries(provenance.hashes).sort(([a], [b]) => a.localeCompare(b));
  return (
    <section className="provenance" aria-label="Snapshot provenance">
      <div className="provenance__chips">
        <span className={`chip chip--${status}`}>{status === "error" ? "disconnected" : status}</span>
        <span className="chip">{provenance.kind}</span>
        <span className="chip">source: {provenance.config_source}</span>
        <span className="chip">worker: {provenance.active_worker ? "active" : "preview only"}</span>
        <span className="chip">tracing: {provenance.tracing}</span>
        {provenance.git_dirty ? <span className="chip chip--warn">dirty tree</span> : null}
        {provenance.missing_inputs.length > 0 ? (
          <span className="chip chip--warn">{provenance.missing_inputs.length} missing inputs</span>
        ) : null}
      </div>
      <dl className="provenance__meta">
        <div>
          <dt>Revision</dt>
          <dd>
            <code>{provenance.git_sha ? provenance.git_sha.slice(0, 10) : "unknown"}</code>
          </dd>
        </div>
        <div>
          <dt>Graph hash</dt>
          <dd title={snapshot.graph_hash}>
            <code>{snapshot.graph_hash.replace("sha256:", "").slice(0, 12)}</code>
          </dd>
        </div>
        <div>
          <dt>Pipeline</dt>
          <dd>{provenance.pipeline_version ?? "unknown"}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{updatedAt ? updatedAt.toLocaleTimeString() : "not yet"}</dd>
        </div>
      </dl>
      <details className="provenance__details">
        <summary>Hashes and revisions</summary>
        <dl>
          {hashes.map(([group, value]) => (
            <div key={group}>
              <dt>{group}</dt>
              <dd>
                <code title={value}>{value.replace("sha256:", "").slice(0, 16)}</code>
              </dd>
            </div>
          ))}
        </dl>
        {provenance.missing_inputs.length > 0 ? (
          <p className="caveat">
            Missing inputs: {provenance.missing_inputs.join(", ")}. Hashes are computed from the
            paths that exist; absent seams are named rather than guessed.
          </p>
        ) : null}
        <p className="caveat">
          A hash detects a moved seam. It does not prove routing, guard or model quality, and a
          preview never impersonates a running worker.
        </p>
      </details>
    </section>
  );
}
