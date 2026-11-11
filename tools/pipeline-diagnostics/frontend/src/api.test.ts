import { afterEach, describe, expect, it } from "vitest";

import { DiagnosticsApiError, parseAuditDocumentDetail, parseAuditDocumentList, parseExperimentDetail, parseRunJournal, readToken, writeToken } from "./api";

const originalSetItem = Storage.prototype.setItem;
const originalGetItem = Storage.prototype.getItem;

afterEach(() => {
  Storage.prototype.setItem = originalSetItem;
  Storage.prototype.getItem = originalGetItem;
  sessionStorage.clear();
});

describe("token storage", () => {
  it("keeps the token in memory when session storage is blocked", () => {
    Storage.prototype.setItem = () => {
      throw new Error("storage blocked");
    };
    Storage.prototype.getItem = () => {
      throw new Error("storage blocked");
    };
    writeToken("memory-token");
    expect(readToken()).toBe("memory-token");
    writeToken("");
    expect(readToken()).toBe("");
  });

  it("round-trips through session storage when available", () => {
    writeToken("stored-token");
    expect(readToken()).toBe("stored-token");
    expect(sessionStorage.getItem("mailroom-diagnostics-token")).toBe("stored-token");
    writeToken("");
    expect(sessionStorage.getItem("mailroom-diagnostics-token")).toBeNull();
  });
});

describe("response validation", () => {
  it("rejects a run journal without a node_sequence array", () => {
    expect(() => parseRunJournal({ execution_id: "r" })).toThrow(DiagnosticsApiError);
    expect(() => parseRunJournal({ node_sequence: 1 })).toThrow(DiagnosticsApiError);
    expect(() => parseRunJournal({ node_sequence: ["a", 2] })).toThrow(DiagnosticsApiError);
    expect(() => parseRunJournal(null)).toThrow(DiagnosticsApiError);
  });

  it("fills in optional journal arrays so playback can read them", () => {
    const journal = parseRunJournal({ execution_id: "r", node_sequence: ["a"], transitions: 3 });
    expect(journal.transitions).toEqual([]);
    expect(journal.events).toEqual([]);
    expect(journal.outcome.failed).toBe(false);
  });

  it("drops transitions the run model cannot read", () => {
    const journal = parseRunJournal({
      execution_id: "r",
      node_sequence: ["a", "b"],
      transitions: [null, { source: "a" }, { source: "a", target: "b" }],
    });
    expect(journal.transitions).toEqual([{ source: "a", target: "b" }]);
  });

  it("rejects an experiment detail without a runs array", () => {
    expect(() => parseExperimentDetail({ experiment_id: "e" })).toThrow(DiagnosticsApiError);
    expect(() => parseExperimentDetail({ runs: "x" })).toThrow(DiagnosticsApiError);
    expect(() => parseExperimentDetail({ runs: [null] })).toThrow(DiagnosticsApiError);
    expect(() => parseExperimentDetail({ runs: ["x"] })).toThrow(DiagnosticsApiError);
    expect(parseExperimentDetail({ runs: [] }).corpus).toEqual({});
  });

  it("validates audit payloads and drops non-string text leaves", () => {
    const list = parseAuditDocumentList({
      documents: [{ doc_id: "DOC-1", actor: { raw: "must not stringify" } }],
      store: { state: "available" }, pagination: { returned: 1 },
    });
    expect(list.documents[0].actor).toBeNull();
    expect(() => parseAuditDocumentList({ documents: "not-an-array" })).toThrow(DiagnosticsApiError);

    const detail = parseAuditDocumentDetail({
      doc_id: "DOC-1", record: { state: "event-chain", events: [{ event: { raw: "secret" }, actor: "archivist", entry_hash: "sha256:abc" }] },
      verification: { status: "verified", checked: { main: 1 } },
    });
    expect(detail.record.events?.[0].event).toBeNull();
    expect(detail.record.events?.[0].actor).toBe("archivist");
    expect(detail.verification.checked.main).toBe(1);
    expect(() => parseAuditDocumentDetail({ doc_id: "DOC-1" })).toThrow(DiagnosticsApiError);
  });

  it("preserves event and attempt arrays within the backend's advertised audit limits", () => {
    const events = Array.from({ length: 1500 }, (_, index) => ({ event: `event-${index}` }));
    const attempts = Array.from({ length: 150 }, (_, index) => ({ attempt_id: `attempt-${index}` }));
    const detail = parseAuditDocumentDetail({
      doc_id: "DOC-LARGE", store: { state: "available", schema: "event-chain" },
      record: { state: "event-chain", entries: 1500, events, attempts, addenda: [] },
      legacy: { state: "absent", count: 0, truncated: false, entries: [] },
      verification: { status: "verified", checked: { main: 1500, attempts: 150, addenda: 0, legacy: 0 }, failures: [], truncated: false },
      limits: { events: 2000, attempts: 500, addenda: 200, decision_fields: 40 },
    });
    expect(detail.record.events).toHaveLength(1500);
    expect(detail.record.attempts).toHaveLength(150);
    expect(detail.verification.status).toBe("verified");
  });

  it.each([
    ["event rows", { record: { state: "event-chain", events: Array(2001).fill({ event: "archived" }) } }],
    ["attempt rows", { record: { state: "unified-row", attempts: Array(501).fill({ attempt_id: "a" }) } }],
    ["addenda rows", { record: { state: "unified-row", addenda: Array(201).fill({ kind: "rescore" }) } }],
  ])("rejects an overbound audit response with excess %s instead of trimming it", (_label, extra) => {
    const record = Object.assign({ state: "event-chain", events: [], attempts: [], addenda: [] }, extra.record);
    const detail = {
      doc_id: "DOC-OVERBOUND", store: { state: "available" },
      record,
      legacy: { state: "absent", count: 0, truncated: false, entries: [] },
      verification: { status: "verified", checked: { main: 0, attempts: 0, addenda: 0, legacy: 0 }, failures: [], truncated: false },
      limits: { events: 2000, attempts: 500, addenda: 200, decision_fields: 40 },
    };
    expect(() => parseAuditDocumentDetail(detail)).toThrow(DiagnosticsApiError);
  });

  it("preserves audit list pagination metadata without accepting excess rows", () => {
    const page = parseAuditDocumentList({
      store: { state: "available" }, pagination: { limit: 25, offset: 25, returned: 1, more: true },
      documents: [{ doc_id: "DOC-26" }],
    });
    expect(page.pagination).toEqual({ limit: 25, offset: 25, returned: 1, more: true });
    expect(() => parseAuditDocumentList({ pagination: { limit: 1 }, documents: [{ doc_id: "a" }, { doc_id: "b" }] })).toThrow(DiagnosticsApiError);
  });
});
