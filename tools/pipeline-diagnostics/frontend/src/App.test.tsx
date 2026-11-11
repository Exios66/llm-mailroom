import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type {
  CheckReport,
  ExperimentDetail,
  ExperimentSummary,
  RunJournal,
  RunSummary,
  Snapshot,
} from "./types";

const provenance: Snapshot["provenance"] = {
  kind: "workspace-preview",
  active_worker: false,
  pipeline_version: "0.9.3",
  contract_version: null,
  git_sha: "abcdef1234567890abcdef1234567890abcdef12",
  git_dirty: false,
  artifact_sha256: null,
  artifact_bytes: null,
  tracing: "disabled",
  config_source: "workspace-files",
  hashes: {
    contract: "sha256:1111111111111111",
    effective_policy: "sha256:2222222222222222",
    implementation: "sha256:3333333333333333",
    routing: "sha256:4444444444444444",
    taxonomy: "sha256:5555555555555555",
    topology: "sha256:6666666666666666",
  },
  missing_inputs: [],
};

const snapshot: Snapshot = {
  schema_version: "1.0.0",
  tool_version: "0.1.0",
  provenance,
  graph: {
    start: "__start__",
    end: "__end__",
    nodes: [
      { id: "intake", span: "ingest-and-prepare", observation_type: "agent" },
      { id: "classify", span: "classify-document", observation_type: "agent" },
      { id: "archive", span: "archive-document", observation_type: "span" },
    ],
    edges: [
      { source: "__start__", target: "intake", conditional: true, label: null },
      { source: "intake", target: "classify", conditional: false, label: null },
      { source: "classify", target: "archive", conditional: true, label: "failed" },
      { source: "archive", target: "__end__", conditional: false, label: null },
    ],
  },
  graph_hash: "sha256:deadbeefdeadbeefdeadbeef",
  thresholds: {},
};

const report: CheckReport = {
  schema_version: "1.0.0",
  tool_version: "0.1.0",
  mode: "check",
  provenance,
  findings: [],
  exit_code: 0,
};

const runSummary: RunSummary = {
  execution_id: "run-1",
  document: { doc_id: "DOC-1", filename: "real-doc.txt", manifest_id: "DOC-1", source: "live-manifest" },
  outcome: { stage: "archived", doc_type: "contract", review: false, failed: false },
  started_at: "2026-11-09T05:00:00+00:00",
  finished_at: "2026-11-09T05:00:01+00:00",
  node_count: 3,
};

const runJournal: RunJournal = {
  schema_version: "1.0.0",
  execution_id: "run-1",
  document: { doc_id: "DOC-1", filename: "real-doc.txt", manifest_id: "DOC-1", source: "live-manifest" },
  ground_truth: { expected_doc_class: "contract", expected_stage: "archived" },
  started_at: "2026-11-09T05:00:00+00:00",
  finished_at: "2026-11-09T05:00:01+00:00",
  events: [],
  node_sequence: ["intake", "classify", "archive"],
  transitions: [
    { source: "intake", target: "classify" },
    { source: "classify", target: "archive" },
  ],
  outcome: { stage: "archived", doc_type: "contract", review: false, failed: false },
};

const experimentSummary: ExperimentSummary = {
  experiment_id: "exp-1",
  generated_at: "2026-11-09T05:00:00+00:00",
  mode: "mock",
  corpus: { name: "Lucius-Morningstar/mailroom-dataset", revision: { tag: "v9.3" } },
  metrics: { n: 1, class_ok: 1, stage_ok: 1, all_checks_ok: true },
  runs: 1,
};

