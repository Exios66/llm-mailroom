"""Human-review resolve dispositions for the REVIEW tray / The-Mailroom proxy.

Dispositions (The-Mailroom PR #18 / #20 contract):

- ``resume`` (default) — parked ``stage=review`` only. Approve re-extracts under
  the same ``doc_id``; reject moves to the failed bin.
- ``record`` — hash-chained audit + optional manifest note; file stays put
  (RECONSIDER / archived paper trail).
- ``requeue`` — copy the source file back to the inbox so the watcher
  resubmits a fresh run.

Optional ``doc_type`` / ``override_doc_type`` / subtype / subclass let an
operator *reroute* classification before a resume (written onto the parked
manifest) or stamp the inbox sidecar on requeue. Optional ``extracted_data``
with ``decision=approved`` and ``disposition=complete`` archives a
human-finished extraction without another LLM pass.

Parked-document viewer (PR #20): ``GET /documents/{doc_id}/source`` returns
extracted text JSON or ``?download=1`` original bytes.
"""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path
from typing import Any

import structlog

from schemas.manifest import DocumentManifest, PipelineStage

logger = structlog.get_logger(__name__)

DISPOSITIONS = frozenset({"resume", "record", "requeue", "complete"})
DECISIONS = frozenset({"approved", "rejected"})

# Cap parked-text pane size so REVIEW desks stay responsive on large PDFs.
SOURCE_TEXT_CAP = 200_000


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


