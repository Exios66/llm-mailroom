import structlog
import hashlib
from pathlib import Path
from schemas.audit import AuditLogEntry, build_audit_entry
from pipeline.bins import move_to_archive, save_manifest

logger = structlog.get_logger(__name__)


def _file_sha256(path: Path) -> str:
    """Best-effort sha256 of the archived file (audit A-7)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        logger.warning("archive_sha256_failed", file=str(path))
        return ""


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

    # A-7: recompute the file hash at archive so post-archive substitution is
    # detectable — the digest is chained into the audit entry.
    archived_sha256 = _file_sha256(archive_path)

    audit_entry = build_audit_entry(
        doc_id=doc_id,
        matter_id=matter_id,
        event="archived",
        actor="archivist",
        detail={
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_path),
            "doc_type": doc_type,
            "file_sha256": archived_sha256,
            "classification_confidence": manifest.classification_confidence,
            "extraction_confidence": manifest.extraction_confidence,
        },
        prev_hash=prev_audit_hash,
    )

    logger.info("archived", doc_id=doc_id, archive_path=str(archive_path))
    return archive_path, audit_entry
