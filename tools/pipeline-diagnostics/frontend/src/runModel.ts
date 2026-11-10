import type { RunJournal } from "./types";

export interface RunOverlay {
  executionId: string;
  sequence: string[];
  transitions: Array<{ source: string; target: string }>;
  visitOrder: Record<string, number>;
  edgeKeys: Set<string>;
  currentIndex: number | null;
  currentEdgeKey: string | null;
  currentStage: string | null;
}

export function edgeKey(source: string, target: string): string {
  return `${source}->${target}`;
}

/**
 * Convert a recorded journal into graph decoration.
 *
 * `step` is inclusive: null shows the whole recorded path; a number shows the
 * path through that node so the viewer can replay it. Node ids come from the
 * journal, never from a frontend inventory.
 */
export function buildRunOverlay(
  journal: RunJournal | null,
  step: number | null = null,
): RunOverlay | null {
  if (!journal) return null;
  const sequence = journal.node_sequence || [];
  if (sequence.length === 0) return null;
  const currentIndex =
    step === null ? null : Math.max(0, Math.min(step, sequence.length - 1));
  const visible = currentIndex === null ? sequence : sequence.slice(0, currentIndex + 1);
  const visitOrder: Record<string, number> = {};
  visible.forEach((node, index) => {
    if (!(node in visitOrder)) visitOrder[node] = index + 1;
  });
  const transitions = (journal.transitions || []).slice(0, Math.max(0, visible.length - 1));
  const edgeKeys = new Set(transitions.map((transition) => edgeKey(transition.source, transition.target)));
  const currentEdgeKey =
    currentIndex !== null && currentIndex > 0
      ? edgeKey(sequence[currentIndex - 1], sequence[currentIndex])
      : null;
  return {
    executionId: journal.execution_id,
    sequence,
    transitions,
    visitOrder,
    edgeKeys,
    currentIndex,
    currentEdgeKey,
    currentStage: journal.outcome?.stage ?? null,
  };
}
