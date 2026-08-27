import os
import re
import uuid
import datetime
import structlog
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Form, Request, Depends
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from pipeline.env import load_env

load_env()
from pipeline.env import default_environment

default_environment("live")

from pipeline.logging import setup_logging

setup_logging()

# O-1: kick the score-config warm-up off the document path at startup.
from observability.scores import warmup_score_configs
from observability.tracing import install_on_dropped

install_on_dropped()  # O-3: dropped trace events log a warning, never vanish

warmup_score_configs(blocking=False)
from observability.field_scoring import warm_embedding_model

warm_embedding_model(blocking=False)  # O-10: load embeddings off the document path

from graph.build_graph import build_graph, _ensure_dirs
from graph.state import DocumentState
from pipeline.bins import inbox_dir, save_manifest, load_manifest
from schemas.manifest import DocumentManifest, PipelineStage

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Auth (audit L-2): every endpoint except /health requires the configured
# bearer token (MAILROOM_API_TOKEN). When unset the API refuses to start in
# server mode — it must never bind unauthenticated.
# ---------------------------------------------------------------------------

_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_API_TOKEN = os.environ.get("MAILROOM_API_TOKEN", "").strip()

# Upload guardrails (audit L-18): default 50 MB cap, 20 uploads/min burst.
MAX_UPLOAD_BYTES = int(os.environ.get("MAILROOM_MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
_UPLOAD_WINDOW_SECONDS = 60
_UPLOAD_MAX_PER_WINDOW = int(os.environ.get("MAILROOM_UPLOAD_RATE", 20))
_upload_timestamps: list[float] = []


def _require_token(request: Request) -> None:
    """Dependency: reject requests without the bearer token (audit L-2)."""
    if not _API_TOKEN:
        return  # token disabled — see server bind note below; loopback-only default
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {_API_TOKEN}":
        raise HTTPException(401, "Missing or invalid API token")


def _rate_limit_upload() -> None:
    """Sliding-window rate limit for /upload (audit L-18)."""
    import time

    now = time.monotonic()
    _upload_timestamps[:] = [t for t in _upload_timestamps if now - t < _UPLOAD_WINDOW_SECONDS]
    if len(_upload_timestamps) >= _UPLOAD_MAX_PER_WINDOW:
        raise HTTPException(429, "Upload rate limit exceeded — try again shortly")
    _upload_timestamps.append(now)


_embedded_watcher = None


def _embed_watcher_running() -> bool:
    w = _embedded_watcher
    return bool(w is not None and getattr(w, "_running", False))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _embedded_watcher
    _ensure_dirs()
    watcher = None
    from pipeline.watcher import Watcher, WatcherLockHeld, embed_watcher_enabled

    if embed_watcher_enabled():
        watcher = Watcher()
        try:
            watcher.start()
            _embedded_watcher = watcher
            logger.info("embedded_watcher_started", worker_id=watcher.worker_id)
        except WatcherLockHeld as exc:
            logger.warning("embedded_watcher_skipped", reason=str(exc))
            watcher = None
            _embedded_watcher = None
    try:
        yield
    finally:
        _embedded_watcher = None
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                logger.exception("embedded_watcher_stop_failed")


app = FastAPI(
    title="Mailroom API",
    description="Multi-Agent Legal Document Processing Pipeline",
    version="0.3.0",
    lifespan=lifespan,
)


async def _check_llm_provider() -> dict:
    """Best-effort LLM provider connectivity check.

    Resolves the provider for the sorter agent (fails fast if the API key is
    missing or is the mock placeholder) and pings the models endpoint with a
    short timeout. Never spends completion tokens.
    """
    import os
    try:
        from llm.providers import resolve_provider
        from pipeline.config import get_agent_config

        agent_cfg = get_agent_config("sorter")
        provider, model = resolve_provider(agent_cfg)
        status = "ok"
        detail = f"{provider.name}:{model}"
        try:
            from openai import OpenAI

            kwargs = {"base_url": provider.base_url, "api_key": "not-needed", "timeout": 5.0}
            if provider.api_key_env:
                key = os.environ.get(provider.api_key_env)
                if key:
                    kwargs["api_key"] = key
            client = OpenAI(**kwargs)
            client.models.list()
        except Exception as exc:
            status = "degraded"
            detail = f"{provider.name}:{model} — models endpoint unreachable: {type(exc).__name__}"
        return {"status": status, "detail": detail, "provider": provider.name}
    except Exception as exc:
        return {
            "status": "degraded",
            "detail": f"provider resolution failed: {type(exc).__name__}: {exc}",
            "provider": None,
        }


async def _check_database() -> dict:
    """Best-effort database connectivity check (SQLite or Postgres)."""
    try:
        from storage.db import check_connectivity

        ok = await check_connectivity()
        return {"status": "ok" if ok else "degraded", "detail": "database reachable" if ok else "database unreachable"}
    except Exception as exc:
        return {"status": "degraded", "detail": f"database check failed: {type(exc).__name__}"}


@app.get("/health")
async def health():
    llm = await _check_llm_provider()
    db = await _check_database()
    from pipeline.bins import is_ingestion_paused, inbox_dir, get_pause_info
    from pipeline.bins import watcher_heartbeat_age, watcher_lamp, count_inbox_pending
    from observability.tracing import flush_health

    paused = is_ingestion_paused()
    tracing_health = flush_health()
    heartbeat_age = watcher_heartbeat_age()
    lamp = watcher_lamp(heartbeat_age)
    overall = "ok" if (llm["status"] == "ok" and db["status"] == "ok") else "degraded"
    if paused or not tracing_health["healthy"]:
        overall = "degraded"
    # Uploads sit in the inbox until the watcher drains them — a missing or
    # stale watcher is an operational outage, not an informational lamp.
    if lamp in ("stale", "missing"):
        overall = "degraded"
    return {
        "status": overall,
        "service": "mailroom",
        "checks": {
            "llm_provider": llm,
            "database": db,
            "ingestion_paused": paused,
            "pause_info": get_pause_info(),
            "watcher": lamp,
            "watcher_embedded": _embed_watcher_running(),
            "inbox_pending": count_inbox_pending(),
            "watcher_heartbeat_seconds_ago": heartbeat_age,
            "observability": tracing_health,
        },
    }


@app.post("/upload", dependencies=[Depends(_require_token)])
async def upload_document(
    file: UploadFile = File(...),
    matter_id: str = Form(default="DEFAULT"),
):
    from pipeline.config import load_config
    from pipeline.bins import is_ingestion_paused, write_inbox_meta

    # Pause gate (audit L-18): refuse new work while ingestion is paused.
    if is_ingestion_paused():
        raise HTTPException(503, "Ingestion is paused by the ops monitor — try again later")

    _rate_limit_upload()

    ext = Path(file.filename or "").suffix.lower()
    accepted = load_config().get("file_extensions", [".txt", ".pdf", ".docx", ".md"])
    if not ext or ext not in accepted:
        raise HTTPException(
            400,
            f"Unsupported file type '{ext}'. Accepted: {', '.join(accepted)}",
        )

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)

    # Avoid clobbering a document with the same name (the watcher keys claims
    # by file name): write to a uniquified name when a collision exists.
    dest = inbox / file.filename
    if dest.exists():
        stem, suffix = Path(file.filename).stem, Path(file.filename).suffix
        counter = 1
        while dest.exists():
            dest = inbox / f"{stem}-{counter}{suffix}"
            counter += 1

    # Size cap (audit L-18): read with a bound so a huge upload cannot exhaust
    # memory/disk before validation.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
        )
    dest.write_bytes(content)

    # Persist the upload metadata (matter_id, tracking id, ...) as a `<file>.meta`
    # sidecar so the watcher files the document under the submitted matter and
    # the upload is trackable via `GET /queue`. Best-effort: a failed sidecar
    # write must not fail the upload (the watcher falls back to filename-derived
    # matter inference).
    upload_id = uuid.uuid4().hex[:12]
    write_inbox_meta(
        dest,
        upload_id=upload_id,
        matter_id=matter_id,
        uploaded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        size=len(content),
        original_filename=file.filename,
    )

    logger.info("file_uploaded", file=str(dest), matter_id=matter_id, upload_id=upload_id, size=len(content))

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "file": dest.name,
            "upload_id": upload_id,
            "matter_id": matter_id,
            "message": "File queued for processing — the API-embedded watcher (or a dedicated watcher process) will pick it up.",
        },
    )


