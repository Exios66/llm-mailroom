# API Reference

Mailroom exposes a FastAPI server on port 8000 by default.

## Starting the API

```bash
PYTHONPATH=src python -m api.main
# or
PYTHONPATH=src uvicorn api.main:app --host 0.0.0.0 --port 8000
```

---

## Endpoints

Each route below is also mounted under `/v1` (for example `GET /health` and
`GET /v1/health` share the same handler). Prefer the `/v1` prefix. Management
routes except health require a bearer token (`MAILROOM_API_TOKEN`).

### Health Check

```
GET /health
```

Checks the API plus best-effort dependency health: LLM provider connectivity (resolves the sorter agent's provider, pings the models endpoint — no completion tokens spent) and database reachability (`SELECT 1`).

**Response:**
```json
{
    "status": "ok",
    "service": "mailroom",
    "checks": {
        "llm_provider": {
            "status": "ok",
            "detail": "openrouter:qwen/qwen3.7-flash",
            "provider": "openrouter"
        },
        "database": {
            "status": "ok",
            "detail": "database reachable"
        },
        "ingestion_paused": false,
        "pause_info": null,
        "watcher": "live",
        "watcher_embedded": true,
        "inbox_pending": 3,
        "watcher_heartbeat_seconds_ago": 2
    }
}
```

`status` is `"ok"` when all checks pass, `"degraded"` when any dependency is unreachable (e.g. provider resolution fails, missing API key, or the models endpoint is down). Dependency checks are best-effort and never block the response.

`checks.watcher` is the producer lamp The-Mailroom reads (`live` / `stale` / `missing`; stale after 15s without a heartbeat). `watcher_heartbeat_seconds_ago` is the age of the watcher's liveness beacon. `inbox_pending` counts processable inbox documents (not `.meta` upload sidecars). The API embeds the inbox watcher by default (`MAILROOM_EMBED_WATCHER=1`) so uploads drain without a second process; set `0` when a dedicated `python -m pipeline.watcher` already holds `watcher.lock`.

---

### Upload Document

```
POST /upload
```

Upload a document to the pipeline inbox. The watcher picks it up automatically and runs the pipeline — no new pipeline run needs to be initialized per upload; the inbox is the queue.

The uploaded file is written to the inbox and a small `<file>.meta` sidecar persists the upload metadata (the submitted `matter_id`, a tracking `upload_id`, upload time, size). The watcher reads the sidecar so the document is filed under the matter you submitted. `matter_id` is honored directly — it does **not** fall back to the filename heuristic when provided.

**Form Data:**
| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | Document file to upload |
| `matter_id` | string | No | Matter ID (default: "DEFAULT") |

**Response (202 Accepted):**
```json
{
    "status": "accepted",
    "file": "contract.pdf",
    "upload_id": "1f2a3b4c5d6e",
    "matter_id": "MATTER-001",
    "message": "File queued for processing — watcher will pick it up."
}
```

`upload_id` is the tracking id for this upload — it appears in the `GET /queue` listing until the file is claimed. The pipeline's `doc_id` (for `GET /status/{doc_id}`) is minted when the watcher starts processing, so poll `GET /queue` or watch the watcher logs for it.

**Example:**
```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@contract.pdf" \
  -F "matter_id=MATTER-001"
```

---

### View the Queue

```
GET /queue
```

Live view of the inbox → processing queue: files currently queued in the inbox (with their `/upload` metadata, including the `upload_id`), files currently being processed by watcher workers, and the most recently updated documents from the catalog.

**Response:**
```json
{
    "queued": [
        {
            "file": "contract.pdf",
            "size": 2048,
            "upload_id": "1f2a3b4c5d6e",
            "matter_id": "MATTER-001",
            "uploaded_at": "2026-08-15T17:00:00+00:00"
        }
    ],
    "queued_count": 1,
    "processing": [{"file": "other.pdf", "worker": "a1b2c3d4"}],
    "processing_count": 1,
    "recent": [
        {
            "doc_id": "550e8400-e29b-41d4-a716-446655440000",
            "file": "done.pdf",
            "matter_id": "MATTER-001",
            "stage": "archived",
            "doc_type": "contract",
            "updated_at": "2026-08-15T17:05:00+00:00"
        }
    ],
    "timestamp": "2026-08-15T17:06:00+00:00"
}
```

Auth-gated like the other management endpoints.

---

### Lookup Document (REVIEW desk)

```
GET /lookup?doc_id=&trace_id=&filename=
```

Resolve a catalog (or manifest) row for The-Mailroom REVIEW proxy. Provide at
least one query parameter. Preference order: `doc_id` → `trace_id` →
`filename` (newest match).

**Response:**
```json
{
  "document": {
    "doc_id": "…",
    "matter_id": "MATTER-001",
    "original_filename": "msa.pdf",
    "stage": "review",
    "doc_type": "contract",
    "escalation_reason": "low_confidence",
    "trace_id": "…"
  }
}
```

**Errors:** `400` (no query), `404` (not found).

---

### REVIEW Tray Queue

```
GET /review/queue
```

Lists parked `stage=review` documents (catalog + on-disk manifests) with the
dispositions each item supports (`resume`, `record`, `requeue`, `complete`).

---

### Resolve Human Review

```
POST /review/{doc_id}/resolve
```

Resolve a document on the REVIEW / RECONSIDER siding. Accepts **JSON** (The-Mailroom
proxy) or **form** (legacy clients).

**Path Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `doc_id` | string | Document ID from the manifest |

**Body / Form fields:**
| Field | Type | Required | Description |
|---|---|---|---|
| `decision` | string | Yes | `approved` or `rejected` |
| `notes` | string | No | Reviewer notes |
| `disposition` | string | No | `resume` (default), `record`, `requeue`, or `complete` |
| `override_doc_type` | string | No | Reroute classification to a live taxonomy class before resume/complete |
| `contract_subtype` / `doc_subclass` | string | No | Optional subtype overrides |
| `extracted_data` | object | For `complete` | Human-finished extraction payload |

**Dispositions:**
| disposition | When | Effect |
|---|---|---|
| `resume` | `stage=review` | Approve → fresh extract under same `doc_id`; reject → failed bin |
| `record` | any stage | Hash-chained audit + optional manifest note; file stays put |
| `requeue` | source file locatable | Copy source back to inbox for a fresh watcher run |
| `complete` | `stage=review` + `decision=approved` | Archive with operator `extracted_data` (no LLM) |

**Response:**
```json
{
    "status": "ok",
    "doc_id": "550e8400-e29b-41d4-a716-446655440000",
    "decision": "approved",
    "disposition": "resume",
    "notes": "Classification confirmed — proceed"
}
```

**Errors:**
- `400`: Invalid decision/disposition, or resume/complete on non-review stage
- `404`: Manifest or source file not found
- `409`: Approve/resume without classification (set `override_doc_type` or requeue)

---

### Analyze Full Audit DB

```
GET /audit?verify=true&recent=20
```

Summarize every row in the local audit log: event/actor histograms, per-doc
hash-chain health, review-related event counts, and recent entries. CLI twin:
`PYTHONPATH=src python src/scripts/analyze_audit_db.py`.

Per-document chain (unchanged): `GET /audit/{doc_id}`.

---

### Get Document Status

```
GET /status/{doc_id}
```

Retrieve the current pipeline status of a document.

**Path Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `doc_id` | string | Document ID |

**Response:**
```json
{
    "doc_id": "550e8400-e29b-41d4-a716-446655440000",
    "matter_id": "MATTER-001",
    "stage": "archived",
    "doc_type": "contract",
    "classification_confidence": 0.95,
    "extraction_confidence": 0.91,
    "escalation_reason": null,
    "created_at": "2024-01-15T10:30:00.000Z",
    "updated_at": "2024-01-15T10:30:15.000Z"
}
```

**Possible stages:** `inbox`, `processing`, `classified`, `review`, `failed`, `archived`

---

### Get Matter Documents

```
GET /matters/{matter_id}
```

List all documents associated with a matter.

**Path Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `matter_id` | string | Matter ID |

**Response:**
```json
{
    "matter_id": "MATTER-001",
    "document_count": 3,
    "documents": [
        {
            "doc_id": "550e8400-...",
            "original_filename": "msa.pdf",
            "doc_type": "contract",
            "stage": "archived",
            "classification_confidence": 0.95,
            "extraction_confidence": 0.91
        }
    ]
}
```

---

### Get Audit Trail

```
GET /audit/{doc_id}
```

Retrieve the full hash-chained audit trail for a document, including a validity check.

**Path Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `doc_id` | string | Document ID |

**Response:**
```json
{
    "doc_id": "550e8400-...",
    "chain_length": 5,
    "chain_valid": true,
    "entries": [
        {
            "entry_id": "...",
            "event": "classified",
            "actor": "sorter",
            "detail": {"doc_type": "contract", "confidence": 0.95},
            "prev_hash": "",
            "entry_hash": "a1b2c3...",
            "timestamp": "2024-01-15T10:30:01.000Z"
        },
        {
            "entry_id": "...",
            "event": "extracted",
            "actor": "contracts_specialist",
            "detail": {"confidence": 0.91},
            "prev_hash": "a1b2c3...",
            "entry_hash": "d4e5f6...",
            "timestamp": "2024-01-15T10:30:05.000Z"
        }
    ]
}
```

The `chain_valid` field is `true` if all hash links are intact. `false` indicates tampering or corruption.

---

### Operations Status

```
GET /ops/status
```

Get pipeline-wide operational metrics.

**Response:**
```json
{
    "stuck_documents": 0,
    "review_queue": 2,
    "error_rates": {
        "contract": {"total": 45, "failed": 1, "review": 3},
        "corporate_record": {"total": 12, "failed": 0, "review": 0}
    },
    "timestamp": "2024-01-15T10:35:00.000Z"
}
```

| Field | Description |
|---|---|
| `stuck_documents` | Documents in `processing` or `inbox` state for >15 minutes |
| `review_queue` | Documents awaiting human review |
| `error_rates` | Per-doc-type breakdown: total, failed, and review counts |

---

### Ops Sweep

```
POST /ops/sweep
```

Run a **one-off Boss ops-monitor sweep on demand** (same logic as the scheduled `pipeline/ops_monitor.py`, without waiting for the interval). Gathers system metrics, runs the Boss agent's analysis, and — if the Boss recommends `pause_ingestion` — writes the `ops_monitor_paused` flag (which the watcher honors). Use this to inspect system health interactively or to trigger a pause without touching the running monitor.

**Response:**
```json
{
    "status": "ok",
    "findings": ["review backlog growing: 12 documents waiting"],
    "severity": "warning",
    "recommended_action": "alert",
    "paused_ingestion": false,
    "timestamp": "2024-01-15T10:35:00.000Z"
}
```

**Errors:**
- `500`: Metrics gathering or Boss analysis failed

---

### Resume Ingestion

```
POST /ops/resume
```

Clear the `ops_monitor_paused` flag so the watcher resumes processing new files. The watcher honors the flag on every file event, so this takes effect without a restart.

**Response:**
```json
{
    "status": "ok",
    "was_paused": true,
    "paused_ingestion": false
}
```

`was_paused` is `true` if the pause flag existed and was cleared; `false` if ingestion was not paused.

---

## Error Responses

All errors follow a consistent format:

```json
{
    "detail": "Error message description"
}
```

| Status | Meaning |
|---|---|
| `400` | Bad request — invalid input |
| `404` | Resource not found |
| `500` | Internal server error / database unavailable |

---

## Interactive Docs

When the API is running, visit:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## API Versioning

The Mailroom API is versioned under `/v1`. Unversioned routes remain as
aliases during the deprecation window and share the same handlers, auth,
and status codes.

### Versioning policy

| Concern | Policy |
|---|---|
| **Current status** | `/v1` is the versioned surface; unversioned routes are deprecated |
| **Version prefix** | `/v1/` |
| **Backwards compatibility** | Breaking changes are grouped into a single release; the old route set is deprecated for one minor release before removal |
| **Response evolution** | Additive fields in JSON responses are allowed within a version (consumers must ignore unknown fields) |
| **Removal of fields** | Always a breaking change → new version |
| **Content type** | `application/json` only |

### Guidance for API consumers

- Prefer `/v1/...` for all new integrations. Unversioned routes will be removed after the deprecation window (see `CHANGELOG.md`).
- Do not depend on undocumented response fields — only fields documented in this reference are stable.
- Breaking changes are announced in `CHANGELOG.md` under the "Breaking changes" section of the release.
- Every management endpoint except `GET /health` and `GET /v1/health` requires `Authorization: Bearer $MAILROOM_API_TOKEN`, including `GET /matters/{matter_id}` and `GET /v1/matters/{matter_id}`.

### `/v1` layout

```
GET  /v1/health
POST /v1/upload
GET  /v1/queue
POST /v1/review/{doc_id}/resolve
GET  /v1/status/{doc_id}
GET  /v1/matters/{matter_id}
GET  /v1/audit/{doc_id}
GET  /v1/ops/status
POST /v1/ops/sweep
POST /v1/ops/resume
```

The unversioned routes (`GET /health`, `POST /upload`, …) continue to work during the deprecation window, then will be removed.
