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
from schemas.manifest import DocumentManifest, PipelineStage
from schemas.audit import build_audit_entry
from pipeline.bins import (
    inbox_dir,
    ensure_dirs,
    claim_file,
    move_to_classified,
    move_to_review,
    move_to_failed,
    move_to_archive,
    save_manifest,
    get_worker_id,
)
from pipeline.config import get_agent_config

logger = structlog.get_logger(__name__)


def _build_checkpointer():
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import os
        db_url = os.environ.get("DATABASE_URL_SYNC", os.environ.get("DATABASE_URL", ""))
        if db_url:
            checkpointer = PostgresSaver.from_conn_string(db_url)
            checkpointer.setup()
            logger.info("checkpointer_initialized", backend="postgres")
            return checkpointer
    except Exception:
        logger.warning("postgres_checkpointer_unavailable", fallback="memory")
    return MemorySaver()


def _ensure_dirs():
    ensure_dirs(
        inbox_dir(),
        Path(str(inbox_dir()).replace("/pipeline/inbox", "/pipeline/processing")),
        Path(str(inbox_dir()).replace("/pipeline/inbox", "/pipeline/classified")),
        Path(str(inbox_dir()).replace("/pipeline/inbox", "/pipeline/review")),
        Path(str(inbox_dir()).replace("/pipeline/inbox", "/pipeline/failed")),
        Path(str(inbox_dir()).replace("/pipeline/inbox", "") + "/archive"),
    )


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

    doc_text = ""
    try:
        doc_text = file_path.read_text(errors="replace")
    except Exception:
        try:
            doc_text = file_path.read_bytes().decode("utf-8", errors="replace")
        except Exception:
            doc_text = f"[Binary/unreadable file: {file_path.name}]"

    matter_id = state.get("matter_id", "DEFAULT")
    manifest = DocumentManifest(
        matter_id=matter_id,
        original_filename=file_path.name,
        stage=PipelineStage.PROCESSING,
    )
    manifest.touch()
    save_manifest(manifest)

    logger.info("ingest", doc_id=manifest.doc_id, file=file_path.name, chars=len(doc_text))
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
        "error_message": None,
    }


def classify_node(state: DocumentState) -> dict[str, Any]:
    from agents.sorter import SorterAgent

    sorter = SorterAgent()
    doc_text = state.get("doc_text", "")
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

    # Alternate prompt: include existing classification for context
    prev_type = state.get("doc_type", "")
    prev_confidence = state.get("classification_confidence", 0)
    augmented_text = (
        f"RE-EVALUATION REQUESTED — previous classification was '{prev_type}' with "
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

    specialists = {
        "contract": _extract_contracts,
        "corporate_record": _extract_corporate_records,
        "due_diligence": _extract_due_diligence,
        "correspondence": _extract_correspondence,
        "compliance_filing": _extract_compliance,
    }

    extractor = specialists.get(doc_type, lambda t: {"confidence": 0.3, "_unsupported": True})
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
        f"RE-EXTRACTION REQUESTED — previous extraction was low-confidence. "
        f"Please re-examine this document independently. Previous attempt found: {prev_extracted}\n\n"
        f"{doc_text[:25000]}"
    )

    specialists = {
        "contract": _extract_contracts,
        "corporate_record": _extract_corporate_records,
        "due_diligence": _extract_due_diligence,
        "correspondence": _extract_correspondence,
        "compliance_filing": _extract_compliance,
    }
    extractor = specialists.get(doc_type, lambda t: {"confidence": 0.3, "_unsupported": True})
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

    logger.info("human_review_required", doc_id=state.get("doc_id"), reason=esc_reason)

    if file_path_str:
        from schemas.manifest import DocumentManifest
        manifest = DocumentManifest(
            doc_id=state.get("doc_id", ""),
            matter_id=state.get("matter_id", "DEFAULT"),
            original_filename=state.get("original_filename", ""),
            stage=PipelineStage.REVIEW,
            doc_type=state.get("doc_type"),
            classification_confidence=state.get("classification_confidence"),
            extracted_data=state.get("extracted_data"),
            extraction_confidence=state.get("extraction_confidence"),
            escalation_reason=esc_reason,
        )
        move_to_review(Path(file_path_str), manifest)

    return {
        "stage": PipelineStage.REVIEW.value,
        "escalation_reason": esc_reason,
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
    from llm.client import get_llm, get_llm_model

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
    logger.info("catalog_write", doc_id=state.get("doc_id"))
    return {}


def archive_node(state: DocumentState) -> dict[str, Any]:
    from schemas.manifest import DocumentManifest
    from agents.archivist import archive_document
    from pipeline.bins import save_manifest as bins_save_manifest
    from schemas.audit import build_audit_entry

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
    )

    file_path = Path(state.get("file_path", ""))
    if not file_path.exists():
        processing_root = Path(str(inbox_dir()).replace("/pipeline/inbox", "/pipeline/processing"))
        candidates = list(processing_root.rglob(state.get("original_filename", "*.txt")))
        if candidates:
            file_path = candidates[0]

    archive_path, audit_entry = archive_document(manifest, file_path)

    try:
        from storage.audit_log import write_audit_entry
        write_audit_entry_sync(audit_entry)
    except Exception:
        logger.exception("audit_log_write_error")

    logger.info("pipeline_complete", doc_id=manifest.doc_id, archive=str(archive_path))
    return {
        "stage": PipelineStage.ARCHIVED.value,
    }


def write_audit_entry_sync(entry):
    try:
        from storage.audit_log import write_audit_entry
        import asyncio
        asyncio.get_event_loop().create_task(write_audit_entry(entry))
    except Exception:
        pass


def build_graph(checkpointer=None):
    if checkpointer is None:
        checkpointer = _build_checkpointer()

    workflow = StateGraph(DocumentState)

    workflow.add_node("ingest", ingest_node)
    workflow.add_node("classify", classify_node)
    workflow.add_node("retry_classify", retry_classify_node)
    workflow.add_node("extract", extract_node)
    workflow.add_node("retry_extract", retry_extract_node)
    workflow.add_node("human_review", human_review_node)
    workflow.add_node("boss_escalation", boss_escalation_node)
    workflow.add_node("compile_report", compile_report_node)
    workflow.add_node("catalog_write", catalog_write_node)
    workflow.add_node("archive", archive_node)

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

    result = graph.invoke(initial_state, config)
    return result
