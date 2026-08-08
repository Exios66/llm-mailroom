# `schemas/` — The data shapes

## What this folder is (plain English)

Every piece of data that flows through the pipeline has a defined shape here, using **Pydantic** (a Python library that validates data). Think of these as the *forms* that must be filled in correctly — if something is the wrong type or missing, Pydantic catches it.

The big ones:

- `documents.py` — the **extraction schemas**: what each specialist agent is allowed to return (e.g. a contract extraction has `parties`, `effective_date`, `governing_law`, …).
- `manifest.py` — `DocumentManifest`: the ID card for every document (doc_id, matter, stage, confidence scores, …) and `PipelineStage` (inbox/processing/classified/review/failed/archived).
- `audit.py` — `AuditLogEntry` + the **hash-chaining** logic that makes the audit trail tamper-evident.
- `matter.py` — `Matter`: a client matter that documents belong to.

## Technical reference

- `documents.py` — `EXTRACTION_SCHEMAS: dict[str, type[BaseModel]]` maps doc-type key → schema; `get_extraction_schema(doc_type)`. Adding a doc class requires adding its schema here **and** to `EXTRACTION_SCHEMAS`.
- `manifest.py` — `PipelineStage(str, Enum)`; `DocumentManifest` with `touch()` to bump `updated_at`. `doc_id` auto-generates as a UUID. Serialized to JSON sidecars via `pipeline/bins.py:save_manifest`.
- `audit.py`
  - `compute_audit_hash(prev_hash, doc_id, entry_id, event, detail)` — SHA-256 over a sorted JSON payload.
  - `build_audit_entry(...)` — creates the entry and fills `entry_hash`.
  - `verify_chain(entries)` — re-checks every link in order; `False` means tampering. Also used by `api` `GET /audit/{doc_id}`.
  - Audit entries are persisted to SQLite by `storage/audit_log.py` (see `storage/` README).
- `matter.py` — `Matter` with `matter_id`, `name`, `client_name`, `practice_area`, `opened_at`.
- `schemas/__init__.py` re-exports everything for convenience (`from schemas import DocumentManifest, ...`).
- Note: `DateTime(timezone=True)` values are stored in SQLite as naive UTC; `verify_chain` sorts by `timestamp`, so timezone handling is on the caller.
