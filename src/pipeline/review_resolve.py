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

Human decision as data (``resolution``): a parked document is resolved with one
of ``approve_as_is`` | ``correct`` | ``reclassify`` | ``reject``. The decision
is planned here (:func:`plan_resolution`: validation, merge of
``field_corrections`` over the parked fields, the ``human_label`` record with a
snapshot of what the model said BEFORE the override) and applied in the graph
(``graph.build_graph.resume_from_review``). Legacy ``decision`` + ``disposition``
requests map onto the same four resolutions (see :func:`plan_resolution`).

Parked-document viewer (PR #20): ``GET /documents/{doc_id}/source`` returns
extracted text JSON or ``?download=1`` original bytes.
"""

from __future__ import annotations

import contextlib
import mimetypes
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


REVIEW_EVIDENCE_FIELDS = (
    "review_causes",
    "review_verdict",
    "reviewer_doc_type",
    "reviewer_doc_subclass",
    "reviewer_confidence",
    "reviewer_model",
)


def _review_evidence(doc) -> dict[str, Any]:
    """Lane A reviewer evidence: manifest attributes, else the catalog's
    ``extracted_data["_review"]`` block. Absent evidence reads as None."""
    extracted = getattr(doc, "extracted_data", None)
    block = extracted.get("_review") if isinstance(extracted, dict) else None
    if not isinstance(block, dict):
        block = {}
    evidence: dict[str, Any] = {}
    for name in REVIEW_EVIDENCE_FIELDS:
        value = getattr(doc, name, None)
        evidence[name] = value if value is not None else block.get(name)
    return evidence


def _review_entered_at(doc) -> Any:
    """When the document entered the review queue (ISO text), else None.

    The manifest attribute, else the catalog's ``extracted_data["_review"]``
    block. Unlike ``updated_at`` it is not reset by a later note or catalog write.
    """
    value = getattr(doc, "review_entered_at", None)
    if value is None:
        extracted = getattr(doc, "extracted_data", None)
        block = extracted.get("_review") if isinstance(extracted, dict) else None
        value = block.get("review_entered_at") if isinstance(block, dict) else None
    return value.isoformat() if hasattr(value, "isoformat") else value


def with_review_evidence(extracted: Any, manifest) -> Any:
    """Catalog ``extracted_data`` carrying the manifest's reviewer evidence.

    Archive, operator completion, rejection and run aborts replace the row's
    parked payload, so ``_review`` is copied forward. Without evidence
    ``extracted`` is returned unchanged.
    """
    evidence = {name: getattr(manifest, name, None) for name in REVIEW_EVIDENCE_FIELDS}
    if all(value is None for value in evidence.values()):
        return extracted
    return {**(extracted if isinstance(extracted, dict) else {}), "_review": evidence}


def serialize_document(doc) -> dict[str, Any]:
    """Catalog or manifest → JSON-safe lookup/tray payload.

    Reviewer evidence comes from attributes or the catalog's ``_review``
    block. A missing document returns an empty dict.
    """
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
        **_review_evidence(doc),
        "review_entered_at": _review_entered_at(doc),
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
        if kind != manifest.doc_type:
            # These belong to the old class; a person chose this one. An
            # explicit subclass or subtype below replaces them.
            manifest.classification_confidence = None
            manifest.doc_subclass = None
            manifest.contract_subtype = None
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
        archived_file_path,
        failed_file_path,
        processing_file_path,
        review_file_path,
    )

    name = manifest.original_filename
    if not name:
        return None
    candidates: list[Path] = [
        review_file_path(manifest),
    ]
    candidates.append(failed_file_path(manifest))
    if manifest.doc_type:
        candidates.append(archived_file_path(manifest))
    processing = processing_file_path(manifest)
    if processing is not None:
        candidates.append(processing)
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

    Not used by ``POST /review/{doc_id}/resolve`` any more: ``disposition=complete``
    now archives through the graph tail (``human_review -> compile_report``), so
    the report, scores and catalog row exist. Kept for direct callers.

    Filesystem + manifest only — the API awaits the catalog upsert separately
    so we never deadlock the FastAPI event loop.
    """
    from pipeline.bins import move_to_archive, save_manifest

    if not isinstance(extracted_data, dict) or not extracted_data:
        raise ValueError("extracted_data must be a non-empty object for disposition=complete")
    if not manifest.doc_type:
        raise ValueError("Document has no classification to complete; set override_doc_type")
    # Reviewer evidence comes only from manifest fields; an operator-supplied
    # ``_review`` would masquerade as it in lookup and queue payloads.
    extracted_data = {k: v for k, v in extracted_data.items() if k != "_review"}
    if not extracted_data:
        raise ValueError("extracted_data must be a non-empty object for disposition=complete")
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
            "extracted_data": with_review_evidence(extracted_data, manifest),
            "escalation_reason": None,
            "trace_id": manifest.trace_id,
        },
    }


