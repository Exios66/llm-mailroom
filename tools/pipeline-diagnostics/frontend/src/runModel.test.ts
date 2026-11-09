import { describe, expect, it } from "vitest";

import { buildRunOverlay } from "./runModel";
import type { RunJournal } from "./types";

const journal: RunJournal = {
  schema_version: "1.0.0",
  execution_id: "run-1",
  document: { filename: "doc.txt", manifest_id: "DOC-1", source: "live-manifest" },
  ground_truth: { expected_doc_class: "contract", expected_stage: "archived" },
  started_at: "2026-11-09T05:00:00+00:00",
  finished_at: "2026-11-09T05:00:01+00:00",
  events: [],
  node_sequence: ["intake", "classify", "extract", "archive"],
  transitions: [
    { source: "intake", target: "classify" },
    { source: "classify", target: "extract" },
    { source: "extract", target: "archive" },
  ],
  outcome: { stage: "archived", doc_type: "contract", review: false, failed: false },
};

describe("buildRunOverlay", () => {
  it("marks the full recorded path in first-visit order", () => {
    const overlay = buildRunOverlay(journal)!;
    expect(overlay.visitOrder).toEqual({ intake: 1, classify: 2, extract: 3, archive: 4 });
    expect(overlay.edgeKeys.has("intake->classify")).toBe(true);
    expect(overlay.currentIndex).toBeNull();
    expect(overlay.currentEdgeKey).toBeNull();
  });

  it("slices the path for replay and identifies the current step/edge", () => {
    const overlay = buildRunOverlay(journal, 1)!;
    expect(overlay.visitOrder).toEqual({ intake: 1, classify: 2 });
    expect(overlay.transitions).toEqual([{ source: "intake", target: "classify" }]);
    expect(overlay.edgeKeys.has("classify->extract")).toBe(false);
    expect(overlay.currentIndex).toBe(1);
    expect(overlay.currentEdgeKey).toBe("intake->classify");
  });

  it("clamps an out-of-range step and returns null without a journal", () => {
    expect(buildRunOverlay(journal, 99)!.currentIndex).toBe(3);
    expect(buildRunOverlay(null)).toBeNull();
  });
});
