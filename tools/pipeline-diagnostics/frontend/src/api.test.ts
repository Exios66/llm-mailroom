import { afterEach, describe, expect, it } from "vitest";

import { DiagnosticsApiError, parseExperimentDetail, parseRunJournal, readToken, writeToken } from "./api";

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

  it("rejects an experiment detail without a runs array", () => {
    expect(() => parseExperimentDetail({ experiment_id: "e" })).toThrow(DiagnosticsApiError);
    expect(() => parseExperimentDetail({ runs: "x" })).toThrow(DiagnosticsApiError);
    expect(parseExperimentDetail({ runs: [] }).corpus).toEqual({});
  });
});
