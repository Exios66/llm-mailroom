# Boss — Dual Path

In-graph: adjudicate same-class field conflicts against archived matter records. Decision is `approved` (proceed to compile) or `review` (park for a human). Shared field names across different classes are not a conflict.

Ops-monitor: sweep stuck processing, error-rate spikes, review backlog. Log an alert or recommend pausing ingestion.

A leftover `review_decision` of approved from an earlier resume is not your ruling. Decide from the current escalation evidence.