# ---------------------------------------------------------------------------
# Human decision as data: resolutions, the human_label record, the resolve lock
# ---------------------------------------------------------------------------

RESOLUTIONS = frozenset({"approve_as_is", "correct", "reclassify", "reject"})
# Enum-ish reasons for a rejection; free text goes in ``notes``.
REJECT_REASONS = (
    "not_a_legal_document",
    "duplicate",
    "unreadable",
    "out_of_scope",
    "wrong_matter",
    "spam",
    "other",
)
DEFAULT_REVIEWER = "human_reviewer"
MAX_REVIEWER_LEN = 120


class ResolveInProgress(RuntimeError):
    """Another resolve for the same document is already running."""


_inflight: set[str] = set()
_inflight_guard = threading.Lock()


@contextlib.contextmanager
def doc_resolve_lock(doc_id: str):
    """Per-document, process-level exclusion around a resolve.

    NON-blocking: the resolve endpoint runs on the event loop, so a second
    resolve for the same document is refused (HTTP 409 from this lock), never
    queued behind the first. A resolve that arrives after the first already
    finished falls to the stage re-check inside the lock, which answers the
    legacy HTTP 400 naming the current stage. Process-level: run one API
    worker per data directory.
    """
    with _inflight_guard:
        if doc_id in _inflight:
            raise ResolveInProgress(
                f"A resolve for document {doc_id} is already in progress"
            )
        _inflight.add(doc_id)
    try:
        yield
    finally:
        with _inflight_guard:
            _inflight.discard(doc_id)


def stage_name(stage: Any) -> str:
    """Enum or string stage as its plain value (``archived``, not ``PipelineStage.ARCHIVED``)."""
    return str(getattr(stage, "value", stage))


def clean_extraction(data: Any) -> dict[str, Any]:
    """Model/human fields without internal ``_``-prefixed bookkeeping keys."""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def normalize_reviewer(value: Any, fallback: Any = None) -> str:
    """Reviewer identity: explicit value, else ``fallback`` (the ``X-Reviewer``
    header; API tokens carry no label in this repo), else ``human_reviewer``."""
    for candidate in (value, fallback):
        text = normalize_optional_str(candidate)
        if text:
            cleaned = re.sub(r"[\x00-\x1f\x7f]", "", text)[:MAX_REVIEWER_LEN]
            if cleaned:
                return cleaned
    return DEFAULT_REVIEWER


def normalize_reject_reason(value: Any) -> str | None:
    text = normalize_optional_str(value)
    if text is None:
        return None
    token = re.sub(r"[\s-]+", "_", text.lower())
    if token not in REJECT_REASONS:
        raise ValueError(f"reject_reason must be one of {list(REJECT_REASONS)}; got {text!r}")
    return token


