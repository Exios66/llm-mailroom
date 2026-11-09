import type { ViewModel } from "../model";
import type { Selection } from "../selection";

interface InspectorProps {
  selection: Selection | null;
  model: ViewModel;
  onSelect: (selection: Selection | null) => void;
}

export function Inspector({ selection, model, onSelect }: InspectorProps) {
  if (!selection) {
    return (
      <section className="panel inspector" aria-label="Selection inspector">
        <h2>Inspector</h2>
        <p className="muted">
          Select a node or connection. The connection list below the graph exposes the same
          information without a pointer.
        </p>
      </section>
    );
  }

  if (selection.kind === "node") {
    const node = model.nodes.find((candidate) => candidate.id === selection.id);
    if (!node) return null;
    const inbound = model.connections.filter((row) => row.target === node.id);
    const outbound = model.connections.filter((row) => row.source === node.id);
    return (
      <section className="panel inspector" aria-label="Selection inspector">
        <h2>{node.data.label}</h2>
        <dl className="facts">
          <dt>Node id</dt>
          <dd>
            <code>{node.id}</code>
          </dd>
          <dt>Span</dt>
          <dd>
            <code>{node.data.span}</code>
          </dd>
          <dt>Observation type</dt>
          <dd>{node.data.observationType}</dd>
          <dt>Depth from start</dt>
          <dd>{model.depths[node.id] ?? "unreachable"}</dd>
        </dl>
        <ConnectionList
          title={`Incoming (${inbound.length})`}
          rows={inbound}
          onSelect={onSelect}
          direction="from"
        />
        <ConnectionList
          title={`Outgoing (${outbound.length})`}
          rows={outbound}
          onSelect={onSelect}
          direction="to"
        />
        <p className="caveat">
          This slice exports wiring and stable ids, not implementation source links or routing
          predicates. A connection being present is not proof that its predicate is correct.
        </p>
      </section>
    );
  }

  const edge = model.connections.find((candidate) => candidate.id === selection.id);
  if (!edge) return null;
  return (
    <section className="panel inspector" aria-label="Selection inspector">
      <h2>Connection</h2>
      <dl className="facts">
        <dt>Edge id</dt>
        <dd>
          <code>{edge.id}</code>
        </dd>
        <dt>From</dt>
        <dd>
          <button className="link-button" onClick={() => onSelect({ kind: "node", id: edge.source })}>
            {edge.source}
          </button>
        </dd>
        <dt>To</dt>
        <dd>
          <button className="link-button" onClick={() => onSelect({ kind: "node", id: edge.target })}>
            {edge.target}
          </button>
        </dd>
        <dt>Kind</dt>
        <dd>{edge.conditional ? "conditional" : "fixed"}</dd>
        <dt>Route label</dt>
        <dd>{edge.label ?? "not exported"}</dd>
        <dt>Self-loop</dt>
        <dd>{edge.selfLoop ? "yes (retry path)" : "no"}</dd>
      </dl>
      <p className="caveat">
        Conditional labels are mostly absent in the generated contract. An unavailable predicate is
        reported as unavailable, never guessed from the destination.
      </p>
    </section>
  );
}

function ConnectionList({
  title,
  rows,
  onSelect,
  direction,
}: {
  title: string;
  rows: ViewModel["connections"];
  onSelect: (selection: Selection | null) => void;
  direction: "from" | "to";
}) {
  return (
    <div className="inspector__group">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="muted">None.</p>
      ) : (
        <ul className="connection-links">
          {rows.map((row) => (
            <li key={row.id}>
              <button className="link-button" onClick={() => onSelect({ kind: "edge", id: row.id })}>
                {direction === "from" ? row.source : row.target}
                {row.selfLoop ? " (self-loop)" : ""}
                {row.label ? ` — ${row.label}` : ""}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
