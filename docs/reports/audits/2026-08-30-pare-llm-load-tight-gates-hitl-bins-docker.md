# Pare LLM load + tight gates + HITL bins + Docker

**Date:** 2026-08-30
**Kind:** audits
**Status:** complete

## Summary

Mailroom happy-path LLM load is pared to **classify + extract** (two generations). Open-ended obligation dumps are replaced by CUAD/MAUD/insurance checklists; corporate, correspondence, and insurance add a semantic trio (`intent` / `subject_matter` / `keywords`). Severity-aware confidence gates and higher retry budgets tighten auto-archive while giving the graph more self-correction before HITL. Arbiter/judge fields persist on archive and review/failed terminals. Docker producer baseline is hardened (non-root, multi-stage, HEALTHCHECK, pinned compose secrets).

## Changes

### Extraction model
- `ContractExtraction`: key entities + `cuad_clauses` / `maud_clauses` (no `key_obligations` / `termination_clauses`).
- Corporate / correspondence / insurance: semantic enrichment; insurance also has `claim_checklist`.
- Compliance: slim key entities (capped `key_requirements`).
- Pilot `manifest.csv` `expected_fields` and field-scoring maps retargeted.

### Gates and Lane B
- Per-class severity tiers in `taxonomy.yaml` (`by_class`); `get_confidence_thresholds(doc_type)`.
- `retry_max: 2`, `arbiter_retry_max: 2`, `judge_max_passes: 3` (= `1 + arbiter_retry_max`).
- Reviewer override uses the reviewer's proposed class high threshold.

### Reporter → procedural assemble
- `compile_report` / `agents/reporter.py` no longer call `get_llm("reporter")`.
- Archivist remains the success-path durable sink; arbiter caveats land in `_report` + manifest/audit/catalog.

### HITL bins
| Disposition | Bin |
|---|---|
| approved `resume` / `complete` (gates pass) | `archive/` |
| post-resume soft miss | `review/` again |
| rejected | `failed/` |
| requeue | `inbox/` |
| record | no move |

### Docker
- Root `Dockerfile`: multi-stage, `USER mailroom`, `HEALTHCHECK`.
- Producer compose: `user: 10001:10001`, `no-new-privileges`.
- Langfuse compose: env-required secrets, pinned tags.
- `publish_space.py --check` asserts the baseline.

## Verification

- `PYTHONPATH=src python3 -m pytest -q -k "not notebook_suite"` — green.
- Notebook headless suite needs a registered `python3` Jupyter kernel (`ipykernel`); environment-dependent.
- `PYTHONPATH=src python3 src/scripts/publish_space.py --check` — Dockerfile assertions.

## Follow-ups

- Collapsing agent directories (issue #13) remains out of scope.
- Optional org-wide hadolint CI.
