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
  document: { filename: "real-doc.txt", manifest_id: "DOC-1", source: "live-manifest" },
  outcome: { stage: "archived", doc_type: "contract", review: false, failed: false },
  started_at: "2026-11-09T05:00:00+00:00",
  finished_at: "2026-11-09T05:00:01+00:00",
  node_count: 3,
};

const runJournal: RunJournal = {
  schema_version: "1.0.0",
  execution_id: "run-1",
  document: { filename: "real-doc.txt", manifest_id: "DOC-1", source: "live-manifest" },
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

function mockApi(override?: Override) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    const url = String(input);
    const overridden = override?.(url);
    if (overridden) return overridden;
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
    document: { filename: "other.txt", manifest_id: "DOC-2", source: "live-manifest" },
  };
  const secondJournal: RunJournal = {
    ...runJournal,
    execution_id: "run-2",
    document: { filename: "other.txt", manifest_id: "DOC-2", source: "live-manifest" },
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
