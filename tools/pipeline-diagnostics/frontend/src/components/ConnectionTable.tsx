import { filterConnections, type ViewModel } from "../model";
import type { Selection } from "../selection";

interface ConnectionTableProps {
  model: ViewModel;
  query: string;
  selection: Selection | null;
  onSelect: (selection: Selection | null) => void;
}

export function ConnectionTable({ model, query, selection, onSelect }: ConnectionTableProps) {
  const rows = filterConnections(model.connections, query);
  return (
    <section className="table-wrap" id="connections" aria-label="All connections" tabIndex={-1}>
      <table>
        <caption>
          All {model.connections.length} connections.{" "}
          {query.trim() ? `${rows.length} match the filter.` : ""}
        </caption>
        <thead>
          <tr>
            <th scope="col">From</th>
            <th scope="col">To</th>
            <th scope="col">Kind</th>
            <th scope="col">Label</th>
            <th scope="col">Stable id</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = selection?.kind === "edge" && selection.id === row.id;
            return (
              <tr key={row.id} className={selected ? "is-selected" : undefined}>
                <td data-label="From">
                  <button
                    className="link-button"
                    onClick={() => onSelect({ kind: "edge", id: row.id })}
                    aria-pressed={selected}
                  >
                    {row.source}
                  </button>
                </td>
                <td data-label="To">{row.target}</td>
                <td data-label="Kind">{row.conditional ? "conditional" : "fixed"}</td>
                <td data-label="Label">{row.label ?? "—"}</td>
                <td data-label="Stable id">
                  <code>{row.id}</code>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 ? <p className="muted">No connections match this filter.</p> : null}
    </section>
  );
}
