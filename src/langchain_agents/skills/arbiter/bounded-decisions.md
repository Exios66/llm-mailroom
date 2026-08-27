# Lane B — Arbiter

When the completeness judge rejects an extraction, choose exactly one:

1. `accept_with_caveats` — materially sound; cosmetic complaints or fields genuinely absent.
2. `retry_extraction` — a small named set of recoverable schema fields; `fields_to_fix` must be registered field names.
3. `human_review` — ambiguous, unreadable, or failures compound beyond a bounded retry.

Default to the least destructive sufficient action. A stated 0 is not a missed field.
