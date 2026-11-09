import { useCallback, useEffect, useMemo, useState } from "react";

import { DiagnosticsApiError, fetchFindings, fetchSnapshot, readToken, writeToken } from "./api";
import { ConnectionTable } from "./components/ConnectionTable";
import { FindingsPanel } from "./components/FindingsPanel";
import { GraphView } from "./components/GraphView";
import { Inspector } from "./components/Inspector";
import { ProvenanceStrip, type SourceStatus } from "./components/ProvenanceStrip";
import { buildViewModel } from "./model";
import type { Selection } from "./selection";
import type { CheckReport, Snapshot } from "./types";
import { useReducedMotion } from "./useReducedMotion";

type View = "graph" | "list";

function initialView(): View {
  if (typeof window === "undefined") return "graph";
  const requested = new URLSearchParams(window.location.search).get("view");
  if (requested === "graph" || requested === "list") return requested;
  return window.matchMedia?.("(max-width: 720px)").matches ? "list" : "graph";
}

function initialSelection(): Selection | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const node = params.get("node");
  if (node) return { kind: "node", id: node };
  const edge = params.get("edge");
  if (edge) return { kind: "edge", id: edge };
  return null;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [report, setReport] = useState<CheckReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>(initialView);
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<Selection | null>(initialSelection);
  const [announcement, setAnnouncement] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [token, setToken] = useState(readToken);
  const reducedMotion = useReducedMotion();

  const model = useMemo(() => (snapshot ? buildViewModel(snapshot.graph) : null), [snapshot]);

  const load = useCallback(async (signal: AbortSignal, quiet: boolean) => {
    if (!quiet) setLoading(true);
    try {
      const [nextSnapshot, nextReport] = await Promise.all([
        fetchSnapshot(signal),
        fetchFindings(signal),
      ]);
      if (signal.aborted) return;
      setSnapshot(nextSnapshot);
      setReport(nextReport);
      setUpdatedAt(new Date());
      setError(null);
      setAnnouncement(
        quiet
          ? `Updated: ${nextSnapshot.graph.nodes.length} nodes and ${nextSnapshot.graph.edges.length} connections.`
          : `Loaded workspace preview with ${nextSnapshot.graph.nodes.length} nodes and ${nextSnapshot.graph.edges.length} connections.`,
      );
    } catch (caught) {
      if (signal.aborted) return;
      const message =
        caught instanceof DiagnosticsApiError || caught instanceof Error
          ? caught.message
          : "Unable to load diagnostics.";
      setError(message);
      setAnnouncement("Diagnostics could not be refreshed.");
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal, false);
    const timer = window.setInterval(() => void load(controller.signal, true), 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    if (!model || !selection) return;
    const exists =
      selection.kind === "node"
        ? model.nodes.some((node) => node.id === selection.id)
        : model.connections.some((row) => row.id === selection.id);
    if (!exists) {
      setSelection(null);
      setAnnouncement("The selected item is no longer present after the latest update.");
    }
  }, [model, selection]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.delete("node");
    params.delete("edge");
    if (selection?.kind === "node") params.set("node", selection.id);
    if (selection?.kind === "edge") params.set("edge", selection.id);
    const queryString = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${queryString ? `?${queryString}` : ""}${window.location.hash}`,
    );
  }, [selection]);

  const status: SourceStatus = error ? "error" : loading && !snapshot ? "loading" : "ready";

  const applyToken = useCallback(() => {
    writeToken(token.trim());
    setAnnouncement(token.trim() ? "Bearer token updated. Reloading diagnostics." : "Bearer token cleared. Reloading diagnostics.");
    void load(new AbortController().signal, false);
  }, [load, token]);

  return (
    <div className="app">
      <a className="skip-link" href="#connections">
        Skip to connection list
      </a>

      <header className="app-header">
        <div>
          <p className="eyebrow">llm-mailroom · native diagnostics</p>
          <h1>Pipeline graph</h1>
          <p className="lede">
            Read-only view of the checked-out LangGraph wiring. The screen renders the exported
            snapshot; it does not keep a second node or edge inventory.
          </p>
        </div>
        <div className="app-header__actions">
          <button className="button" type="button" onClick={() => void load(new AbortController().signal, false)} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <details className="auth">
            <summary>Authentication</summary>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                applyToken();
              }}
            >
              <label htmlFor="token">Bearer token (this tab only)</label>
              <input
                id="token"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
              <button className="button" type="submit">
                Apply and reload
              </button>
            </form>
            <p className="caveat">
              Loopback without a token needs no header. Off loopback the producer requires
              <code>MAILROOM_API_TOKEN</code> or <code>MAILROOM_API_TOKENS</code>.
            </p>
          </details>
        </div>
      </header>

      <ProvenanceStrip snapshot={snapshot} status={status} updatedAt={updatedAt} />

      <section className="toolbar" aria-label="View controls">
        <div className="field">
          <label htmlFor="filter">Filter nodes and connections</label>
          <input
            id="filter"
            type="search"
            placeholder="intake, judge_verify, failed…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="segmented" role="group" aria-label="Layout">
          <button
            type="button"
            aria-pressed={view === "graph"}
            onClick={() => setView("graph")}
          >
            Graph
          </button>
          <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")}>
            Connection list
          </button>
        </div>
      </section>

      <div role="status" className="sr-only">
        {announcement}
      </div>

      {error ? (
        <div className="error-banner" role="alert">
          <strong>Diagnostics unavailable.</strong> {error}
        </div>
      ) : null}

      {snapshot && model ? (
        <>
          <main className="workbench" aria-busy={loading}>
            <div className="canvas-column">
              {view === "graph" ? (
                <GraphView
                  model={model}
                  selection={selection}
                  query={query}
                  reducedMotion={reducedMotion}
                  onSelect={setSelection}
                />
              ) : (
                <ConnectionTable
                  model={model}
                  query={query}
                  selection={selection}
                  onSelect={setSelection}
                />
              )}
            </div>
            <aside className="side-column" aria-label="Diagnostics details">
              <Inspector selection={selection} model={model} onSelect={setSelection} />
              <FindingsPanel report={report} />
            </aside>
          </main>
          {view === "graph" ? (
            <section className="list-section" aria-labelledby="connections-heading">
              <h2 id="connections-heading">Connection list</h2>
              <p className="muted">
                The same connections as the graph, in reading order. Use this list for keyboard or
                screen-reader inspection.
              </p>
              <ConnectionTable
                model={model}
                query={query}
                selection={selection}
                onSelect={setSelection}
              />
            </section>
          ) : null}
        </>
      ) : (
        <main className="empty-state">
          <p>{loading ? "Loading workspace preview…" : "No snapshot loaded."}</p>
        </main>
      )}

      <footer className="app-footer">
        <p>
          Read-only workspace preview. Opening this screen starts no watcher, document processing,
          model call or hosted tracing. Exit 0 from the structural checks is not a safety
          certificate.
        </p>
      </footer>
    </div>
  );
}
