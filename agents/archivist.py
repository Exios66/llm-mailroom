import structlog
from pathlib import Path
from schemas.audit import AuditLogEntry, build_audit_entry
from pipeline.bins import move_to_archive, save_manifest

logger = structlog.get_logger(__name__)


def archive_document(
    manifest,
    file_path: Path,
    prev_audit_hash: str = "",
) -> tuple[Path, AuditLogEntry]:
    matter_id = manifest.matter_id
    doc_type = manifest.doc_type or "unknown"
    doc_id = manifest.doc_id

    logger.info("archiving", doc_id=doc_id, matter_id=matter_id, doc_type=doc_type)

    archive_path = move_to_archive(file_path, matter_id, doc_type)

    manifest_path = save_manifest(manifest)

    audit_entry = build_audit_entry(
        doc_id=doc_id,
        matter_id=matter_id,
        event="archived",
        actor="archivist",
        detail={
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_path),
            "doc_type": doc_type,
            "classification_confidence": manifest.classification_confidence,
            "extraction_confidence": manifest.extraction_confidence,
        },
        prev_hash=prev_audit_hash,
    )

    logger.info("archived", doc_id=doc_id, archive_path=str(archive_path))
    return archive_path, audit_entry
