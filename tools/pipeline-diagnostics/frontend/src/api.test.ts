import { afterEach, describe, expect, it } from "vitest";

import { readToken, writeToken } from "./api";

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
