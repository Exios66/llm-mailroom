import type {
  CheckReport,
  AuditDocumentDetail,
  AuditDocumentList,
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

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const textValue = (value: unknown, max = 512): string | null =>
  typeof value === "string" ? value.slice(0, max) : null;
const numberValue = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const AUDIT_LIMITS = { events: 2000, attempts: 500, addenda: 200, decision_fields: 40 } as const;

function boundedArray(value: unknown, maximum: number, what: string): unknown[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > maximum) throw malformed(what);
  return value;
}

function stringList(value: unknown, maxItems = 100): string[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > maxItems) throw malformed("audit string list");
  return value.flatMap((item) => (typeof item === "string" ? [item.slice(0, 256)] : []));
}

function parseAuditEntry(value: unknown, fieldLimit: number) {
  const entry = objectValue(value);
  return {
    source: textValue(entry.source, 32) ?? undefined,
    entry_id: textValue(entry.entry_id, 256),
    event: textValue(entry.event, 64),
    actor: textValue(entry.actor, 64),
    matter_id: textValue(entry.matter_id, 256),
    timestamp: textValue(entry.timestamp, 80),
    seq: numberValue(entry.seq),
    prev_hash: textValue(entry.prev_hash, 256),
    entry_hash: textValue(entry.entry_hash, 256),
    status: textValue(entry.status, 64),
    decision_fields: stringList(entry.decision_fields, fieldLimit),
  };
}

function parseAuditAttempt(value: unknown, fieldLimit: number) {
  const attempt = objectValue(value);
  return {
    attempt_id: textValue(attempt.attempt_id, 256),
    stage: textValue(attempt.stage, 64),
    attempt_no: numberValue(attempt.attempt_no),
    trigger: textValue(attempt.trigger, 64),
    outcome: textValue(attempt.outcome, 64),
    started_at: textValue(attempt.started_at, 80),
    ended_at: textValue(attempt.ended_at, 80),
    entry_hash: textValue(attempt.entry_hash, 256),
    decision_fields: stringList(attempt.decision_fields, fieldLimit),
  };
}

function parseAuditAddendum(value: unknown, limits: typeof AUDIT_LIMITS) {
  const addendum = objectValue(value);
  return {
    addendum_id: textValue(addendum.addendum_id, 256),
    kind: textValue(addendum.kind, 64),
    seq: numberValue(addendum.seq),
    actor: textValue(addendum.actor, 64),
    timestamp: textValue(addendum.timestamp, 80),
    prev_hash: textValue(addendum.prev_hash, 256),
    entry_hash: textValue(addendum.entry_hash, 256),
    decision_fields: stringList(addendum.decision_fields, limits.decision_fields),
    attempts: boundedArray(addendum.attempts, limits.attempts, "audit addendum attempts")
      .map((attempt) => parseAuditAttempt(attempt, limits.decision_fields)),
  };
}

export function parseAuditDocumentList(payload: unknown): AuditDocumentList {
  const data = objectValue(payload);
  if (!Array.isArray(data.documents)) throw malformed("audit document list");
  const pagination = objectValue(data.pagination);
  const store = objectValue(data.store);
  const limit = numberValue(pagination.limit) ?? 25;
  if (!Number.isInteger(limit) || limit < 1 || limit > 100 || data.documents.length > limit) {
    throw malformed("audit document list pagination");
  }
  return {
    schema_version: textValue(data.schema_version, 32) ?? "unknown",
    read_only: true,
    store: {
      state: textValue(store.state, 32) ?? "unavailable",
      reason: textValue(store.reason, 64),
      schema: textValue(store.schema, 64),
      engine: textValue(store.engine, 64),
    },
    pagination: {
      limit,
      offset: numberValue(pagination.offset) ?? 0,
      returned: numberValue(pagination.returned) ?? data.documents.length,
      more: pagination.more === true,
    },
    documents: data.documents.flatMap((value) => {
      const row = objectValue(value);
      const docId = textValue(row.doc_id, 128);
      return docId ? [{
        doc_id: docId,
        matter_id: textValue(row.matter_id, 256),
        event: textValue(row.event, 64),
        actor: textValue(row.actor, 64),
        timestamp: textValue(row.timestamp, 80),
        seq: numberValue(row.seq),
        entries: numberValue(row.entries),
        source: textValue(row.source, 32) ?? "unknown",
      }] : [];
    }),
    notes: stringList(data.notes),
  };
}

