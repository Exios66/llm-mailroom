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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mailroom"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
