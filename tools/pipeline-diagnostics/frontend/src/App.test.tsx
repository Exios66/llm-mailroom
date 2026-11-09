import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type { CheckReport, Snapshot } from "./types";

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

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function mockApi() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    const url = String(input);
    if (url.includes("/snapshot")) return jsonResponse(snapshot);
    if (url.includes("/findings")) return jsonResponse(report);
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