export function parseAuditDocumentDetail(payload: unknown): AuditDocumentDetail {
  const data = objectValue(payload);
  if (typeof data.doc_id !== "string" || !data.record || typeof data.record !== "object") {
    throw malformed("audit document detail");
  }
  const rawRecord = objectValue(data.record);
  const rawLegacy = objectValue(data.legacy);
  const rawVerification = objectValue(data.verification);
  const checked = objectValue(rawVerification.checked);
  const rawStore = data.store === null ? null : objectValue(data.store);
  const rawLimits = objectValue(data.limits);
  const limits = Object.fromEntries(Object.entries(AUDIT_LIMITS).map(([key, defaultLimit]) => {
    const advertised = numberValue(rawLimits[key]);
    if (advertised !== null && (!Number.isInteger(advertised) || advertised < 1 || advertised > defaultLimit)) {
      throw malformed("audit detail limits");
    }
    return [key, advertised ?? defaultLimit];
  })) as typeof AUDIT_LIMITS;
  const events = boundedArray(rawRecord.events, limits.events, "audit events");
  const attempts = boundedArray(rawRecord.attempts, limits.attempts, "audit attempts");
  const addenda = boundedArray(rawRecord.addenda, limits.addenda, "audit addenda");
  const legacyEntries = boundedArray(rawLegacy.entries, limits.events, "legacy audit events");
  const decisionFields = (value: unknown) => stringList(value, limits.decision_fields);
  const record = {
    state: textValue(rawRecord.state, 32) ?? "unavailable",
    reason: textValue(rawRecord.reason, 64),
    entries: numberValue(rawRecord.entries),
    last_event: textValue(rawRecord.last_event, 64),
    status: textValue(rawRecord.status, 64),
    started_at: textValue(rawRecord.started_at, 80),
    updated_at: textValue(rawRecord.updated_at, 80),
    events: events.map((entry) => parseAuditEntry(entry, limits.decision_fields)),
    entry_id: textValue(rawRecord.entry_id, 256),
    matter_id: textValue(rawRecord.matter_id, 256),
    event: textValue(rawRecord.event, 64),
    actor: textValue(rawRecord.actor, 64),
    timestamp: textValue(rawRecord.timestamp, 80),
    seq: numberValue(rawRecord.seq),
    prev_hash: textValue(rawRecord.prev_hash, 256),
    entry_hash: textValue(rawRecord.entry_hash, 256),
    terminal: rawRecord.terminal === true,
    decision_fields: decisionFields(rawRecord.decision_fields),
    attempts: attempts.map((attempt) => parseAuditAttempt(attempt, limits.decision_fields)),
    attempts_total: numberValue(rawRecord.attempts_total) ?? 0,
    addenda: addenda.map((addendum) => parseAuditAddendum(addendum, limits)),
  };
  const totalAttempts = record.attempts.length + record.addenda.reduce((total, addendum) => total + addendum.attempts.length, 0);
  if (totalAttempts > limits.attempts) throw malformed("audit attempts");
  return {
    schema_version: textValue(data.schema_version, 32) ?? "unknown",
    read_only: true,
    doc_id: data.doc_id.slice(0, 128),
    store: rawStore ? {
      state: textValue(rawStore.state, 32) ?? "unavailable",
      reason: textValue(rawStore.reason, 64),
      schema: textValue(rawStore.schema, 64),
      engine: textValue(rawStore.engine, 64),
    } : null,
    record,
    legacy: {
      state: textValue(rawLegacy.state, 32) ?? "absent",
      count: numberValue(rawLegacy.count) ?? 0,
      truncated: rawLegacy.truncated === true,
      entries: legacyEntries.map((entry) => parseAuditEntry(entry, limits.decision_fields)),
    },
    verification: {
      status: textValue(rawVerification.status, 32) ?? "unavailable",
      method: textValue(rawVerification.method, 128),
      checked: {
        main: numberValue(checked.main) ?? 0,
        attempts: numberValue(checked.attempts) ?? 0,
        addenda: numberValue(checked.addenda) ?? 0,
        legacy: numberValue(checked.legacy) ?? 0,
      },
      failures: boundedArray(rawVerification.failures, 100, "audit verification failures").map((item) => {
        const failure = objectValue(item);
        return { scope: textValue(failure.scope, 64), id: textValue(failure.id, 256), reason: textValue(failure.reason, 64) };
      }),
      truncated: rawVerification.truncated === true,
      notes: stringList(rawVerification.notes),
    },
    limits,
    notes: stringList(data.notes),
  };
}

export async function fetchAuditDocuments(limit = 25, offset = 0, signal?: AbortSignal): Promise<AuditDocumentList> {
  return parseAuditDocumentList(await getJson<unknown>(`/v1/diagnostics/audit/documents?limit=${limit}&offset=${offset}`, signal));
}

export async function fetchAuditDocument(docId: string, signal?: AbortSignal): Promise<AuditDocumentDetail> {
  return parseAuditDocumentDetail(await getJson<unknown>(`/v1/diagnostics/audit/documents/${encodeURIComponent(docId)}`, signal));
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
