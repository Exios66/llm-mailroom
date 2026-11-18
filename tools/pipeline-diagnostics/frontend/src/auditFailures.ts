/** Display helpers for the audit verification failure list. */

export interface AuditFailure {
  scope?: string | null;
  id?: string | null;
  reason?: string | null;
}

/**
 * Failure rows shown before the rest are summarised as "N more". A tampered
 * store can produce one failure per foreign sub-row (the reader lists up to
 * `limits.foreign_attempts` of them), which must not push the record off screen.
 */
export const FAILURE_DISPLAY_LIMIT = 10;

/**
 * One stable key per failure: scope, id and reason, never the list position, so a
 * row keeps its identity when rows before it appear or disappear. Rows that are
 * identical in all three (for example two id-less orphans) get an occurrence
 * suffix so keys stay unique.
 */
export function failureKeys(failures: AuditFailure[]): string[] {
  const seen = new Map<string, number>();
  return failures.map((failure) => {
    const base = `${failure.scope ?? ""}|${failure.id ?? ""}|${failure.reason ?? ""}`;
    const occurrence = (seen.get(base) ?? 0) + 1;
    seen.set(base, occurrence);
    return occurrence === 1 ? base : `${base}#${occurrence}`;
  });
}

export function limitFailures<T extends AuditFailure>(
  failures: T[],
  limit: number = FAILURE_DISPLAY_LIMIT,
): { visible: T[]; hidden: number } {
  return { visible: failures.slice(0, limit), hidden: Math.max(0, failures.length - limit) };
}
