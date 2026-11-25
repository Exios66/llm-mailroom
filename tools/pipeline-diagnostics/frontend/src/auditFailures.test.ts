import { describe, expect, it } from "vitest";

import { FAILURE_DISPLAY_LIMIT, failureKeys, limitFailures } from "./auditFailures";

const failure = (scope: string, reason: string, id: string | null = null) => ({ scope, reason, id });

describe("audit failure rows", () => {
  it("derives a stable key from scope, id and reason, not from the position", () => {
    const rows = [failure("attempts", "attempt-orphan", "a-1"), failure("attempts", "hash-mismatch", "a-2")];
    const keys = failureKeys(rows);
    expect(keys[0]).toBe("attempts|a-1|attempt-orphan");
    // The same failure keeps its key when an earlier row appears or disappears.
    expect(failureKeys([failure("main", "seq-gap"), ...rows]).slice(1)).toEqual(keys);
  });

  it("keeps identical rows distinct with an occurrence suffix", () => {
    const keys = failureKeys([failure("attempts", "attempt-orphan"), failure("attempts", "attempt-orphan")]);
    expect(new Set(keys).size).toBe(2);
    expect(keys[0]).toBe("attempts||attempt-orphan");
    expect(keys[1]).toBe("attempts||attempt-orphan#2");
  });

  it("shows at most the display limit and counts the rest", () => {
    const rows = Array.from({ length: FAILURE_DISPLAY_LIMIT + 7 }, (_, index) => failure("attempts", "attempt-doc-mismatch", `a-${index}`));
    const shown = limitFailures(rows);
    expect(shown.visible).toHaveLength(FAILURE_DISPLAY_LIMIT);
    expect(shown.hidden).toBe(7);
    expect(limitFailures(rows.slice(0, 3))).toEqual({ visible: rows.slice(0, 3), hidden: 0 });
  });
});