const experimentDetail: ExperimentDetail = {
  ...experimentSummary,
  runs: [
    {
      execution_id: "run-1",
      manifest_id: "DOC-1",
      expected: "contract",
      predicted: "contract",
      stage: "archived",
      node_sequence: ["intake", "classify", "archive"],
      checks: {},
      ok: true,
    },
  ],
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

type Override = (url: string) => Response | Promise<Response> | undefined;

function errorResponse(status: number): Response {
  return { ok: false, status, json: async () => ({}) } as Response;
}

function auditPayload(docId: string, status = "verified", truncated = false) {
  return {
    schema_version: "1.0.0", read_only: true, doc_id: docId,
    store: { state: "available", schema: "unified-row", engine: "sqlite" },
    record: { state: "unified-row", entry_id: `row-${docId}`, event: "archived", actor: "archivist", timestamp: "2026-11-09T05:00:01Z", seq: 3, prev_hash: "sha256:previous", entry_hash: `sha256:${docId}`, terminal: true, attempts: [{ attempt_id: `attempt-${docId}`, stage: "extraction", attempt_no: 1, outcome: "ok", started_at: "2026-11-09T05:00:00Z", entry_hash: "sha256:attempt" }], attempts_total: 1, addenda: [{ addendum_id: `addendum-${docId}`, kind: "rescore", actor: "reviewer", timestamp: "2026-11-09T05:00:02Z", entry_hash: "sha256:addendum", attempts: [{ attempt_id: `addendum-attempt-${docId}`, stage: "review", attempt_no: 1, outcome: "ok", started_at: "2026-11-09T05:00:02Z", entry_hash: "sha256:addendum-attempt" }] }] },
    legacy: { state: "absent", count: 0, truncated: false, entries: [] },
    verification: { status, method: "audit.writer", checked: { main: 1, attempts: 1, addenda: 1, legacy: 0 }, failures: status === "failed" ? [{ scope: "attempts", reason: "hash-mismatch" }] : [], truncated, notes: [] },
    limits: { events: 2000, attempts: 500, addenda: 200, decision_fields: 40 }, notes: [],
  };
}

function mockApi(override?: Override) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    const url = String(input);
    const overridden = override?.(url);
    if (overridden) return overridden;
    if (url.includes("/v1/diagnostics/audit/documents?")) return jsonResponse({
      schema_version: "1.0.0", read_only: true,
      store: { state: "available", schema: "event-chain", engine: "sqlite" },
      pagination: { limit: 25, offset: 0, returned: 1, more: false },
      documents: [{ doc_id: "DOC-1", matter_id: "matter-1", event: "archived", actor: "archivist", timestamp: "2026-11-09T05:00:01Z", seq: null, entries: 3, source: "event-chain" }], notes: [],
    });
    if (url.includes("/v1/diagnostics/audit/documents/")) {
      const docId = decodeURIComponent(url.split("/v1/diagnostics/audit/documents/")[1]);
      return jsonResponse(auditPayload(docId));
    }
    if (url.includes("/snapshot")) return jsonResponse(snapshot);
    if (url.includes("/findings")) return jsonResponse(report);
    if (url.endsWith("/v1/diagnostics/runs")) return jsonResponse({ runs: [runSummary] });
    if (url.includes("/v1/diagnostics/runs/run-1")) return jsonResponse(runJournal);
    if (url.endsWith("/v1/diagnostics/experiments")) {
      return jsonResponse({ experiments: [experimentSummary] });
    }
    if (url.includes("/v1/diagnostics/experiments/exp-1")) return jsonResponse(experimentDetail);
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  window.history.replaceState({}, "", "/?view=list");
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("renders the connection list and provenance without a graph inventory", async () => {
    mockApi();
    render(<App />);

    expect(await screen.findByText(/All 4 connections/)).toBeInTheDocument();
    expect(screen.getByText("source: workspace-files")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(screen.getByRole("heading", { name: /Findings/ })).toBeInTheDocument();
  });

  it("selects a connection and shows its stable id in the inspector", async () => {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.click(screen.getByRole("button", { name: "__start__" }));

    const inspector = screen.getByRole("region", { name: "Selection inspector" });
    expect(within(inspector).getByText("__start__->intake#1")).toBeInTheDocument();
    expect(within(inspector).getByText("conditional")).toBeInTheDocument();
    expect(window.location.search).toContain("edge=");
  });

  it("filters the connection list and preserves the caption count", async () => {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.type(screen.getByLabelText("Filter nodes and connections"), "archive");
    expect(await screen.findByText(/2 match the filter/)).toBeInTheDocument();
  });

  it("restores a deep-linked node selection", async () => {
    window.history.replaceState({}, "", "/?view=list&node=classify");
    mockApi();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "classify" })).toBeInTheDocument();
    expect(screen.getByText("classify-document")).toBeInTheDocument();
  });

  it("loads a recorded run and replays its node path", async () => {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    const path = await screen.findByRole("list", { name: "Recorded node path" });
    expect(within(path).getAllByRole("button")).toHaveLength(3);

    await user.click(within(path).getByRole("button", { name: /classify/ }));
    expect(screen.getByRole("heading", { name: "classify" })).toBeInTheDocument();
    expect(window.location.search).toContain("node=classify");
  });

  it("follows the selected run document into its verified audit record", async () => {
    const fetchMock = mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    expect(await screen.findByText("Verification verified")).toBeInTheDocument();
    expect(screen.getByText("row-DOC-1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Row attempts (1)" })).toBeInTheDocument();
    expect(screen.getAllByText("ID: attempt-DOC-1")).toHaveLength(1);
    expect(screen.getAllByText("ID: addendum-attempt-DOC-1")).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/audit/documents/DOC-1"))).toBe(true);
  });

  it("refreshes the active audit on global refresh and audit-list refresh without changing its document id", async () => {
    const currentEvent = { value: "archived" };
    const fetchMock = mockApi((url) => {
      if (!url.endsWith("/audit/documents/DOC-1")) return undefined;
      const base = auditPayload("DOC-1");
      return jsonResponse({ ...base, record: { ...base.record, event: currentEvent.value } });
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    const panel = screen.getByRole("region", { name: "Audit record" });
    expect(await within(panel).findByText("archived")).toBeInTheDocument();
    currentEvent.value = "review_requeued";
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await within(panel).findByText("review_requeued")).toBeInTheDocument();
    currentEvent.value = "triage_archived";
    await user.click(screen.getByRole("button", { name: "Refresh audit and list" }));
    expect(await within(panel).findByText("triage_archived")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/audit/documents/DOC-1"))).toHaveLength(3);
  });

  it("coalesces repeated refresh ticks while a slow audit read is in flight so it can still land", async () => {
    // The 15s poll and the Refresh button both bump refreshKey. A read slower
    // than the poll interval must not be aborted forever by the next tick.
    let resolveSlow!: (response: Response) => void;
    const slow = new Promise<Response>((done) => { resolveSlow = done; });
    let auditCalls = 0;
    mockApi((url) => {
      if (!url.endsWith("/audit/documents/DOC-1")) return undefined;
      auditCalls += 1;
      if (auditCalls === 1) return slow;
      return new Promise<Response>(() => {});
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    await screen.findByText(/Loading audit for DOC-1/);
    expect(auditCalls).toBe(1);

    // Two more refresh ticks while the first read is still pending: they are
    // coalesced onto it instead of aborting it and starting fresh requests.
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(auditCalls).toBe(1);
    // The coalesced ticks still report the read as in flight rather than
    // blanking the panel until the response arrives.
    expect(screen.getByText(/Loading audit for DOC-1/)).toBeInTheDocument();

    await act(async () => resolveSlow(jsonResponse(auditPayload("DOC-1"))));
    expect(await screen.findByText("row-DOC-1")).toBeInTheDocument();
    expect(auditCalls).toBe(1);
  });

  it("keeps manual document lookup independent from the followed run", async () => {
    const fetchMock = mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.click(screen.getByLabelText("Follow selected run document"));
    await user.selectOptions(screen.getByLabelText("Choose a listed document"), "DOC-1");
    expect(await screen.findByText("row-DOC-1")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Look up document id"), "MANUAL-7");
    await user.click(screen.getByRole("button", { name: "Look up" }));
    expect(await screen.findByText("row-MANUAL-7")).toBeInTheDocument();
    expect(screen.getByLabelText("Follow selected run document")).not.toBeChecked();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/audit/documents/MANUAL-7"))).toBe(true);
  });

  it("pages the audit document selector and preserves a manual lookup across pages", async () => {
    const fetchMock = mockApi((url) => {
      if (!url.includes("/v1/diagnostics/audit/documents?")) return undefined;
      const offset = Number(new URL(url, window.location.origin).searchParams.get("offset") ?? 0);
      const documents = offset === 0
        ? Array.from({ length: 25 }, (_, index) => ({ doc_id: `DOC-${index + 1}`, event: "archived" }))
        : [{ doc_id: "DOC-26", event: "archived" }];
      return jsonResponse({
        schema_version: "1.0.0", read_only: true, store: { state: "available", schema: "event-chain" },
        pagination: { limit: 25, offset, returned: documents.length, more: offset === 0 }, documents, notes: [],
      });
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.click(screen.getByLabelText("Follow selected run document"));
    const select = screen.getByLabelText("Choose a listed document");
    await waitFor(() => expect(within(select).getByRole("option", { name: "DOC-1 — archived" })).toBeInTheDocument());
    await user.selectOptions(select, "DOC-1");
    expect(await screen.findByText("row-DOC-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(within(select).getByRole("option", { name: "DOC-26 — archived" })).toBeInTheDocument());
    expect(select).toHaveValue("DOC-1");
    await user.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => expect(within(select).getByRole("option", { name: "DOC-1 — archived" })).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("offset=25"))).toBe(true);
  });

  it("loads an experiment log and jumps to one of its runs", async () => {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.selectOptions(screen.getByLabelText("Load an experiment log"), "exp-1");
    expect(await screen.findByText(/Class \/ stage ok/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /DOC-1/ }));
    expect(await screen.findByRole("list", { name: "Recorded node path" })).toBeInTheDocument();
    expect(screen.getByLabelText("Load a run journal")).toHaveValue("run-1");
  });

  it("starts and stops recorded-path playback", async () => {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    await screen.findByRole("list", { name: "Recorded node path" });
    await user.click(screen.getByRole("button", { name: "Play path" }));
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByRole("button", { name: "Play path" })).toBeInTheDocument();
  });

  it("sends the stored bearer token on diagnostics requests", async () => {
    sessionStorage.setItem("mailroom-diagnostics-token", "tab-token");
    const fetchMock = mockApi();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tab-token");
  });
});

describe("recorded run playback", () => {
  async function loadRun() {
    mockApi();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    const path = await screen.findByRole("list", { name: "Recorded node path" });
    return within(path).getAllByRole("button");
  }

  const currentIndex = (steps: HTMLElement[]) =>
    steps.findIndex((step) => step.getAttribute("aria-current") === "step");

  it("advances one node per interval and stops after the last node", async () => {
    const steps = await loadRun();
    expect(currentIndex(steps)).toBe(-1);
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("button", { name: "Play path" }));
    expect(currentIndex(steps)).toBe(0);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    act(() => void vi.advanceTimersByTime(899));
    expect(currentIndex(steps)).toBe(0);
    act(() => void vi.advanceTimersByTime(1));
    expect(currentIndex(steps)).toBe(1);
    act(() => void vi.advanceTimersByTime(900));
    expect(currentIndex(steps)).toBe(2);

    // Completion: the last node stays current and playback returns to "Play path".
    expect(screen.getByRole("button", { name: "Play path" })).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(5000));
    expect(currentIndex(steps)).toBe(2);
  });

  it("Stop halts advancement where it is", async () => {
    const steps = await loadRun();
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("button", { name: "Play path" }));
    act(() => void vi.advanceTimersByTime(900));
    expect(currentIndex(steps)).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByRole("button", { name: "Play path" })).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(10_000));
    expect(currentIndex(steps)).toBe(1);
  });

  it("Show all clears the current step and stops playback", async () => {
    const steps = await loadRun();
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("button", { name: "Play path" }));
    act(() => void vi.advanceTimersByTime(900));
    expect(currentIndex(steps)).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(currentIndex(steps)).toBe(-1);
    expect(screen.getByRole("button", { name: "Play path" })).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(10_000));
    expect(currentIndex(steps)).toBe(-1);
  });

  it("replays from the start when played again after completion", async () => {
    const steps = await loadRun();
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("button", { name: "Play path" }));
    act(() => void vi.advanceTimersByTime(900));
    act(() => void vi.advanceTimersByTime(900));
    expect(currentIndex(steps)).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Play path" }));
    expect(currentIndex(steps)).toBe(0);
  });
});

