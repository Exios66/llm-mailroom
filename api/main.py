import structlog
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Form
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from pipeline.env import load_env

load_env()
from pipeline.env import default_environment

default_environment("live")

from pipeline.logging import setup_logging

setup_logging()

from graph.build_graph import build_graph, _ensure_dirs
from graph.state import DocumentState
from pipeline.bins import inbox_dir, save_manifest, load_manifest
from schemas.manifest import DocumentManifest, PipelineStage

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_dirs()
    yield


app = FastAPI(
    title="Mailroom API",
    description="Multi-Agent Legal Document Processing Pipeline",
    version="0.2.2",
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
    overall = "ok" if (llm["status"] == "ok" and db["status"] == "ok") else "degraded"
    return {
        "status": overall,
        "service": "mailroom",
        "checks": {"llm_provider": llm, "database": db},
    }


@app.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    matter_id: str = Form(default="DEFAULT"),
):
    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)

    file_path = inbox / file.filename
    content = await file.read()
    file_path.write_bytes(content)

    logger.info("file_uploaded", file=str(file_path), matter_id=matter_id, size=len(content))

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "file": file.filename,
            "matter_id": matter_id,
            "message": "File queued for processing — watcher will pick it up.",
        },
    )


@app.post("/review/{doc_id}/resolve")
async def resolve_review(
    doc_id: str,
    decision: str = Form(..., description="approved or rejected"),
    notes: str = Form(default=""),
):
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


@app.get("/status/{doc_id}")
async def get_document_status(doc_id: str):
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
        pass

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


@app.get("/matters/{matter_id}")
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


@app.get("/audit/{doc_id}")
async def get_audit_trail(doc_id: str):
    try:
        from storage.audit_log import get_audit_chain
        from schemas.audit import verify_chain
        records = await get_audit_chain(doc_id)
        from schemas.audit import AuditLogEntry
        entries = [
            AuditLogEntry(
                entry_id=r["entry_id"],
                doc_id=doc_id,
                matter_id="",
                event=r["event"],
                actor=r["actor"],
                detail=r["detail"],
                prev_hash=r["prev_hash"],
                entry_hash=r["entry_hash"],
            )
            for r in records
        ]
        chain_valid = verify_chain(entries)
        return {
            "doc_id": doc_id,
            "chain_length": len(records),
            "chain_valid": chain_valid,
            "entries": records,
        }
    except Exception:
        raise HTTPException(500, "Audit log unavailable")


@app.get("/ops/status")
async def ops_status():
    try:
        from storage.catalog import get_stuck_documents, get_error_rate_by_doc_type
        from storage.catalog import get_documents_by_stage
    except ImportError:
        raise HTTPException(500, "Database not available")

    stuck = await get_stuck_documents(stale_minutes=15)
    review_docs = await get_documents_by_stage("review")
    error_rates = await get_error_rate_by_doc_type()

    return {
        "stuck_documents": len(stuck),
        "review_queue": len(review_docs),
        "error_rates": error_rates,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }


@app.post("/ops/sweep")
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
            try:
                monitor._pause_file.parent.mkdir(parents=True, exist_ok=True)
                monitor._pause_file.write_text("1")
                logger.critical("ops_sweep_paused_ingestion")
            except Exception:
                logger.exception("ops_sweep_pause_file_failed")

    return {
        "status": "ok",
        "findings": findings.get("findings", []),
        "severity": findings.get("severity"),
        "recommended_action": findings.get("recommended_action"),
        "paused_ingestion": monitor.is_paused,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }


@app.post("/ops/resume")
async def ops_resume():
    """Clear the ingestion-pause flag so the watcher resumes processing.

    The ops monitor (scheduled sweep or `/ops/sweep`) can pause ingestion by
    writing `ops_monitor_paused`. This endpoint clears it — the watcher picks
    up the change on the next file event without a restart.
    """
    try:
        from pipeline.ops_monitor import OpsMonitor

        monitor = OpsMonitor()
        pause_file = monitor._pause_file
        if pause_file.exists():
            pause_file.unlink()
            logger.info("ops_resume_ingestion")
        return {
            "status": "ok",
            "was_paused": True,
            "paused_ingestion": monitor.is_paused,
        }
    except Exception as exc:
        logger.exception("ops_resume_failed")
        raise HTTPException(500, f"Ops resume failed: {exc}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