@app.get("/queue", dependencies=[Depends(_require_token)])
async def get_queue():
    """Live view of the inbox → processing queue.

    The inbox bin IS the queue: files land there via `/upload` (or a direct
    drop) and the always-on watcher claims and processes them. This endpoint
    shows what is queued (with the `/upload` metadata, incl. the upload_id
    used for tracking), what is currently being processed, and the most recent
    terminal documents — so a specific upload is observable from accepted →
    archived/review/failed without digging through logs.
    """
    from pipeline.bins import (
        inbox_dir,
        processing_dir,
        accepted_extensions,
        read_inbox_meta,
    )

    queued = []
    inbox = inbox_dir()
    if inbox.exists():
        for p in sorted(inbox.iterdir()):
            if not p.is_file() or p.suffix.lower() not in accepted_extensions():
                continue
            meta = read_inbox_meta(p) or {}
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            queued.append(
                {
                    "file": p.name,
                    "size": size,
                    "upload_id": meta.get("upload_id"),
                    "matter_id": meta.get("matter_id", "DEFAULT"),
                    "uploaded_at": meta.get("uploaded_at"),
                }
            )

    processing = []
    proc_root = processing_dir()
    if proc_root.exists():
        for worker_dir in sorted(proc_root.iterdir()):
            if not worker_dir.is_dir():
                continue
            for p in sorted(worker_dir.iterdir()):
                if p.is_file():
                    processing.append({"file": p.name, "worker": worker_dir.name})

    recent = []
    try:
        from storage.catalog import get_recent_documents

        for d in await get_recent_documents(limit=20):
            recent.append(
                {
                    "doc_id": d.doc_id,
                    "file": d.original_filename,
                    "matter_id": d.matter_id,
                    "stage": d.stage,
                    "doc_type": d.doc_type,
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                }
            )
    except Exception:
        logger.exception("queue_recent_fetch_failed")

    return {
        "queued": queued,
        "queued_count": len(queued),
        "processing": processing,
        "processing_count": len(processing),
        "recent": recent,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _validate_doc_id(doc_id: str) -> str:
    """Reject unvalidated identifiers before they reach manifest paths (L-21)."""
    if not _DOC_ID_RE.match(doc_id or ""):
        raise HTTPException(400, "doc_id must match [A-Za-z0-9_-]")
    return doc_id


@app.post("/review/{doc_id}/resolve", dependencies=[Depends(_require_token)])
async def resolve_review(
    doc_id: str,
    decision: str = Form(..., description="approved or rejected"),
    notes: str = Form(default=""),
):
    _validate_doc_id(doc_id)
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")

    manifest = load_manifest(doc_id)
    if not manifest:
        raise HTTPException(404, f"Manifest not found for doc_id: {doc_id}")

    if manifest.stage != PipelineStage.REVIEW:
        raise HTTPException(400, f"Document is not in review (current stage: {manifest.stage})")

    if decision == "rejected":
        manifest.review_decision = "rejected"
        manifest.stage = PipelineStage.FAILED
        manifest.touch()
        save_manifest(manifest)
        _move_rejected_to_failed(doc_id, manifest)
        _write_review_audit_entry(doc_id, manifest.matter_id, "review_rejected", notes)
        logger.info("review_rejected", doc_id=doc_id)
        return {"status": "ok", "doc_id": doc_id, "decision": decision, "notes": notes}

    # Approved → resume the pipeline with a FRESH extraction (never reuse the
    # reviewed extraction data). Stateless: re-invoke the graph from the
    # manifest, starting at the extraction stage, then compile → catalog →
    # archive under the original doc_id.
    if not manifest.doc_type:
        raise HTTPException(
            409,
            "Document has no classification to resume; re-submit it to the inbox instead.",
        )

    import asyncio
    from pipeline.bins import review_dir
    from graph.build_graph import resume_from_review

    review_file = review_dir() / manifest.original_filename
    if not review_file.exists():
        raise HTTPException(404, f"File not found in review bin: {review_file}")

    try:
        result = await asyncio.to_thread(resume_from_review, manifest, review_file)
    except Exception as exc:
        logger.exception("review_resume_failed", doc_id=doc_id)
        raise HTTPException(500, f"Resume failed: {exc}")

    _write_review_audit_entry(
        doc_id,
        manifest.matter_id,
        "review_approved",
        notes,
        detail={"resumed_stage": result.get("stage")},
    )
    logger.info("review_approved_resumed", doc_id=doc_id, stage=result.get("stage"))
    return {
        "status": "ok",
        "doc_id": doc_id,
        "decision": decision,
        "notes": notes,
        "resume": {
            "stage": result.get("stage"),
            "doc_type": result.get("doc_type"),
            "extraction_confidence": result.get("extraction_confidence"),
            "extraction_attempts": result.get("extraction_attempts"),
        },
    }


def _write_review_audit_entry(
    doc_id: str, matter_id: str, event: str, notes: str, detail: dict | None = None
) -> None:
    """Append a hash-chained audit entry for a human review decision.

    The review decision is part of the document's compliance record: it must
    be chained to the previous entry for this doc_id (best-effort; the audit
    log is the durable record, but a DB failure must not fail the API call).
    """
    try:
        import asyncio
        from schemas.audit import build_audit_entry
        from graph.build_graph import _latest_audit_hash, _write_audit_log

        entry_detail = dict(detail or {})
        if notes:
            entry_detail["notes"] = notes
        entry = build_audit_entry(
            doc_id=doc_id,
            matter_id=matter_id,
            event=event,
            actor="human_reviewer",
            detail=entry_detail,
            prev_hash=_latest_audit_hash(doc_id),
        )
        _write_audit_log(entry)
    except Exception:
        logger.exception("review_audit_entry_failed", doc_id=doc_id, event=event)


def _move_rejected_to_failed(doc_id: str, manifest) -> None:
    """Close the conveyor loop for a rejected review: move the file from the
    review bin to the failed bin and flip the catalog record to failed.

    Without this the manifest says failed while the file stays in review/ and
    the catalog row stays `review` — inflating review_queue/ops stats forever.
    Best-effort: never fails the API call.
    """
    try:
        from pipeline.bins import review_dir, move_to_failed
        from storage.catalog import write_document_record

        review_file = review_dir() / manifest.original_filename
        if review_file.exists():
            move_to_failed(review_file)

        doc_record = {
            "doc_id": doc_id,
            "matter_id": manifest.matter_id,
            "original_filename": manifest.original_filename,
            "doc_type": manifest.doc_type,
            "contract_subtype": manifest.contract_subtype,
            "doc_subclass": getattr(manifest, "doc_subclass", None),
            "stage": PipelineStage.FAILED.value,
            "classification_confidence": manifest.classification_confidence,
            "extraction_confidence": manifest.extraction_confidence,
            "extracted_data": manifest.extracted_data,
            "escalation_reason": manifest.escalation_reason,
            "trace_id": manifest.trace_id,
        }

        async def _write():
            await write_document_record(doc_record)

        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(_write(), loop)
                future.result(timeout=5)
            else:
                asyncio.run(_write())
        except RuntimeError:
            asyncio.run(_write())
        logger.info("review_rejected_finalized", doc_id=doc_id)
    except Exception:
        logger.exception("review_rejected_finalize_failed", doc_id=doc_id)


@app.get("/status/{doc_id}", dependencies=[Depends(_require_token)])
async def get_document_status(doc_id: str):
    _validate_doc_id(doc_id)
    manifest = load_manifest(doc_id)

    try:
        from storage.catalog import get_document
        doc = await get_document(doc_id)
        if doc:
            return {
                "doc_id": doc.doc_id,
                "matter_id": doc.matter_id,
                "stage": doc.stage,
                "doc_type": doc.doc_type,
                "classification_confidence": doc.classification_confidence,
                "extraction_confidence": doc.extraction_confidence,
                "escalation_reason": doc.escalation_reason,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            }
    except Exception:
        logger.exception("catalog_read_failed", doc_id=doc_id)

    if manifest:
        return {
            "doc_id": manifest.doc_id,
            "matter_id": manifest.matter_id,
            "stage": manifest.stage.value,
            "doc_type": manifest.doc_type,
            "classification_confidence": manifest.classification_confidence,
            "extraction_confidence": manifest.extraction_confidence,
            "escalation_reason": manifest.escalation_reason,
            "created_at": manifest.created_at.isoformat(),
            "updated_at": manifest.updated_at.isoformat(),
        }

    raise HTTPException(404, f"Document not found: {doc_id}")


@app.get("/matters/{matter_id}", dependencies=[Depends(_require_token)])
async def get_matter(matter_id: str):
    try:
        from storage.catalog import get_matter_documents
    except ImportError:
        raise HTTPException(500, "Database not available")

    docs = await get_matter_documents(matter_id)
    return {
        "matter_id": matter_id,
        "document_count": len(docs),
        "documents": [
            {
                "doc_id": d.doc_id,
                "original_filename": d.original_filename,
                "doc_type": d.doc_type,
                "stage": d.stage,
                "classification_confidence": d.classification_confidence,
                "extraction_confidence": d.extraction_confidence,
            }
            for d in docs
        ],
    }


@app.get("/audit/{doc_id}", dependencies=[Depends(_require_token)])
async def get_audit_trail(doc_id: str):
    _validate_doc_id(doc_id)
    try:
        from storage.audit_log import get_audit_chain
        from schemas.audit import verify_chain
        records = await get_audit_chain(doc_id)
        from schemas.audit import AuditLogEntry
        entries = [
            AuditLogEntry(
                entry_id=r["entry_id"],
                doc_id=doc_id,
                matter_id=r.get("matter_id") or "",
                event=r["event"],
                actor=r["actor"],
                detail=r["detail"],
                prev_hash=r["prev_hash"],
                entry_hash=r["entry_hash"],
                timestamp=r["timestamp"],
            )
            for r in records
        ]
        chain_valid = verify_chain(entries)
        return {
            "doc_id": doc_id,
            "chain_length": len(records),
            "chain_valid": chain_valid,
            "entries": [
                {**r, "timestamp": r["timestamp"].isoformat() if hasattr(r["timestamp"], "isoformat") else r["timestamp"]}
                for r in records
            ],
        }
    except Exception:
        raise HTTPException(500, "Audit log unavailable")


@app.get("/ops/status", dependencies=[Depends(_require_token)])
async def ops_status():
    try:
        from storage.catalog import get_stuck_documents, get_error_rate_by_doc_type
        from storage.catalog import get_documents_by_stage
    except ImportError:
        raise HTTPException(500, "Database not available")

    stuck = await get_stuck_documents(stale_minutes=15)
    review_docs = await get_documents_by_stage("review")
    error_rates = await get_error_rate_by_doc_type()
    try:
        from storage.catalog import count_first_pass_throughput

        throughput = await count_first_pass_throughput()
    except Exception:
        throughput = {"archived": 0, "first_pass": 0, "first_pass_rate": None}

    from pipeline.bins import is_ingestion_paused, get_pause_info
    from observability.tracing import flush_health

    return {
        "stuck_documents": len(stuck),
        "review_queue": len(review_docs),
        "error_rates": error_rates,
        "archived": throughput.get("archived", 0),
        "first_pass": throughput.get("first_pass", 0),
        "first_pass_rate": throughput.get("first_pass_rate"),
        "ingestion_paused": is_ingestion_paused(),
        "pause_info": get_pause_info(),
        "observability": flush_health(),
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }


@app.post("/ops/sweep", dependencies=[Depends(_require_token)])
async def ops_sweep():
    """Run a one-off Boss ops-monitor sweep on demand.

    Mirrors the scheduled `pipeline/ops_monitor.py` sweep (gather metrics →
    Boss analysis) without waiting for the interval. Pauses ingestion if the
    Boss recommends `pause_ingestion` (writes the `ops_monitor_paused` flag,
    which the watcher honors).
    """
    try:
        from pipeline.ops_monitor import OpsMonitor

        monitor = OpsMonitor()
        metrics = await monitor._gather_metrics()
        findings = await monitor._analyze_metrics(metrics)
    except Exception as exc:
        logger.exception("ops_sweep_failed")
        raise HTTPException(500, f"Ops sweep failed: {exc}")

    if findings.get("recommended_action") in ("alert", "pause_ingestion"):
        if findings.get("recommended_action") == "pause_ingestion":
            from pipeline.bins import set_ingestion_paused

            ok = set_ingestion_paused(
                actor="api_ops_sweep",
                reason="; ".join(findings.get("findings", [])[:3]) or "Boss recommended pause",
            )
            if ok:
                logger.critical("ops_sweep_paused_ingestion")
            else:
                logger.error("ops_sweep_pause_write_failed")

    return {
        "status": "ok",
        "findings": findings.get("findings", []),
        "severity": findings.get("severity"),
        "recommended_action": findings.get("recommended_action"),
        "paused_ingestion": monitor.is_paused,
        "pause_info": monitor.pause_info,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }


@app.post("/ops/resume", dependencies=[Depends(_require_token)])
async def ops_resume():
    """Clear the ingestion-pause flag so the watcher resumes processing.

    The ops monitor (scheduled sweep or `/ops/sweep`) can pause ingestion by
    writing `ops_monitor_paused` (JSON with actor/reason/TTL). This endpoint
    clears it — the watcher picks up the change on the next file event
    without a restart.
    """
    try:
        from pipeline.bins import clear_ingestion_paused, get_pause_info

        was_paused = get_pause_info() is not None
        clear_ingestion_paused()
        if was_paused:
            logger.info("ops_resume_ingestion")
        return {
            "status": "ok",
            "was_paused": was_paused,
            "paused_ingestion": get_pause_info() is not None,
        }
    except Exception as exc:
        logger.exception("ops_resume_failed")
        raise HTTPException(500, f"Ops resume failed: {exc}")


def _mount_v1_aliases() -> None:
    """Expose the documented /v1 layout as aliases of the live handlers.

    Unversioned routes stay registered for the deprecation window. Both
    prefixes share the same functions, auth, and status codes.
    """
    from fastapi.routing import APIRoute

    wanted = {
        "/health",
        "/upload",
        "/queue",
        "/review/{doc_id}/resolve",
        "/status/{doc_id}",
        "/matters/{matter_id}",
        "/audit/{doc_id}",
        "/ops/status",
        "/ops/sweep",
        "/ops/resume",
    }
    for route in list(app.router.routes):
        if not isinstance(route, APIRoute) or route.path not in wanted:
            continue
        methods = sorted(m for m in route.methods if m not in {"HEAD"})
        app.add_api_route(
            "/v1" + route.path,
            route.endpoint,
            methods=methods,
            dependencies=route.dependencies,
            status_code=route.status_code,
            name=f"v1_{route.name}",
            tags=["v1"],
        )


_mount_v1_aliases()


if __name__ == "__main__":
    import signal
    import uvicorn
    from observability.tracing import ensure_process_tracing

    # Audit L-2: bind loopback by default; allow explicit MAILROOM_API_HOST
    # override. When binding non-loopback, a bearer token is mandatory.
    host = os.environ.get("MAILROOM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MAILROOM_API_PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not _API_TOKEN:
        raise SystemExit(
            "Refusing to bind to a non-loopback address without MAILROOM_API_TOKEN "
            "(audit L-2: unauthenticated API exposure)."
        )
    ensure_process_tracing()  # O-7: drop-warnings + flush/shutdown on exit
    uvicorn.run(app, host=host, port=port)
