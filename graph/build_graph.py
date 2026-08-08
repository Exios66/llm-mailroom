import structlog
from pathlib import Path
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from graph.state import DocumentState
from graph.routing import (
    after_classify,
    after_retry_classify,
    after_extraction,
    after_retry_extraction,
    after_boss,
    after_human_review,
)
from observability.tracing import pipeline_trace, traced_node
from schemas.manifest import DocumentManifest, PipelineStage
from pipeline.bins import (
    inbox_dir,
    processing_dir,
    classified_dir,
    review_dir,
    failed_dir,
    archive_dir,
    manifests_dir,
    ensure_dirs,
    claim_file,
    move_to_review,
    move_to_failed,
    move_to_archive,
    save_manifest,
    get_worker_id,
)

logger = structlog.get_logger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | {".txt", ".md", ".docx"}


def _build_checkpointer():
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        from pipeline.bins import get_base_dir

        db_path = get_base_dir() / "checkpoints.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        try:
            checkpointer.setup()
        except Exception:
            pass
        logger.info("checkpointer_initialized", backend="sqlite", path=str(db_path))
        return checkpointer
    except Exception:
        logger.warning("sqlite_checkpointer_unavailable", fallback="memory")
    return MemorySaver()


def _ensure_dirs():
    ensure_dirs(
        inbox_dir(),
        processing_dir(),
        classified_dir(),
        review_dir(),
        failed_dir(),
        archive_dir(),
        manifests_dir(),
    )


def _read_file_text(file_path: Path) -> tuple[str, bool]:
    ext = file_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return _extract_text_from_image(file_path)
    if ext in PDF_EXTENSIONS:
        return _extract_text_from_pdf(file_path)
    try:
        text = file_path.read_text(errors="replace")
        if not text.strip():
            return ("", False)
        return (text, True)
    except Exception:
        try:
            text = file_path.read_bytes().decode("utf-8", errors="replace")
            return (text, bool(text.strip()))
        except Exception:
            return (f"[Unreadable file: {file_path.name}]", False)


def _extract_text_from_image(file_path: Path) -> tuple[str, bool]:
    logger.info("image_detected", file=str(file_path))
    try:
        from agents.image_extractor import ImageExtractor
        extractor = ImageExtractor()
        result = extractor.extract(file_path)
        text = result.get("text", "")
        if text:
            logger.info("image_extracted", file=file_path.name, chars=len(text))
            return (text, True)
    except Exception:
        logger.exception("image_extraction_failed", file=str(file_path))
    return (f"[Image file: {file_path.name} — text extraction failed]", False)


def _extract_text_from_pdf(file_path: Path) -> tuple[str, bool]:
    logger.info("pdf_detected", file=str(file_path))
    try:
        from agents.pdf_transcriber import PDFTranscriber
        transcriber = PDFTranscriber()
        result = transcriber.transcribe(file_path)
        text = result.get("markdown", "") or result.get("text", "")
        if text:
            logger.info("pdf_transcribed", file=file_path.name, chars=len(text))
            return (text, True)
    except Exception:
        logger.exception("pdf_transcription_failed", file=str(file_path))
    try:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            pass
        subprocess.run(["pdftotext", str(file_path), tmp.name], capture_output=True, timeout=30)
        text = Path(tmp.name).read_text(errors="replace")
        Path(tmp.name).unlink(missing_ok=True)
        if text.strip():
            logger.info("pdf_fallback_text", chars=len(text))
            return (text, True)
    except Exception:
        logger.exception("pdf_fallback_failed")
    return (f"[PDF file: {file_path.name} — transcription failed]", False)


def _build_specialist_dispatch():
    from pipeline.config import load_config
    cfg = load_config()
    doc_classes = cfg.get("doc_classes", [])
    dispatch = {}
    for cls in doc_classes:
        key = cls["key"]
        spec_name = cls.get("specialist", "")
        if spec_name == "contracts_specialist":
            dispatch[key] = _extract_contracts
        elif spec_name == "corporate_records_specialist":
            dispatch[key] = _extract_corporate_records
        elif spec_name == "due_diligence_specialist":
            dispatch[key] = _extract_due_diligence
        elif spec_name == "correspondence_specialist":
            dispatch[key] = _extract_correspondence
        elif spec_name == "compliance_specialist":
            dispatch[key] = _extract_compliance
    return dispatch


