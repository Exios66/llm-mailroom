"""Human-review resolve dispositions for the REVIEW tray / The-Mailroom proxy.

Dispositions (The-Mailroom PR #18 contract):

- ``resume`` (default) — parked ``stage=review`` only. Approve re-extracts under
  the same ``doc_id``; reject moves to the failed bin.
- ``record`` — hash-chained audit + optional manifest note; file stays put
  (RECONSIDER / archived paper trail).
- ``requeue`` — copy the source file back to the inbox so the watcher
  resubmits a fresh run.

Optional ``override_doc_type`` / subtype / subclass let an operator *reroute*
classification before a resume. Optional ``extracted_data`` with
``decision=approved`` and ``disposition=complete`` archives a human-finished
extraction without another LLM pass.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import structlog

from schemas.manifest import DocumentManifest, PipelineStage

logger = structlog.get_logger(__name__)

DISPOSITIONS = frozenset({"resume", "record", "requeue", "complete"})
DECISIONS = frozenset({"approved", "rejected"})


def live_doc_types() -> set[str]:
    from pipeline.config import load_config

    return {c["key"] for c in load_config().get("doc_classes", []) if c.get("key")}


def serialize_document(doc) -> dict[str, Any]:
    """Catalog or manifest → JSON-safe lookup/tray payload."""
    if doc is None:
        return {}
    stage = getattr(doc, "stage", None)
    if hasattr(stage, "value"):
        stage = stage.value
    updated = getattr(doc, "updated_at", None)
    created = getattr(doc, "created_at", None)
    return {
        "doc_id": getattr(doc, "doc_id", None),
        "matter_id": getattr(doc, "matter_id", None),
        "original_filename": getattr(doc, "original_filename", None),
        "stage": stage,
        "doc_type": getattr(doc, "doc_type", None),
        "contract_subtype": getattr(doc, "contract_subtype", None),
        "doc_subclass": getattr(doc, "doc_subclass", None),
        "classification_confidence": getattr(doc, "classification_confidence", None),
        "extraction_confidence": getattr(doc, "extraction_confidence", None),
        "escalation_reason": getattr(doc, "escalation_reason", None),
        "trace_id": getattr(doc, "trace_id", None),
        "review_decision": getattr(doc, "review_decision", None),
        "extracted_data": getattr(doc, "extracted_data", None),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def tray_actions_for(stage: str | None) -> list[dict[str, str]]:
    """Document which REVIEW-tray actions are available for a given stage."""
    stage = (stage or "").lower()
    actions = [
        {
            "disposition": "record",
            "decisions": "approved|rejected",
            "when": "Any stage — audit paper trail; file stays put",
        },
        {
            "disposition": "requeue",
            "decisions": "approved|rejected",
            "when": "Source file locatable — copy back to inbox for a fresh run",
        },
    ]
    if stage == "review":
        actions.insert(
            0,
            {
                "disposition": "resume",
                "decisions": "approved|rejected",
                "when": "Parked review — approve re-extracts; reject → failed bin",
            },
        )
        actions.append(
            {
                "disposition": "complete",
                "decisions": "approved",
                "when": "Parked review with human extracted_data — archive without LLM",
            },
        )
    return actions


def apply_classification_override(
    manifest: DocumentManifest,
    *,
    override_doc_type: str | None = None,
    contract_subtype: str | None = None,
    doc_subclass: str | None = None,
) -> DocumentManifest:
    """Reroute classification fields on the manifest before resume/complete."""
    if override_doc_type:
        allowed = live_doc_types()
        if override_doc_type not in allowed:
            raise ValueError(
                f"override_doc_type must be one of {sorted(allowed)}; "
                f"got {override_doc_type!r}"
            )
        manifest.doc_type = override_doc_type
    if contract_subtype is not None:
        manifest.contract_subtype = contract_subtype or None
    if doc_subclass is not None:
        manifest.doc_subclass = doc_subclass or None
    return manifest


def locate_document_file(manifest: DocumentManifest) -> Path | None:
    """Best-effort locate the on-disk file for a manifest across bins."""
    from pipeline.bins import (
        archive_dir,
        failed_dir,
        processing_dir,
        review_dir,
    )

    name = manifest.original_filename
    if not name:
        return None
    candidates: list[Path] = [
        review_dir() / name,
        failed_dir() / name,
    ]
    if manifest.doc_type:
        arch = archive_dir(manifest.matter_id, manifest.doc_type)
        candidates.append(arch / name)
        stem, suffix = Path(name).stem, Path(name).suffix
        candidates.append(arch / f"{stem}--{manifest.doc_id}{suffix}")
    proc = processing_dir()
    if proc.exists():
        for worker in proc.iterdir():
            if worker.is_dir():
                candidates.append(worker / name)
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def copy_to_inbox(
    source: Path,
    *,
    preferred_name: str | None = None,
    matter_id: str = "DEFAULT",
) -> Path:
    """Copy ``source`` into the inbox (collision-safe). Does not move the original."""
    from pipeline.bins import inbox_dir, write_inbox_meta

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    name = preferred_name or source.name
    dest = inbox / name
    if dest.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        n = 1
        while dest.exists():
            dest = inbox / f"{stem}-requeue-{n}{suffix}"
            n += 1
    shutil.copy2(str(source), str(dest))
    write_inbox_meta(
        dest,
        upload_id=f"requeue-{dest.stem[:8]}",
        matter_id=matter_id or "DEFAULT",
        original_filename=name,
        note="requeued_from_review",
    )
    return dest

def complete_human_extraction(
    manifest: DocumentManifest,
    review_file: Path,
    extracted_data: dict[str, Any],
) -> dict[str, Any]:
    """Archive a parked review with operator-supplied extraction (no LLM).

    Filesystem + manifest only — the API awaits the catalog upsert separately
    so we never deadlock the FastAPI event loop.
    """
    from pipeline.bins import move_to_archive, save_manifest

    if not isinstance(extracted_data, dict) or not extracted_data:
        raise ValueError("extracted_data must be a non-empty object for disposition=complete")
    if not manifest.doc_type:
        raise ValueError("Document has no classification to complete; set override_doc_type")

    conf = extracted_data.get("confidence")
    try:
        extraction_confidence = float(conf) if conf is not None else 1.0
    except (TypeError, ValueError):
        extraction_confidence = 1.0

    manifest.extracted_data = extracted_data
    manifest.extraction_confidence = extraction_confidence
    manifest.review_decision = "approved"
    manifest.stage = PipelineStage.ARCHIVED
    manifest.escalation_reason = None
    manifest.touch()
    save_manifest(manifest)

    archived = move_to_archive(
        review_file, manifest.matter_id, manifest.doc_type, doc_id=manifest.doc_id
    )

    logger.info(
        "review_completed_by_human",
        doc_id=manifest.doc_id,
        archive=str(archived),
    )
    return {
        "stage": "archived",
        "doc_type": manifest.doc_type,
        "extraction_confidence": extraction_confidence,
        "extraction_attempts": manifest.extraction_attempts,
        "extracted_data": extracted_data,
        "archive_path": str(archived),
        "doc_record": {
            "doc_id": manifest.doc_id,
            "matter_id": manifest.matter_id,
            "original_filename": manifest.original_filename,
            "doc_type": manifest.doc_type,
            "contract_subtype": manifest.contract_subtype,
            "doc_subclass": getattr(manifest, "doc_subclass", None),
            "stage": PipelineStage.ARCHIVED.value,
            "classification_confidence": manifest.classification_confidence,
            "extraction_confidence": extraction_confidence,
            "extracted_data": extracted_data,
            "escalation_reason": None,
            "trace_id": manifest.trace_id,
        },
    }
