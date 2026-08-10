# API Reference

Mailroom exposes a FastAPI server on port 8000.

## Start the API

```bash
python api/main.py
# or
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

**Interactive docs:** `http://localhost:8000/docs` (Swagger) | `http://localhost:8000/redoc` (ReDoc)

---

## Endpoints

### `GET /health`

Health check — API plus best-effort dependency checks: LLM provider connectivity (pings the models endpoint, no completion tokens) and database reachability (`SELECT 1`).

**Response:**
```json
{
  "status": "ok",
  "service": "mailroom",
  "checks": {
    "llm_provider": {"status": "ok", "detail": "openrouter:qwen/qwen3.7-flash", "provider": "openrouter"},
    "database": {"status": "ok", "detail": "database reachable"}
  }
}
```

`status` is `"degraded"` when any dependency is unreachable.

---

### `POST /upload`

Upload a document to the pipeline inbox.

**Form Data:**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | Document to upload |
| `matter_id` | string | No | Matter ID (default: "DEFAULT") |

**Response (202):**
```json
{
    "status": "accepted",
    "file": "contract.pdf",
    "matter_id": "MATTER-001",
    "message": "File queued for processing"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@contract.pdf" \
  -F "matter_id=MATTER-001"
```

---

### `POST /review/{doc_id}/resolve`

Resolve a document in human review.

**Path Parameters:** `doc_id` (string) — Document ID

**Form Data:**

| Field | Type | Required | Description |
|---|---|---|---|
| `decision` | string | Yes | `approved` or `rejected` |
| `notes` | string | No | Reviewer notes |

**Response:**
```json
{"status": "ok", "doc_id": "550e8400-...", "decision": "approved", "notes": "Confirmed"}
```

**Errors:** 400 (not in review, invalid decision) | 404 (manifest not found)

---

### `GET /status/{doc_id}`

Get pipeline status of a document.

**Response:**
```json
{
    "doc_id": "550e8400-...",
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

### `GET /matters/{matter_id}`

List all documents in a matter.

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

### `GET /audit/{doc_id}`

Retrieve the full hash-chained audit trail with validity check.

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
        }
    ]
}
```

`chain_valid: false` indicates tampering or hash chain corruption.

---

### `GET /ops/status`

Pipeline-wide operational metrics.

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
| `stuck_documents` | Documents stale in processing/inbox >15min |
| `review_queue` | Documents awaiting human review |
| `error_rates` | Per-doc-type: total, failed, and review counts |

---

### `POST /ops/sweep`

Run a **one-off Boss ops-monitor sweep on demand** (same logic as the scheduled `pipeline/ops_monitor.py`). Gathers metrics, runs Boss analysis, and pauses ingestion if recommended (`ops_monitor_paused` flag).

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

---

### `POST /ops/resume`

Clear the `ops_monitor_paused` flag so the watcher resumes processing. Takes effect without a restart.

**Response:**
```json
{"status": "ok", "was_paused": true, "paused_ingestion": false}
```

---

## Error Responses

```json
{"detail": "Error message"}
```

| Status | Meaning |
|---|---|
| 400 | Bad request |
| 404 | Not found |
| 500 | Internal / DB unavailable |

---

## API Versioning

The Mailroom API is currently **unversioned**. All endpoints are served from the root path (`/`) without a version prefix. This is acceptable while the API is internal and pre-1.0, but the following conventions apply:

### Versioning policy

| Concern | Policy |
|---|---|
| **Current status** | Unversioned (pre-1.0), internal use only |
| **Version prefix** | Planned: `/v1/` when the first breaking change ships |
| **Backwards compatibility** | Breaking changes are grouped into a single release; the old route set is deprecated for one minor release before removal |
| **Response evolution** | Additive fields in JSON responses are allowed within a version (consumers must ignore unknown fields) |
| **Removal of fields** | Always a breaking change → new version |
| **Content type** | `application/json` only |

### Guidance for API consumers

- Treat the API as unstable: pin to the Mailroom release you integrate against (see `CHANGELOG.md`).
- Do not depend on undocumented response fields — only fields documented in this reference are stable.
- Breaking changes are announced in `CHANGELOG.md` under the "Breaking changes" section of the release.

### Planned `/v1/` layout

When versioning ships, routes will move under a prefix:

```
GET  /v1/health
POST /v1/upload
POST /v1/review/{doc_id}/resolve
GET  /v1/status/{doc_id}
GET  /v1/matters/{matter_id}
GET  /v1/audit/{doc_id}
GET  /v1/ops/status
```

The unversioned routes will continue to work during the deprecation window, then be removed.