def ingest_node(state: DocumentState) -> dict[str, Any]:
    _ensure_dirs()
    worker_id = get_worker_id()

    if state.get("file_path"):
        file_path = Path(state["file_path"])
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
    else:
        inbox = inbox_dir()
        files = list(inbox.glob("*"))
        if not files:
            raise RuntimeError("No files in inbox")
        file_path = files[0]

    if str(inbox_dir()) in str(file_path):
        file_path = claim_file(file_path, worker_id)

    doc_text, text_ok = _read_file_text(file_path)

    matter_id = state.get("matter_id", "DEFAULT")
    manifest = DocumentManifest(
        matter_id=matter_id,
        original_filename=file_path.name,
        stage=PipelineStage.PROCESSING,
    )
    manifest.touch()
    save_manifest(manifest)

    logger.info("ingest", doc_id=manifest.doc_id, file=file_path.name, chars=len(doc_text), suffix=file_path.suffix)
    return {
        "doc_id": manifest.doc_id,
        "matter_id": matter_id,
        "original_filename": file_path.name,
        "stage": PipelineStage.PROCESSING.value,
        "file_path": str(file_path),
        "doc_text": doc_text,
        "classification_attempts": 0,
        "extraction_attempts": 0,
        "retry_count": 0,
        "conflict_detected": False,
        "error_message": None if text_ok else f"Could not extract text from {file_path.suffix} file",
    }


def classify_node(state: DocumentState) -> dict[str, Any]:
    doc_text = state.get("doc_text", "")
    if not doc_text or not doc_text.strip():
        logger.warning("empty_doc_text_classify", doc_id=state.get("doc_id"))
        return {
            "doc_type": "correspondence",
            "classification_confidence": 0.1,
            "classification_attempts": state.get("classification_attempts", 0) + 1,
            "stage": PipelineStage.CLASSIFIED.value,
            "escalation_reason": "Empty or unreadable document content",
        }

    from agents.sorter import SorterAgent
    sorter = SorterAgent()
    doc_type, confidence, reasoning = sorter.classify(doc_text)
    attempts = state.get("classification_attempts", 0) + 1

    logger.info("classified", doc_type=doc_type, confidence=confidence, attempts=attempts)
    return {
        "doc_type": doc_type,
        "classification_confidence": confidence,
        "classification_attempts": attempts,
        "stage": PipelineStage.CLASSIFIED.value,
        "escalation_reason": reasoning if confidence < 0.7 else None,
    }


def retry_classify_node(state: DocumentState) -> dict[str, Any]:
    from agents.sorter import SorterAgent

    sorter = SorterAgent()
    doc_text = state.get("doc_text", "")
    attempts = state.get("classification_attempts", 0) + 1

    prev_type = state.get("doc_type", "")
    prev_confidence = state.get("classification_confidence", 0)
    augmented_text = (
        f"RE-EVALUATION REQUESTED - previous classification was '{prev_type}' with "
        f"confidence {prev_confidence:.2f}. Please re-examine this document independently:\n\n"
        f"{doc_text[:12000]}"
    )
    doc_type, confidence, reasoning = sorter.classify(augmented_text)

    logger.info("retry_classified", doc_type=doc_type, confidence=confidence, attempts=attempts)
    return {
        "doc_type": doc_type,
        "classification_confidence": confidence,
        "classification_attempts": attempts,
        "retry_count": state.get("retry_count", 0) + 1,
        "stage": PipelineStage.CLASSIFIED.value,
        "escalation_reason": reasoning if confidence < 0.7 else None,
    }


def extract_node(state: DocumentState) -> dict[str, Any]:
    doc_type = state.get("doc_type", "")
    doc_text = state.get("doc_text", "")

    dispatch = _build_specialist_dispatch()
    extractor = dispatch.get(doc_type, lambda t: {"confidence": 0.3, "_unsupported": True})
    result = extractor(doc_text)
    confidence = result.pop("confidence", None)
    attempts = state.get("extraction_attempts", 0) + 1

    logger.info("extracted", doc_type=doc_type, confidence=confidence, attempts=attempts)
    return {
        "extracted_data": result,
        "extraction_confidence": confidence,
        "extraction_attempts": attempts,
    }