def normalize_optional_str(value: Any) -> str | None:
    """Empty / whitespace → None (visualizer sends '' for 'keep current')."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_doc_type_override(
    *,
    override_doc_type: Any = None,
    doc_type: Any = None,
) -> str | None:
    """Prefer ``override_doc_type``; accept The-Mailroom ``doc_type`` alias."""
    return normalize_optional_str(override_doc_type) or normalize_optional_str(doc_type)


def apply_classification_override(
    manifest: DocumentManifest,
    *,
    override_doc_type: str | None = None,
    doc_type: str | None = None,
    contract_subtype: str | None = None,
    doc_subclass: str | None = None,
) -> dict[str, str]:
    """Reroute classification fields on the manifest before resume/complete.

    Returns the applied override dict (may be empty) for audit / sidecar /
    response ``class_override``. ``None`` means leave the field alone; an
    empty string clears it. Prefer ``override_doc_type``; accept The-Mailroom
    ``doc_type`` alias (PR #20).
    """
    applied: dict[str, str] = {}
    kind = resolve_doc_type_override(
        override_doc_type=override_doc_type, doc_type=doc_type
    )
    if kind:
        allowed = live_doc_types()
        if kind not in allowed:
            raise ValueError(
                f"doc_type must be one of {sorted(allowed)}; got {kind!r}"
            )
        manifest.doc_type = kind
        applied["doc_type"] = kind

    subclass_set = doc_subclass is not None
    subtype_set = contract_subtype is not None
    subclass = normalize_optional_str(doc_subclass) if subclass_set else None
    subtype = normalize_optional_str(contract_subtype) if subtype_set else None

    if subclass_set:
        manifest.doc_subclass = subclass
        if subclass:
            applied["doc_subclass"] = subclass

    if subtype_set:
        manifest.contract_subtype = subtype
        if subtype:
            applied["contract_subtype"] = subtype
    elif subclass and (kind or manifest.doc_type) == "contract":
        # Visualizer sends doc_subclass; mirror for contract extract routing.
        manifest.contract_subtype = subclass
        applied["contract_subtype"] = subclass

    return applied


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
    class_override: dict[str, str] | None = None,
) -> Path:
    """Copy ``source`` into the inbox (collision-safe). Does not move the original.

    When ``class_override`` is set (The-Mailroom PR #20), stamp those fields on
    the inbox ``.meta`` sidecar so operators can see the intended reroute on
    the requeued upload.
    """
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
    meta: dict[str, Any] = {
        "upload_id": f"requeue-{dest.stem[:8]}",
        "matter_id": matter_id or "DEFAULT",
        "original_filename": name,
        "note": "requeued_from_review",
    }
    if class_override:
        for key in ("doc_type", "doc_subclass", "contract_subtype"):
            if class_override.get(key):
                meta[key] = class_override[key]
    write_inbox_meta(dest, **meta)
    return dest


def guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    if ctype:
        if ctype.startswith("text/") and "charset" not in ctype:
            return f"{ctype}; charset=utf-8"
        return ctype
    return "application/octet-stream"


def read_document_source(
    manifest: DocumentManifest,
    *,
    max_chars: int = SOURCE_TEXT_CAP,
) -> dict[str, Any]:
    """Build the parked-document JSON payload for ``GET …/source``.

    Locates the on-disk file across bins, extracts text (PDF/image via the
    same helpers the pipeline uses), and truncates to ``max_chars``.
    """
    path = locate_document_file(manifest)
    if path is None:
        raise FileNotFoundError(
            f"Source file not found for doc_id={manifest.doc_id}"
        )
    size = path.stat().st_size
    content_type = guess_content_type(path)
    text, readable = _extract_source_text(path)
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "status": "ok",
        "doc_id": manifest.doc_id,
        "filename": path.name,
        "content_type": content_type,
        "text": text,
        "truncated": truncated,
        "bytes": size,
        "readable": bool(readable and text.strip()),
        "path": str(path),
    }


def _extract_source_text(path: Path) -> tuple[str, bool]:
    """Best-effort text for the REVIEW pane (never raises)."""
    ext = path.suffix.lower()
    if ext in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return text, bool(text.strip())
        except OSError:
            return "", False
    try:
        # Reuse pipeline transcription for PDF / image / docx.
        from graph.build_graph import _read_file_text

        return _read_file_text(path)
    except Exception:
        logger.exception("document_source_extract_failed", file=str(path))
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            return text, bool(text.strip())
        except OSError:
            return f"[Unreadable file: {path.name}]", False

def coerce_extracted_data(value: Any) -> dict[str, Any] | None:
    """Normalize operator ``extracted_data``. Empty / missing → ``None``.

    The visualizer Complete button often posts before the catalog probe fills
    the JSON field, or sends ``{}`` / a JSON string. Those must not 400 when
    the parked manifest already has extraction.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        import json as _json

        try:
            value = _json.loads(text)
        except Exception as exc:
            raise ValueError("extracted_data must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("extracted_data must be a JSON object")
    return value or None


def resolve_complete_extracted(submitted: Any, parked: Any = None) -> dict[str, Any]:
    """Pick operator ``extracted_data``, else the parked manifest payload."""
    chosen = coerce_extracted_data(submitted)
    if chosen:
        return chosen
    chosen = coerce_extracted_data(parked)
    if chosen:
        return chosen
    raise ValueError(
        "disposition=complete requires extracted_data object "
        "(none parked on this document)"
    )


_META_EXTRACT_KEYS = frozenset({"confidence", "reasoning", "mock_extraction"})


def specialist_schema_keys(doc_type: str) -> frozenset[str]:
    from schemas.documents import get_extraction_schema

    model = get_extraction_schema(doc_type)
    if model is None:
        return frozenset()
    return frozenset(model.model_fields)


def all_specialist_schema_keys() -> frozenset[str]:
    from schemas.documents import EXTRACTION_SCHEMAS

    names: set[str] = set()
    for model in EXTRACTION_SCHEMAS.values():
        names.update(model.model_fields)
    return frozenset(names) - _META_EXTRACT_KEYS


def validate_operator_extraction(doc_type: str, extracted: dict[str, Any]) -> dict[str, Any]:
    """Reject Complete payloads that do not match the parked document class.

    Extra keys that belong to another specialist schema (e.g. correspondence
    ``sender`` on a contract manifest) used to merge silently. Schema-invalid
    values also used to archive.
    """
    from observability.scores import validate_extraction

    payload = dict(extracted)
    allowed = specialist_schema_keys(doc_type) | _META_EXTRACT_KEYS
    foreign = sorted(
        key
        for key in payload
        if key not in allowed
        and not str(key).startswith("_")
        and key in all_specialist_schema_keys()
    )
    if foreign:
        raise ValueError(
            f"extracted_data fields {foreign} belong to another specialist, "
            f"not {doc_type}"
        )
    checks = validate_extraction(doc_type, payload)
    if not checks.get("schema_valid"):
        raise ValueError(f"extracted_data does not match the {doc_type} schema")
    return payload


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
    extracted_data = validate_operator_extraction(manifest.doc_type, extracted_data)

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