describe("detail requests and partial failures", () => {
  const secondRun: RunSummary = {
    ...runSummary,
    execution_id: "run-2",
    document: { doc_id: "DOC-2", filename: "other.txt", manifest_id: "DOC-2", source: "live-manifest" },
  };
  const secondJournal: RunJournal = {
    ...runJournal,
    execution_id: "run-2",
    document: { doc_id: "DOC-2", filename: "other.txt", manifest_id: "DOC-2", source: "live-manifest" },
    node_sequence: ["intake", "classify"],
  };

  function deferred() {
    let resolve!: (response: Response) => void;
    const promise = new Promise<Response>((done) => {
      resolve = done;
    });
    return { promise, resolve };
  }

  it("keeps the latest selection when an older run request finishes last", async () => {
    const first = deferred();
    const second = deferred();
    mockApi((url) => {
      if (url.endsWith("/v1/diagnostics/runs")) return jsonResponse({ runs: [runSummary, secondRun] });
      if (url.includes("/runs/run-1")) return first.promise;
      if (url.includes("/runs/run-2")) return second.promise;
      return undefined;
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load a run journal");

    await user.selectOptions(select, "run-1");
    await user.selectOptions(select, "run-2");
    await act(async () => second.resolve(jsonResponse(secondJournal)));
    const path = await screen.findByRole("list", { name: "Recorded node path" });
    expect(within(path).getAllByRole("button")).toHaveLength(2);

    await act(async () => first.resolve(jsonResponse(runJournal)));
    expect(within(screen.getByRole("list", { name: "Recorded node path" })).getAllByRole("button")).toHaveLength(2);
    expect(screen.getByText("DOC-2")).toBeInTheDocument();
  });

  it("keeps the latest experiment when an older experiment request finishes last", async () => {
    const first = deferred();
    const second = deferred();
    const secondSummary: ExperimentSummary = { ...experimentSummary, experiment_id: "exp-2" };
    const secondDetail: ExperimentDetail = {
      ...experimentDetail,
      experiment_id: "exp-2",
      runs: [{ ...experimentDetail.runs[0], execution_id: "run-2", manifest_id: "DOC-2" }],
    };
    mockApi((url) => {
      if (url.endsWith("/v1/diagnostics/experiments")) {
        return jsonResponse({ experiments: [experimentSummary, secondSummary] });
      }
      if (url.includes("/experiments/exp-1")) return first.promise;
      if (url.includes("/experiments/exp-2")) return second.promise;
      return undefined;
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load an experiment log");

    await user.selectOptions(select, "exp-1");
    await user.selectOptions(select, "exp-2");
    await act(async () => second.resolve(jsonResponse(secondDetail)));
    expect(await screen.findByRole("button", { name: /DOC-2/ })).toBeInTheDocument();

    await act(async () => first.resolve(jsonResponse(experimentDetail)));
    expect(screen.getByRole("button", { name: /DOC-2/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /DOC-1/ })).not.toBeInTheDocument();
  });

  it("does not restore an experiment that finishes loading after the selector was cleared", async () => {
    const pending = deferred();
    mockApi((url) => (url.includes("/experiments/exp-1") ? pending.promise : undefined));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load an experiment log");

    await user.selectOptions(select, "exp-1");
    await user.selectOptions(select, "");
    await act(async () => pending.resolve(jsonResponse(experimentDetail)));
    expect(screen.queryByText(/Class \/ stage ok/)).not.toBeInTheDocument();
  });

  it("does not restore a run that finishes loading after the selector was cleared", async () => {
    const pending = deferred();
    mockApi((url) => (url.includes("/runs/run-1") ? pending.promise : undefined));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load a run journal");

    await user.selectOptions(select, "run-1");
    await user.selectOptions(select, "");
    await act(async () => pending.resolve(jsonResponse(runJournal)));
    expect(screen.queryByRole("list", { name: "Recorded node path" })).not.toBeInTheDocument();
  });

  it("drops the previous journal as soon as another run is selected", async () => {
    const pending = deferred();
    mockApi((url) => {
      if (url.endsWith("/v1/diagnostics/runs")) return jsonResponse({ runs: [runSummary, secondRun] });
      if (url.includes("/runs/run-2")) return pending.promise;
      return undefined;
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load a run journal");

    await user.selectOptions(select, "run-1");
    await screen.findByRole("list", { name: "Recorded node path" });
    await user.selectOptions(select, "run-2");
    expect(screen.queryByRole("list", { name: "Recorded node path" })).not.toBeInTheDocument();
    await act(async () => pending.resolve(errorResponse(500)));
    expect(screen.queryByRole("list", { name: "Recorded node path" })).not.toBeInTheDocument();
  });

  it("clears the old audit while switching runs and ignores out-of-order audit replies", async () => {
    const auditOne = deferred();
    const auditTwo = deferred();
    mockApi((url) => {
      if (url.endsWith("/v1/diagnostics/runs")) return jsonResponse({ runs: [runSummary, secondRun] });
      if (url.endsWith("/v1/diagnostics/runs/run-2")) return jsonResponse(secondJournal);
      if (url.endsWith("/audit/documents/DOC-1")) return auditOne.promise;
      if (url.endsWith("/audit/documents/DOC-2")) return auditTwo.promise;
      return undefined;
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load a run journal");
    await user.selectOptions(select, "run-1");
    await screen.findByText(/Loading audit for DOC-1/);
    await user.selectOptions(select, "run-2");
    expect(screen.queryByText(/Loading audit for DOC-1/)).not.toBeInTheDocument();
    expect(screen.getByText(/Loading audit for DOC-2/)).toBeInTheDocument();
    await act(async () => auditTwo.resolve(jsonResponse(auditPayload("DOC-2"))));
    expect(await screen.findByText("row-DOC-2")).toBeInTheDocument();
    await act(async () => auditOne.resolve(jsonResponse(auditPayload("DOC-1"))));
    expect(screen.getByText("row-DOC-2")).toBeInTheDocument();
    expect(screen.queryByText("row-DOC-1")).not.toBeInTheDocument();
  });

  it("immediately clears a loaded audit record while the next document is pending", async () => {
    const nextAudit = deferred();
    mockApi((url) => {
      if (url.endsWith("/v1/diagnostics/runs")) return jsonResponse({ runs: [runSummary, secondRun] });
      if (url.endsWith("/v1/diagnostics/runs/run-2")) return jsonResponse(secondJournal);
      if (url.endsWith("/audit/documents/DOC-2")) return nextAudit.promise;
      return undefined;
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    const select = screen.getByLabelText("Load a run journal");
    await user.selectOptions(select, "run-1");
    expect(await screen.findByText("row-DOC-1")).toBeInTheDocument();
    await user.selectOptions(select, "run-2");
    expect(screen.queryByText("row-DOC-1")).not.toBeInTheDocument();
    expect(screen.getByText(/Loading audit for DOC-2/)).toBeInTheDocument();
    await act(async () => nextAudit.resolve(jsonResponse(auditPayload("DOC-2"))));
    expect(await screen.findByText("row-DOC-2")).toBeInTheDocument();
  });

  it("shows failed, unverified, and truncated verification states explicitly", async () => {
    let payload: unknown = auditPayload("DOC-1", "failed");
    mockApi((url) => url.endsWith("/audit/documents/DOC-1") ? jsonResponse(payload) : undefined);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    expect(await screen.findByText("Verification failed")).toBeInTheDocument();
    expect(screen.getByText(/attempts: hash-mismatch/)).toBeInTheDocument();
    payload = { ...auditPayload("DOC-1", "unverified", true), record: { state: "event-chain", entries: 1, events: [] } };
    await user.click(screen.getByLabelText("Follow selected run document"));
    // Manual mode starts empty; direct lookup demonstrates a truncated read.
    await user.type(screen.getByLabelText("Look up document id"), "DOC-1");
    await user.click(screen.getByRole("button", { name: "Look up" }));
    expect(await screen.findByText("Verification unverified")).toBeInTheDocument();
    expect(screen.getByText(/bounded audit read was truncated/)).toBeInTheDocument();
  });

  it("distinguishes an unavailable store from a missing audit record", async () => {
    mockApi((url) => {
      if (!url.includes("/audit/documents/")) return undefined;
      const docId = decodeURIComponent(url.split("/audit/documents/")[1]);
      if (docId === "DOC-MISSING") return jsonResponse({
        doc_id: docId, store: { state: "available", schema: "event-chain" }, record: { state: "not-recorded" }, legacy: { state: "absent", count: 0, truncated: false, entries: [] },
        verification: { status: "not-recorded", checked: { main: 0, attempts: 0, addenda: 0, legacy: 0 }, failures: [], truncated: false, notes: [] },
      });
      return jsonResponse({
        doc_id: docId, store: { state: "unavailable", reason: "database-missing" }, record: { state: "unavailable", reason: "database-missing" },
        verification: { status: "unavailable", checked: { main: 0, attempts: 0, addenda: 0, legacy: 0 }, failures: [], truncated: false, notes: [] },
      });
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);
    await user.click(screen.getByLabelText("Follow selected run document"));
    await user.type(screen.getByLabelText("Look up document id"), "DOC-MISSING");
    await user.click(screen.getByRole("button", { name: "Look up" }));
    expect(await screen.findByText("No audit record")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Look up document id"));
    await user.type(screen.getByLabelText("Look up document id"), "DOC-UNAVAILABLE");
    await user.click(screen.getByRole("button", { name: "Look up" }));
    expect(await screen.findByText("Audit store unavailable")).toBeInTheDocument();
    expect(screen.getByText("database-missing")).toBeInTheDocument();
  });

  it("does not crash on a run journal without node_sequence", async () => {
    const { node_sequence: _omitted, ...broken } = runJournal;
    void _omitted;
    mockApi((url) => (url.includes("/runs/run-1") ? jsonResponse(broken) : undefined));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.selectOptions(screen.getByLabelText("Load a run journal"), "run-1");
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("could not be loaded"),
    );
    expect(screen.queryByRole("list", { name: "Recorded node path" })).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("does not crash on an experiment detail without runs", async () => {
    const { runs: _omitted, ...broken } = experimentDetail;
    void _omitted;
    mockApi((url) => (url.includes("/experiments/exp-1") ? jsonResponse(broken) : undefined));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/All 4 connections/);

    await user.selectOptions(screen.getByLabelText("Load an experiment log"), "exp-1");
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("could not be loaded"),
    );
    expect(screen.queryByText(/Class \/ stage ok/)).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("names a failed collection request and keeps the successful results", async () => {
    mockApi((url) => (url.endsWith("/v1/diagnostics/runs") ? errorResponse(500) : undefined));
    render(<App />);

    expect(await screen.findByText(/All 4 connections/)).toBeInTheDocument();
    const banner = await screen.findByText(/Some diagnostics could not be refreshed/);
    expect(banner.closest("[role=alert]")).toHaveTextContent("Recorded runs");
    expect(banner.closest("[role=alert]")).not.toHaveTextContent("Findings:");
    expect(screen.getByRole("heading", { name: /Findings/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Load an experiment log")).toContainElement(
      screen.getByRole("option", { name: /exp-1/ }),
    );
  });

  it("says why findings are missing instead of claiming the snapshot has not loaded", async () => {
    mockApi((url) => (url.includes("/findings") ? errorResponse(503) : undefined));
    render(<App />);

    await screen.findByText(/All 4 connections/);
    const panel = screen.getByRole("region", { name: "Structural findings" });
    expect(panel).toHaveTextContent("Findings: ");
    expect(panel).not.toHaveTextContent("until the snapshot loads");
  });
});