def _extract_contracts(doc_text: str) -> dict:
    from agents.contracts_specialist import ContractsSpecialist
    return ContractsSpecialist().extract(doc_text)


def _extract_corporate_records(doc_text: str) -> dict:
    from agents.corporate_records_specialist import CorporateRecordsSpecialist
    return CorporateRecordsSpecialist().extract(doc_text)


def _extract_due_diligence(doc_text: str) -> dict:
    from agents.due_diligence_specialist import DueDiligenceSpecialist
    return DueDiligenceSpecialist().extract(doc_text)


def _extract_correspondence(doc_text: str) -> dict:
    from agents.correspondence_specialist import CorrespondenceSpecialist
    return CorrespondenceSpecialist().extract(doc_text)


def _extract_compliance(doc_text: str) -> dict:
    from agents.compliance_specialist import ComplianceSpecialist
    return ComplianceSpecialist().extract(doc_text)


def retry_extract_node(state: DocumentState) -> dict[str, Any]:
    doc_type = state.get("doc_type", "")
    doc_text = state.get("doc_text", "")
    prev_extracted = state.get("extracted_data", {})
    attempts = state.get("extraction_attempts", 0) + 1

    augmented_text = (
        f"RE-EXTRACTION REQUESTED - previous extraction was low-confidence. "
        f"Please re-examine this document independently. Previous attempt found: {prev_extracted}\n\n"
        f"{doc_text[:25000]}"
    )

    dispatch = _build_specialist_dispatch()
    extractor = dispatch.get(doc_type, lambda t: {"confidence": 0.3, "_unsupported": True})
    result = extractor(augmented_text)
    confidence = result.pop("confidence", None)

    logger.info("retry_extracted", doc_type=doc_type, confidence=confidence, attempts=attempts)
    return {
        "extracted_data": result,
        "extraction_confidence": confidence,
        "extraction_attempts": attempts,
        "retry_count": state.get("retry_count", 0) + 1,
    }


def human_review_node(state: DocumentState) -> dict[str, Any]:
    file_path_str = state.get("file_path", "")
    esc_reason = state.get("escalation_reason", "Unknown reason")
    doc_id = state.get("doc_id", "")

    logger.info("human_review_required", doc_id=doc_id, reason=esc_reason)

    if file_path_str:
        manifest = DocumentManifest(
            doc_id=doc_id,
            matter_id=state.get("matter_id", "DEFAULT"),
            original_filename=state.get("original_filename", ""),
            stage=PipelineStage.REVIEW,
            doc_type=state.get("doc_type"),
            classification_confidence=state.get("classification_confidence"),
            extracted_data=state.get("extracted_data"),
            extraction_confidence=state.get("extraction_confidence"),
            escalation_reason=esc_reason,
            trace_id=state.get("trace_id"),
            classification_attempts=state.get("classification_attempts", 0),
            extraction_attempts=state.get("extraction_attempts", 0),
        )
        move_to_review(Path(file_path_str), manifest)

    return {
        "stage": PipelineStage.REVIEW.value,
        "escalation_reason": esc_reason,
        "review_decision": "rejected",
    }


def boss_escalation_node(state: DocumentState) -> dict[str, Any]:
    from agents.boss import BossAgent

    boss = BossAgent()
    manifest_data = {
        "doc_id": state.get("doc_id"),
        "doc_type": state.get("doc_type"),
        "classification_confidence": state.get("classification_confidence"),
        "extraction_confidence": state.get("extraction_confidence"),
        "extracted_data": state.get("extracted_data"),
        "escalation_reason": state.get("escalation_reason"),
    }

    result = boss.adjudicate(manifest_data)
    decision = result.get("decision", "review")
    reasoning = result.get("reasoning", "")

    logger.info("boss_decision", doc_id=state.get("doc_id"), decision=decision)
    return {
        "review_decision": decision,
        "escalation_reason": f"Boss: {reasoning}",
    }


