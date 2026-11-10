import type {
  CheckReport,
  ExperimentDetail,
  ExperimentSummary,
  RunJournal,
  RunSummary,
  Snapshot,
} from "./types";

const TOKEN_KEY = "mailroom-diagnostics-token";

// In-memory fallback: if the browser blocks session storage, the token entered
// on this page must still be used until reload.
let memoryToken = "";

export class DiagnosticsApiError extends Error {
  constructor(
    readonly status: number | null,
    message: string,
  ) {
    super(message);
    this.name = "DiagnosticsApiError";
  }
}

export function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export function writeToken(value: string): void {
  memoryToken = value;
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can be unavailable in hardened browsers; the in-memory value
    // keeps this page's requests authenticated until reload.
  }
}

function requestHeaders(): HeadersInit {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = readToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: requestHeaders(), signal, cache: "no-store" });
  } catch {
    throw new DiagnosticsApiError(
      null,
      "Diagnostics API unreachable. Start `mailroom-diagnostics serve` (loopback) or build the static assets.",
    );
  }
  if (!response.ok) {
    if (response.status === 401) {
      throw new DiagnosticsApiError(
        401,
        "The diagnostics API rejected this request. Add a bearer token under Authentication.",
      );
    }
    if (response.status === 503) {
      throw new DiagnosticsApiError(
        503,
        "The diagnostics API has no token for this listener or could not build the graph. Check the server log.",
      );
    }
    throw new DiagnosticsApiError(
      response.status,
      `Diagnostics request failed with HTTP ${response.status}.`,
    );
  }
  return (await response.json()) as T;
}

export function fetchSnapshot(signal?: AbortSignal): Promise<Snapshot> {
  return getJson<Snapshot>("/v1/diagnostics/snapshot", signal);
}

export function fetchFindings(signal?: AbortSignal): Promise<CheckReport> {
  return getJson<CheckReport>("/v1/diagnostics/findings", signal);
}


export async function fetchRuns(signal?: AbortSignal): Promise<RunSummary[]> {
  const payload = await getJson<{ runs: RunSummary[] }>("/v1/diagnostics/runs", signal);
  if (!Array.isArray(payload?.runs)) throw malformed("run list");
  return payload.runs;
}

function malformed(what: string): DiagnosticsApiError {
  return new DiagnosticsApiError(null, `The diagnostics API returned a malformed ${what}.`);
}

/** Reject a journal the viewer cannot iterate instead of crashing during playback. */
export function parseRunJournal(payload: unknown): RunJournal {
  const journal = payload as Partial<RunJournal> | null;
  if (
    !journal ||
    typeof journal !== "object" ||
    !Array.isArray(journal.node_sequence) ||
    !journal.node_sequence.every((node) => typeof node === "string")
  ) {
    throw malformed("run journal");
  }
  return {
    ...(journal as RunJournal),
    events: Array.isArray(journal.events) ? journal.events : [],
    transitions: Array.isArray(journal.transitions)
      ? journal.transitions.filter(
          (edge) =>
            !!edge &&
            typeof edge === "object" &&
            typeof edge.source === "string" &&
            typeof edge.target === "string",
        )
      : [],
    document: journal.document ?? {},
    outcome: journal.outcome ?? { review: false, failed: false },
  };
}

/** Reject an experiment detail without a runs list instead of crashing the panel. */
export function parseExperimentDetail(payload: unknown): ExperimentDetail {
  const detail = payload as Partial<ExperimentDetail> | null;
  if (
    !detail ||
    typeof detail !== "object" ||
    !Array.isArray(detail.runs) ||
    !detail.runs.every((run) => !!run && typeof run === "object")
  ) {
    throw malformed("experiment detail");
  }
  return {
    ...(detail as ExperimentDetail),
    corpus: detail.corpus ?? {},
    metrics: detail.metrics ?? {},
  };
}

export async function fetchRun(executionId: string, signal?: AbortSignal): Promise<RunJournal> {
  return parseRunJournal(
    await getJson<unknown>(`/v1/diagnostics/runs/${encodeURIComponent(executionId)}`, signal),
  );
}

export async function fetchExperiments(signal?: AbortSignal): Promise<ExperimentSummary[]> {
  const payload = await getJson<{ experiments: ExperimentSummary[] }>(
    "/v1/diagnostics/experiments",
    signal,
  );
  if (!Array.isArray(payload?.experiments)) throw malformed("experiment list");
  return payload.experiments;
}


export async function fetchExperiment(
  experimentId: string,
  signal?: AbortSignal,
): Promise<ExperimentDetail> {
  return parseExperimentDetail(
    await getJson<unknown>(
      `/v1/diagnostics/experiments/${encodeURIComponent(experimentId)}`,
      signal,
    ),
  );
}
