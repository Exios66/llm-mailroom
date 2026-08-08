# API Reference

Mailroom exposes a FastAPI server on port 8000 by default.

## Starting the API

```bash
python api/main.py
# or
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

---

## Endpoints

### Health Check

```
GET /health
```

**Response:**
```json
{
    "status": "ok",
    "service": "mailroom"
}
```

---

### Upload Document

```
POST /upload
```

Upload a document to the pipeline inbox. The watcher will pick it up automatically.

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
    "matter_id": "MATTER-001",
    "message": "File queued for processing — watcher will pick it up."
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@contract.pdf" \
  -F "matter_id=MATTER-001"
```

---

### Resolve Human Review

```
POST /review/{doc_id}/resolve
```

Resolve a document that's been routed to human review.

**Path Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `doc_id` | string | Document ID from the manifest |

**Form Data:**
| Field | Type | Required | Description |
|---|---|---|---|
| `decision` | string | Yes | `approved` or `rejected` |
| `notes` | string | No | Reviewer notes |

**Response:**
```json
{
    "status": "ok",
    "doc_id": "550e8400-e29b-41d4-a716-446655440000",
    "decision": "approved",
    "notes": "Classification confirmed — proceed"
}
```

**Errors:**
- `400`: Document not in review stage
- `400`: Invalid decision value
- `404`: Manifest not found

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