def coerce_field_corrections(value: Any) -> dict[str, Any]:
    """Normalize ``field_corrections`` (object, JSON string, empty) to a dict."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        import json as _json

        try:
            value = _json.loads(value)
        except Exception as exc:
            raise ValueError("field_corrections must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("field_corrections must be a JSON object")
    return clean_extraction(value)


def model_snapshot(manifest: DocumentManifest) -> dict[str, Any]:
    """What the model said, captured BEFORE any override or merge.

    A manifest that parked again holding a reviewer's fields (``extraction_source``
    is ``human``, e.g. the report failed after a correction) no longer holds the
    model's output: its first resolution's snapshot is the record of that, so it
    is carried forward instead of snapshotting the human's fields as the model's.
    """
    label = getattr(manifest, "human_label", None)
    earlier = label.get("model_snapshot") if isinstance(label, dict) else None
    if getattr(manifest, "extraction_source", None) == "human" and isinstance(earlier, dict) and earlier:
        return dict(earlier)
    return {
        "doc_type": manifest.doc_type,
        "contract_subtype": manifest.contract_subtype,
        "doc_subclass": manifest.doc_subclass,
        "classification_confidence": manifest.classification_confidence,
        "extraction_confidence": manifest.extraction_confidence,
        "extracted_data": clean_extraction(manifest.extracted_data) or None,
        # clean_extraction drops the "_parse_error" marker; keep what it said.
        "parse_error": bool(
            isinstance(manifest.extracted_data, dict) and manifest.extracted_data.get("_parse_error")
        ),
        "reviewer_doc_type": manifest.reviewer_doc_type,
        "reviewer_doc_subclass": manifest.reviewer_doc_subclass,
        "reviewer_confidence": manifest.reviewer_confidence,
    }


@dataclass
class ResolutionPlan:
    """A validated human decision, ready to apply (nothing has been mutated).

    ``route``: ``archive`` (the reviewer's fields through compile_report ->
    catalog_write -> archive, no LLM), ``reextract`` (fresh extraction under the
    manifest's class) or ``failed``.
    """

    decision: str
    resolution: str
    route: str
    legacy: bool
    reviewer: str
    notes: str
    label: dict[str, Any]
    extracted_data: dict[str, Any] | None = None
    field_corrections: dict[str, Any] = field(default_factory=dict)
    reject_reason: str | None = None

    def payload(self) -> dict[str, Any]:
        """The structured ``Command(resume=...)`` payload for ``human_review_node``."""
        out: dict[str, Any] = {
            "decision": self.decision,
            "resolution": self.resolution,
            "notes": self.notes,
            "reviewer": self.reviewer,
            "human_label": self.label,
        }
        if self.route == "archive":
            out["extracted_data"] = self.extracted_data
        if self.reject_reason:
            out["reject_reason"] = self.reject_reason
        return out


def plan_resolution(
    *,
    before: DocumentManifest,
    manifest: DocumentManifest,
    class_override: dict[str, str] | None,
    decision: str,
    disposition: str = "resume",
    resolution: str | None = None,
    notes: str = "",
    submitted_extracted: Any = None,
    field_corrections: Any = None,
    reviewer: Any = None,
    reject_reason: Any = None,
) -> ResolutionPlan:
    """Validate a human decision and build the ``human_label``. Raises ValueError (HTTP 400).

    ``before`` is the manifest as parked (the model's output); ``manifest`` has
    the classification override applied. Resolutions:

    - ``approve_as_is``: archive the parked fields unchanged (validated), no LLM.
      With no parked extraction (sorter-stage park) it means "class confirmed"
      and re-enters extraction.
    - ``correct``: ``field_corrections`` merged over the parked fields,
      validated, archived with no LLM. Cannot change the class.
    - ``reclassify``: a class override is required; re-extracts under it.
    - ``reject``: manifest FAILED, file to ``failed/``; ``reject_reason`` required.

    Legacy ``decision``/``disposition`` requests (no ``resolution``) map as:
    approved+resume -> re-extract (recorded ``reclassify`` if the class was
    overridden, else ``approve_as_is``); rejected+resume -> ``reject``
    (``reject_reason`` optional); approved+complete -> archive the posted
    ``extracted_data`` (a FULL replacement, recorded ``correct`` with the changed
    keys as ``field_corrections``) or, with none posted, the parked fields
    (``approve_as_is``).
    """
    class_override = dict(class_override or {})
    reviewer_id = normalize_reviewer(reviewer)
    corrections_in = coerce_field_corrections(field_corrections)
    reason = normalize_reject_reason(reject_reason)
    # Parked fields are what the human reviews and rules on: an approve_as_is /
    # correct decision ARCHIVES them (plus corrections) without any
    # re-extraction call — including when the model's extraction failed to
    # JSON-parse (the snapshot below records parse_error=True for the record).
    parked = clean_extraction(manifest.extracted_data)
    class_changed = (
        bool(class_override.get("doc_type")) and class_override["doc_type"] != before.doc_type
    )
    # Subtype / subclass moved (or were cleared: a clear is not in class_override)
    # while the doc_type stayed. The parked fields were extracted for the old ones.
    details_changed = not class_changed and (
        manifest.contract_subtype != before.contract_subtype
        or manifest.doc_subclass != before.doc_subclass
    )
    classification_changed = class_changed or details_changed
    legacy = not resolution

    route = "reextract"
    extracted: dict[str, Any] | None = None
    corrections: dict[str, Any] = {}
    removed: list[str] = []

    if legacy:
        disposition = (disposition or "resume").lower()
        if decision == "rejected":
            if disposition == "complete":
                raise ValueError("disposition=complete requires decision=approved")
            resolution, route = "reject", "failed"
        elif disposition == "complete":
            extracted = clean_extraction(
                resolve_complete_extracted(submitted_extracted, manifest.extracted_data)
            )
            if not extracted:
                raise ValueError("extracted_data must be a non-empty object for disposition=complete")
            if not manifest.doc_type:
                raise ValueError("Document has no classification to complete; set override_doc_type")
            extracted = validate_operator_extraction(manifest.doc_type, extracted)
            corrections = {k: v for k, v in extracted.items() if k not in parked or parked[k] != v}
            # The posted object is a FULL replacement: a parked field it leaves out is gone.
            removed = sorted(
                k for k in parked if k not in extracted and k not in _META_EXTRACT_KEYS
            )
            resolution = "correct" if (corrections or removed or class_changed) else "approve_as_is"
            route = "archive"
        else:
            resolution = "reclassify" if classification_changed else "approve_as_is"
    else:
        resolution = str(resolution).strip().lower()
        if resolution not in RESOLUTIONS:
            raise ValueError(f"resolution must be one of {sorted(RESOLUTIONS)}; got {resolution!r}")
        if resolution == "reject":
            if decision == "approved":
                raise ValueError("resolution=reject conflicts with decision=approved")
            decision, route = "rejected", "failed"
            if reason is None:
                raise ValueError(
                    f"resolution=reject requires reject_reason (one of {list(REJECT_REASONS)})"
                )
            if corrections_in:
                raise ValueError("field_corrections do not apply to resolution=reject")
        else:
            if decision == "rejected":
                raise ValueError(f"resolution={resolution} conflicts with decision=rejected")
            decision = "approved"
            if reason is not None:
                raise ValueError("reject_reason only applies to resolution=reject")
            if resolution == "approve_as_is":
                if corrections_in:
                    raise ValueError("approve_as_is takes no field_corrections; use resolution=correct")
                if class_changed:
                    raise ValueError(
                        "the parked fields belong to the old class; use resolution=reclassify "
                        "to change doc_type"
                    )
                if details_changed and parked:
                    raise ValueError(
                        "the parked fields were extracted for the old subtype/subclass; use "
                        "resolution=reclassify to change it"
                    )
                # The human's approval is authoritative for the parked model
                # fields, parse failure included (see the note above): archive
                # them as-is. No parked fields means a sorter-stage park, so
                # approving just confirms the class and re-extracts.
                if parked:
                    route = "archive"
                    extracted = parked
            elif resolution == "correct":
                if class_changed:
                    raise ValueError("resolution=correct cannot change doc_type; use resolution=reclassify")
                if details_changed and parked:
                    raise ValueError(
                        "resolution=correct cannot change the subtype/subclass of parked fields; "
                        "use resolution=reclassify"
                    )
                if not corrections_in:
                    raise ValueError("resolution=correct requires non-empty field_corrections")
                route = "archive"
                corrections = corrections_in
                extracted = {**parked, **corrections_in}
            else:  # reclassify
                if corrections_in:
                    raise ValueError("reclassify takes no field_corrections (the document is re-extracted)")
                if not class_override and not classification_changed:
                    raise ValueError(
                        "resolution=reclassify requires doc_type (or override_doc_type), "
                        "doc_subclass or contract_subtype"
                    )
            if route == "archive":
                if not manifest.doc_type:
                    raise ValueError(
                        "Document has no classification; use resolution=reclassify with doc_type"
                    )
                extracted = validate_operator_extraction(manifest.doc_type, extracted or {})

    label: dict[str, Any] = {
        "decision": decision,
        "resolution": resolution,
        "route": route,
        "doc_type": manifest.doc_type,
        "contract_subtype": manifest.contract_subtype,
        "doc_subclass": manifest.doc_subclass,
        "field_corrections": corrections,
        "model_snapshot": model_snapshot(before),
        "reviewer": reviewer_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes or "",
        "reject_reason": reason,
        "class_override": class_override or None,
        "legacy_request": legacy,
    }
    if removed:
        label["removed_fields"] = removed
    return ResolutionPlan(
        decision=decision,
        resolution=resolution,
        route=route,
        legacy=legacy,
        reviewer=reviewer_id,
        notes=notes or "",
        label=label,
        extracted_data=extracted,
        field_corrections=corrections,
        reject_reason=reason,
    )