def compile_report_node(state: DocumentState) -> dict[str, Any]:
    from llm.client import get_llm

    manifest_data = {
        "doc_id": state.get("doc_id"),
        "matter_id": state.get("matter_id"),
        "doc_type": state.get("doc_type"),
        "classification_confidence": state.get("classification_confidence"),
        "extraction_confidence": state.get("extraction_confidence"),
        "extracted_data": state.get("extracted_data"),
    }

    llm_client, model = get_llm("reporter")
    from agents.reporter import compile_matter_record
    report = compile_matter_record(manifest_data, llm_client, model)

    logger.info("report_compiled", doc_id=state.get("doc_id"))
    return {
        "extracted_data": {
            **state.get("extracted_data", {}),
            "_report": report,
        },
    }


def catalog_write_node(state: DocumentState) -> dict[str, Any]:
    doc_id = state.get("doc_id", "")
    logger.info("catalog_write", doc_id=doc_id)
    try:
        import asyncio
        from schemas.matter import Matter
        from storage.catalog import write_document_record as _write_doc, write_matter_record as _write_matter

        matter = Matter(
            matter_id=state.get("matter_id", ""),
            name=state.get("matter_id", "DEFAULT"),
            client_name="auto-created",
            practice_area="transactional",
        )

        doc_record = {
            "doc_id": state["doc_id"],
            "matter_id": state["matter_id"],
            "original_filename": state["original_filename"],
            "doc_type": state.get("doc_type", "unknown"),
            "stage": state.get("stage", "cataloged"),
            "classification_confidence": state.get("classification_confidence"),
            "extraction_confidence": state.get("extraction_confidence"),
            "extracted_data": state.get("extracted_data"),
            "escalation_reason": state.get("escalation_reason"),
            "trace_id": state.get("trace_id"),
        }

        def _sync_write():
            async def _runner():
                await _write_matter(matter)
                await _write_doc(doc_record)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    future = asyncio.run_coroutine_threadsafe(_runner(), loop)
                    future.result(timeout=5)
                else:
                    asyncio.run(_runner())
            except RuntimeError:
                asyncio.run(_runner())

        _sync_write()
        logger.info("catalog_written", doc_id=doc_id)
    except Exception:
        logger.exception("catalog_write_error")
    return {}


def archive_node(state: DocumentState) -> dict[str, Any]:
    manifest = DocumentManifest(
        doc_id=state.get("doc_id", ""),
        matter_id=state.get("matter_id", "DEFAULT"),
        original_filename=state.get("original_filename", ""),
        stage=PipelineStage.ARCHIVED,
        doc_type=state.get("doc_type", "unknown"),
        classification_confidence=state.get("classification_confidence"),
        extracted_data=state.get("extracted_data"),
        extraction_confidence=state.get("extraction_confidence"),
        trace_id=state.get("trace_id"),
        escalation_reason=state.get("escalation_reason"),
        classification_attempts=state.get("classification_attempts", 0),
        extraction_attempts=state.get("extraction_attempts", 0),
    )

    file_path_str = state.get("file_path", "")
    if not file_path_str:
        logger.error("archive_no_file_path", doc_id=manifest.doc_id)
        return {"stage": PipelineStage.FAILED.value, "error_message": "No file path in state"}

    file_path = Path(file_path_str)
    if not file_path.exists():
        processing_root = processing_dir()
        candidates = list(processing_root.rglob(state.get("original_filename", "*.txt")))
        if candidates:
            file_path = candidates[0]
        else:
            logger.error("archive_file_not_found", doc_id=manifest.doc_id, path=file_path_str)
            return {"stage": PipelineStage.FAILED.value, "error_message": f"File not found: {file_path_str}"}

    from agents.archivist import archive_document
    archive_path, audit_entry = archive_document(manifest, file_path)

    _write_audit_log(audit_entry)

    logger.info("pipeline_complete", doc_id=manifest.doc_id, archive=str(archive_path))
    return {"stage": PipelineStage.ARCHIVED.value}


def _write_audit_log(entry):
    try:
        import asyncio
        from storage.audit_log import write_audit_entry

        async def _write():
            await write_audit_entry(entry)

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
        logger.info("audit_entry_written", entry_id=entry.entry_id, event_name=entry.event)
    except Exception:
        logger.exception("audit_log_write_error")


def _persist_scores(state: dict, scores: dict):
    if not scores or not state.get("doc_id"):
        return
    try:
        import asyncio
        from storage.catalog import update_document_scores

        async def _write():
            await update_document_scores(state["doc_id"], scores)

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
    except Exception:
        logger.exception("scores_persist_error")


def build_graph(checkpointer=None):
    if checkpointer is None:
        checkpointer = _build_checkpointer()

    workflow = StateGraph(DocumentState)

    # Node names stay stable (best practice); per-run values go in metadata.
    workflow.add_node("ingest", traced_node("ingest-document")(ingest_node))
    workflow.add_node("classify", traced_node("classify-document")(classify_node))
    workflow.add_node("retry_classify", traced_node("classify-document")(retry_classify_node))
    workflow.add_node("extract", traced_node("extract-fields")(extract_node))
    workflow.add_node("retry_extract", traced_node("extract-fields")(retry_extract_node))
    workflow.add_node("human_review", traced_node("route-for-review")(human_review_node))
    workflow.add_node("boss_escalation", traced_node("adjudicate-conflict")(boss_escalation_node))
    workflow.add_node("compile_report", traced_node("compile-report")(compile_report_node))
    workflow.add_node("catalog_write", traced_node("write-catalog")(catalog_write_node))
    workflow.add_node("archive", traced_node("archive-document")(archive_node))

    workflow.add_edge(START, "ingest")
    workflow.add_edge("ingest", "classify")

    workflow.add_conditional_edges("classify", after_classify, {
        "retry_classify": "retry_classify",
        "extract": "extract",
        "human_review": "human_review",
    })

    workflow.add_conditional_edges("retry_classify", after_retry_classify, {
        "extract": "extract",
        "human_review": "human_review",
    })

    workflow.add_conditional_edges("extract", after_extraction, {
        "retry_extract": "retry_extract",
        "compile_report": "compile_report",
        "human_review": "human_review",
        "boss_escalation": "boss_escalation",
    })

    workflow.add_conditional_edges("retry_extract", after_retry_extraction, {
        "compile_report": "compile_report",
        "human_review": "human_review",
        "boss_escalation": "boss_escalation",
    })

    workflow.add_conditional_edges("boss_escalation", after_boss, {
        "compile_report": "compile_report",
        "human_review": "human_review",
    })

    workflow.add_conditional_edges("human_review", after_human_review, {
        "compile_report": "compile_report",
        "failed": END,
    })

    workflow.add_edge("compile_report", "catalog_write")
    workflow.add_edge("catalog_write", "archive")
    workflow.add_edge("archive", END)

    return workflow.compile(checkpointer=checkpointer)


def run_pipeline(file_path: Path, matter_id: str = "DEFAULT") -> dict[str, Any]:
    _ensure_dirs()
    graph = build_graph()
    config = {"configurable": {"thread_id": file_path.stem}}

    initial_state: DocumentState = {
        "doc_id": "",
        "matter_id": matter_id,
        "original_filename": file_path.name,
        "stage": "inbox",
        "doc_type": None,
        "classification_confidence": None,
        "classification_attempts": 0,
        "extracted_data": None,
        "extraction_confidence": None,
        "extraction_attempts": 0,
        "trace_id": None,
        "escalation_reason": None,
        "review_decision": None,
        "retry_count": 0,
        "conflict_detected": False,
        "file_path": str(file_path),
        "doc_text": "",
        "error_message": None,
        "messages": [],
    }

    import os
    from observability import tracing

    from observability import scores as pipeline_scores

    pipeline_scores.ensure_score_configs()

    with tracing.pipeline_trace(
        seed=file_path.stem,  # deterministic trace id -> correlates with our doc
        session_id=matter_id,  # groups every document of a matter into one session
        name="document-pipeline",
        input={"filename": file_path.name, "matter_id": matter_id},
        metadata={"pipeline": "mailroom"},
        tags=["mailroom"],
        environment=os.environ.get("OBSERVABILITY_ENVIRONMENT") or None,
    ) as root:
        try:
            result = graph.invoke(initial_state, config)
        except Exception:
            if root is not None:
                root.update(output={"stage": "failed", "error": True})
            raise
        score_values = pipeline_scores.emit_pipeline_scores(result)
        _persist_scores(result, score_values)
        if root is not None:
            root.update(output={
                "stage": result.get("stage"),
                "doc_type": result.get("doc_type"),
                "classification_confidence": result.get("classification_confidence"),
                "extraction_confidence": result.get("extraction_confidence"),
            })
    tracing.flush()
    return result
