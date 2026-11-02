"""Archivist scoring block and hash-chained ``archived`` audit row.

Mailroom-issues #236 (report → judge → archive), #237 (unified scoring
block), and #238 (one call, one block per document). This package does
not write the hash-chain DB. It:

* returns the ``detail.scoring`` payload the archivist files after the
  deterministic report exists;
* formats the same audit-log row the archivist would append (hash
  version 2);
* validates that row against the pipeline steps for the document and
  signs off when there is nothing to revise.

Do not invent a second formula: :func:`score_extraction` is the overall
score, and :func:`extraction_binary_metrics` is field-micro F1/F2.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .extraction_metrics import extraction_binary_metrics
from .field_scoring import (
    NEVER_SCORED_FIELDS,
    RETIRED_PROMPT_KEYS,
    score_extraction,
)
from .gt_metadata import is_empty_value
from .scorecard_honesty import (
    _METRIC_ID_BY_NAME,
    ProvenanceExportError,
    assess_extraction_gt,
    metric_id_for,
    score_format_layer,
    stamp_provenance,
)
from .trace_knobs import capture_trace_knobs, empty_trace_payload

__all__ = [
    "ARCHIVE_HASH_VERSION",
    "ARCHIVE_SCORING_KEYS",
    "ARCHIVE_SCORING_POST_V021_KEYS",
    "ARCHIVE_SCORING_METHOD",
    "ARCHIVED_REQUIRED_TAIL",
    "AUDIT_DETAIL_KEYS",
    "AUDIT_ENTRY_KEYS",
    "AUDIT_HASH_FIELDS",
    "AUDIT_NODE_ALIASES",
    "AUDIT_PIPELINE_NODES",
    "HAPPY_PATH_NODES",
    "LIVE_ARCHIVE_DOC_TYPES",
    "NEVER_SCORED_FIELDS",
    "RETIRED_PROMPT_KEYS",
    "archivist_sign_off",
    "archive_entry_hash",
    "audit_hash_payload",
    "canonical_json",
    "empty_archive_scoring_block",
    "empty_audit_detail",
    "empty_audit_log_entry",
    "field_map_digest",
    "format_audit_entry",
    "normalize_audit_node",
    "normalize_audit_nodes",
    "prepare_archivist_handoff",
    "score_archive_block",
    "upsert_archive_scoring",
    "validate_audit_entry",
    "verify_entry_hash",
]

#: Hash version 2: SHA-256 of canonical JSON over the fields listed in
#: :func:`archive_entry_hash` (mailroom-issues #236).
ARCHIVE_HASH_VERSION = 2

ARCHIVE_SCORING_METHOD = "unweighted_mean_of_nonempty_expected_fields"

#: Keys the archivist ``detail.scoring`` object always carries.
ARCHIVE_SCORING_KEYS: tuple[str, ...] = (
    "method",
    "field_map",
    "field_map_sha",
    "overall_score",
    "schema_valid",
    "n_fields_scored",
    "field_scores",
    "extraction_precision",
    "extraction_recall",
    "extraction_f1",
    "extraction_f2",
    "tp",
    "fp",
    "fn",
    "status",
    "reason",
    "trace",
    "provenance",
)

#: Scoring keys added after v0.21.0 (#16 status/reason, #22 provenance).
#: Rows filed before then are hash-chained and cannot gain them, so
#: :func:`validate_audit_entry` does not require them.
ARCHIVE_SCORING_POST_V021_KEYS: frozenset[str] = frozenset(
    {"status", "reason", "provenance"}
)

#: Live extraction classes that reach the archive (#238).
LIVE_ARCHIVE_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)

#: Top-level columns of a hash-chained audit row (#236). ``entry_hash`` is
#: computed; ``seq`` is the DB sequence and is not hashed.
AUDIT_ENTRY_KEYS: tuple[str, ...] = (
    "entry_id",
    "doc_id",
    "matter_id",
    "seq",
    "event",
    "actor",
    "timestamp",
    "prev_hash",
    "entry_hash",
)

#: Fields hashed under version 2 (SHA-256 of canonical JSON).
AUDIT_HASH_FIELDS: tuple[str, ...] = (
    "hash_version",
    "prev_hash",
    "doc_id",
    "entry_id",
    "matter_id",
    "actor",
    "timestamp",
    "event",
    "detail",
)

#: Keys ``detail`` always carries on the ``archived`` row.
AUDIT_DETAIL_KEYS: tuple[str, ...] = (
    "stage",
    "pipeline_success",
    "nodes_visited",
    "doc_type",
    "doc_subclass",
    "classification_confidence",
    "extraction_confidence",
    "classification_attempts",
    "extraction_attempts",
    "judge_verdict",
    "judge_score",
    "scoring",
    "report_path",
    "archive_path",
    "file_sha256",
    "review_decision",
    "arbiter_decision",
    "escalation_reason",
)

#: Canonical node names on ``detail.nodes_visited`` (flowchart in #236).
AUDIT_PIPELINE_NODES: tuple[str, ...] = (
    "intake",
    "sorter",
    "classification_review",
    "human_review_classification",
    "document_type_specialist",
    "arbiter",
    "specialist_retry",
    "judge",
    "human_review_extraction",
    "deterministic_reporter",
    "deterministic_judge",
    "archivist",
)

#: Successful job: intake → sorter → specialist → report → judge → archive.
HAPPY_PATH_NODES: tuple[str, ...] = (
    "intake",
    "sorter",
    "document_type_specialist",
    "deterministic_reporter",
    "deterministic_judge",
    "archivist",
)

#: Every terminal ``archived`` row still reports, scores, then archives.
ARCHIVED_REQUIRED_TAIL: tuple[str, ...] = (
    "deterministic_reporter",
    "deterministic_judge",
    "archivist",
)

#: Langfuse / agent / tray names folded onto the #236 audit node list.
AUDIT_NODE_ALIASES: dict[str, str] = {
    "intake-document": "intake",
    "normalize-intake": "intake",
    "classify-document": "sorter",
    "sorter_reviewer": "classification_review",
    "classification_review_agent": "classification_review",
    "human_review_tray_classification": "human_review_classification",
    "extract-fields": "document_type_specialist",
    "contracts_specialist": "document_type_specialist",
    "merger_agreement_specialist": "document_type_specialist",
    "corporate_records_specialist": "document_type_specialist",
    "correspondence_specialist": "document_type_specialist",
    "insurance_claims_specialist": "document_type_specialist",
    "specialist": "document_type_specialist",
    "arbitrate-verdict": "arbiter",
    # Classification arbiter between a parked decision review and a person
    # (llm-mailroom graph node ``classification_arbiter``).
    "arbitrate-classification": "classification_review",
    "judge-verify": "judge",
    "human_review_tray_extraction": "human_review_extraction",
    "compile-report": "deterministic_reporter",
    "reporter": "deterministic_reporter",
    "archive-document": "archivist",
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: CUAD / MAUD headline names that must not overwrite field-micro F1.
_HEADLINE_METHOD_ALIASES: dict[str, str] = {
    "extraction_category_presence": "cuad.clause_presence.micro_f1",
    "cuad.clause_presence.micro_f1": "cuad.clause_presence.micro_f1",
    "maud_question_accuracy": "maud.question.micro_accuracy",
    "maud.question.micro_accuracy": "maud.question.micro_accuracy",
    "maud_clause_presence": "maud.clause_presence.rate",
    "maud.clause_presence.rate": "maud.clause_presence.rate",
}


def _archive_metric_id(resolved_method: str) -> str:
    """Canonical ``metric_id`` for an archive ``method`` (#17).

    The default method is the pipeline rubric; CUAD / MAUD headline ids pass
    through; registry metric names resolve through the canonical table, so
    archive rows and emitted records carry the same identity. Anything else
    raises :class:`~llm_dojo_scoring.scorecard_honesty.UnknownMetricNameError`
    rather than stamping an id no other leg can match.
    """
    if resolved_method == ARCHIVE_SCORING_METHOD:
        return "pipeline.extraction.overall"
    if resolved_method in _HEADLINE_METHOD_ALIASES.values():
        return resolved_method
    if resolved_method in set(_METRIC_ID_BY_NAME.values()):
        return resolved_method  # already a canonical id
    return metric_id_for(resolved_method)


def canonical_json(obj: Any) -> str:
    """Canonical JSON for hash version 2 — byte-identical to llm-mailroom.

    llm-mailroom ``src/schemas/audit.py::compute_audit_hash`` serializes the
    payload with ``json.dumps(payload, sort_keys=True, default=str)`` (default
    separators, ``default=str``). This function must stay byte-identical to
    that call: any change here (or there) breaks hash-chain verification
    across the two packages even though each side round-trips its own rows.
    """
    return json.dumps(obj, sort_keys=True, default=str)


def _hash_timestamp(timestamp: Any) -> str:
    """Timestamp form used inside the hash — mirrors llm-mailroom exactly.

    llm-mailroom hashes ``timestamp.isoformat()`` when the value is a
    ``datetime`` and ``str(timestamp)`` otherwise. Normalizing here keeps
    ``datetime`` inputs interoperable instead of relying on ``default=str``
    (which would render ``"YYYY-MM-DD HH:MM:SS+00:00"``, not ISO ``T``).
    """
    if hasattr(timestamp, "isoformat"):
        return timestamp.isoformat()
    return str(timestamp)


def field_map_digest(
    field_types: Mapping[str, str],
    *,
    taxonomy_bytes: bytes | None = None,
) -> str:
    """SHA of the field map used for this score.

    When ``taxonomy_bytes`` is the live ``taxonomy.yaml`` blob, this is the
    git-blob SHA-1 of those bytes. Otherwise SHA-256 of canonical JSON of
    the field-type map actually scored.
    """
    if taxonomy_bytes is not None:
        header = f"blob {len(taxonomy_bytes)}\0".encode("utf-8")
        return hashlib.sha1(header + taxonomy_bytes).hexdigest()
    payload = canonical_json(dict(field_types))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def empty_archive_scoring_block() -> dict[str, Any]:
    """Shape of the archivist scoring block with every numeric slot null."""
    return {
        "method": ARCHIVE_SCORING_METHOD,
        "field_map": "taxonomy.yaml",
        "field_map_sha": None,
        "overall_score": None,
        "schema_valid": None,
        "n_fields_scored": None,
        "field_scores": {},
        "extraction_precision": None,
        "extraction_recall": None,
        "extraction_f1": None,
        "extraction_f2": None,
        "tp": None,
        "fp": None,
        "fn": None,
        "status": None,
        "reason": None,
        "trace": empty_trace_payload(),
        "provenance": stamp_provenance()["provenance"],
    }


def _is_empty(value: Any) -> bool:
    """True for ``None``, blank strings, and empty collections (including the
    stringified ``"[]"`` / ``"{}"`` forms the Hub metadata uses)."""
    return is_empty_value(value)


def _live_field_types(doc_class: str, field_types: Mapping[str, str] | None) -> dict[str, str]:
    """Return ``field_types`` as given, else the live default map for ``doc_class``."""
    if field_types:
        return dict(field_types)
    from .suites import DEFAULT_FIELD_TYPES

    mapped = DEFAULT_FIELD_TYPES.get(doc_class)
    if mapped:
        return dict(mapped)
    raise KeyError(
        f"no live field map for {doc_class!r}; known: {sorted(LIVE_ARCHIVE_DOC_TYPES)}"
    )


def _filter_to_live_map(
    record: Mapping[str, Any] | None,
    field_types: Mapping[str, str],
) -> dict[str, Any]:
    """Drop never-scored / retired keys and anything outside ``field_types``."""
    src = dict(record or {})
    out: dict[str, Any] = {}
    for key, value in src.items():
        if key in NEVER_SCORED_FIELDS or key in RETIRED_PROMPT_KEYS:
            continue
        if key not in field_types:
            continue
        out[key] = value
    return out


def score_archive_block(
    doc_class: str,
    predicted: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    *,
    field_types: Mapping[str, str] | None = None,
    field_map: str = "taxonomy.yaml",
    field_map_sha: str | None = None,
    taxonomy_bytes: bytes | None = None,
    method: str | None = None,
    doc_text: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One scoring block for one document (mailroom-issues #237 / #238).

    ``overall_score`` is the unweighted mean of typed scores on expected
    fields that are non-null and non-empty. ``schema_valid`` is the share
    of required keys present on the prediction, filed as a bool (True
    iff every live-map key is present). Field-micro F1/F2 are filled only
    when TP/FP/FN counts exist. CUAD / MAUD headline names passed as
    ``method`` replace ``method`` only — they never overwrite
    ``extraction_f1``.

    ``provenance`` (dataset revision, prompt lineage, model, serving kind)
    is stamped onto the block (#22); ``metric_id`` follows ``method`` and a
    contradicting caller value raises
    :class:`~llm_dojo_scoring.scorecard_honesty.ProvenanceExportError`.
    """
    ftypes = _live_field_types(doc_class, field_types)
    exp = _filter_to_live_map(expected, ftypes)
    pred = _filter_to_live_map(predicted, ftypes)

    result = score_extraction(
        doc_class, ftypes, pred, exp, doc_text=doc_text
    )
    format_scores = score_format_layer(
        predicted=dict(predicted or {}),
        required_keys=list(ftypes.keys()),
    )
    schema_share = format_scores.get("schema_valid")
    schema_valid: bool | None
    if schema_share is None:
        schema_valid = None
    else:
        schema_valid = float(schema_share) >= 1.0

    prf = extraction_binary_metrics(
        exp, pred, field_map=ftypes, doc_class=doc_class, result=result
    )
    # Fail closed on GT with nothing extractable (#16): status/reason say
    # why, and the micro counts stay null instead of reading 0.0. F1/F2 need
    # at least one expected event — with none, recall (and so F1) is
    # undefined, and spurious fills alone are not a score (#238).
    assessment = assess_extraction_gt(
        dict(expected) if isinstance(expected, Mapping) else expected,
        ftypes,
        doc_class=doc_class,
    )
    countable = assessment.scorable and int(prf.get("expected_events") or 0) > 0
    resolved_method = ARCHIVE_SCORING_METHOD
    if method:
        resolved_method = _HEADLINE_METHOD_ALIASES.get(method, method)

    digest = field_map_sha
    if digest is None:
        digest = field_map_digest(ftypes, taxonomy_bytes=taxonomy_bytes)

    block = empty_archive_scoring_block()
    block["method"] = resolved_method
    block["field_map"] = field_map
    block["field_map_sha"] = digest
    block["overall_score"] = result.overall_score
    block["schema_valid"] = schema_valid
    block["n_fields_scored"] = len(result.field_scores)
    block["field_scores"] = dict(result.field_scores)
    block["status"] = assessment.status
    block["reason"] = assessment.reason
    if countable:
        block["extraction_precision"] = prf.get("extraction_precision")
        block["extraction_recall"] = prf.get("extraction_recall")
        block["extraction_f1"] = prf.get("extraction_f1")
        block["extraction_f2"] = prf.get("extraction_f2")
        block["tp"] = prf.get("tp")
        block["fp"] = prf.get("fp")
        block["fn"] = prf.get("fn")
    metric_id = _archive_metric_id(resolved_method)
    given = dict(provenance or {})
    claimed = given.get("metric_id")
    if claimed is not None and claimed != metric_id:
        raise ProvenanceExportError(
            f"archive provenance metric_id {given['metric_id']!r} contradicts "
            f"method {resolved_method!r} ({metric_id!r})"
        )
    block["provenance"] = stamp_provenance(given, metric_id=metric_id)["provenance"]
    block["trace"] = capture_trace_knobs(
        predicted,
        expected=expected,
        correctness=result.overall_score,
    )
    return block


def audit_hash_payload(
    *,
    prev_hash: str,
    doc_id: str,
    entry_id: str,
    matter_id: str,
    actor: str,
    timestamp: str | None,
    event: str,
    detail: Mapping[str, Any],
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> dict[str, Any]:
    """Object hashed under version 2. ``seq`` and ``entry_hash`` stay off it.

    The ``timestamp`` slot is normalized through :func:`_hash_timestamp` so a
    ``datetime`` hashes like llm-mailroom's ``isoformat()`` form.
    """
    return {
        "hash_version": hash_version,
        "prev_hash": prev_hash,
        "doc_id": doc_id,
        "entry_id": entry_id,
        "matter_id": matter_id,
        "actor": actor,
        "timestamp": _hash_timestamp(timestamp),
        "event": event,
        "detail": dict(detail),
    }


def archive_entry_hash(
    *,
    prev_hash: str,
    doc_id: str,
    entry_id: str,
    matter_id: str,
    actor: str,
    timestamp: str,
    event: str,
    detail: Mapping[str, Any],
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> str:
    """SHA-256 of canonical JSON over hash version 2 fields (#236)."""
    payload = audit_hash_payload(
        prev_hash=prev_hash,
        doc_id=doc_id,
        entry_id=entry_id,
        matter_id=matter_id,
        actor=actor,
        timestamp=timestamp,
        event=event,
        detail=detail,
        hash_version=hash_version,
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def empty_audit_detail() -> dict[str, Any]:
    """Template ``detail`` object for an ``archived`` row."""
    return {
        "stage": "archived",
        "pipeline_success": None,
        "nodes_visited": [],
        "doc_type": None,
        "doc_subclass": None,
        "classification_confidence": None,
        "extraction_confidence": None,
        "classification_attempts": None,
        "extraction_attempts": None,
        "judge_verdict": None,
        "judge_score": None,
        "scoring": empty_archive_scoring_block(),
        "report_path": None,
        "archive_path": None,
        "file_sha256": None,
        "review_decision": None,
        "arbiter_decision": None,
        "escalation_reason": None,
    }


def empty_audit_log_entry() -> dict[str, Any]:
    """Template hash-chain row passed to the archivist to fill or revise."""
    return {
        "entry_id": None,
        "doc_id": None,
        "matter_id": None,
        "seq": None,
        "event": "archived",
        "actor": "archivist",
        "timestamp": None,
        "prev_hash": "",
        "entry_hash": None,
        "detail": empty_audit_detail(),
    }


def normalize_audit_node(name: str) -> str:
    """Fold a graph / agent / tray name onto the #236 audit node list."""
    key = str(name or "").strip()
    if not key:
        return key
    return AUDIT_NODE_ALIASES.get(key, AUDIT_NODE_ALIASES.get(key.lower(), key))


def normalize_audit_nodes(nodes: Iterable[str] | None) -> list[str]:
    """Fold a list of graph / agent / tray names onto the #236 audit node list."""
    return [normalize_audit_node(n) for n in (nodes or ())]


def _is_sha256_hex(value: Any) -> bool:
    """True for a 64-char lowercase sha256 hex digest."""
    return isinstance(value, str) and bool(_SHA256_HEX.match(value))


def _merge_audit_detail(
    detail: Mapping[str, Any] | None,
    *,
    scoring: Mapping[str, Any] | None = None,
    nodes_visited: Sequence[str] | None = None,
    pipeline_success: bool | None = None,
) -> dict[str, Any]:
    """Overlay ``detail`` (and explicit overrides) onto the empty-detail template."""
    body = empty_audit_detail()
    extras: dict[str, Any] = {}
    for key, value in dict(detail or {}).items():
        if key in AUDIT_DETAIL_KEYS:
            if key == "scoring" and isinstance(value, Mapping):
                merged_scoring = empty_archive_scoring_block()
                merged_scoring.update(dict(value))
                body["scoring"] = merged_scoring
            elif key == "nodes_visited":
                body["nodes_visited"] = normalize_audit_nodes(value)
            else:
                body[key] = value
        else:
            extras[key] = value
    if scoring is not None:
        merged_scoring = empty_archive_scoring_block()
        merged_scoring.update(dict(scoring))
        body["scoring"] = merged_scoring
    if nodes_visited is not None:
        body["nodes_visited"] = normalize_audit_nodes(nodes_visited)
    if pipeline_success is not None:
        body["pipeline_success"] = bool(pipeline_success)
    body.update(extras)
    return body


def format_audit_entry(
    *,
    doc_id: str,
    matter_id: str,
    entry_id: str,
    timestamp: str,
    prev_hash: str = "",
    seq: int | None = None,
    event: str = "archived",
    actor: str = "archivist",
    scoring: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
    nodes_visited: Sequence[str] | None = None,
    pipeline_success: bool | None = None,
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> dict[str, Any]:
    """Format the hash-chained ``archived`` row the archivist files.

    Fills the template, embeds ``scoring`` as ``detail.scoring``, and
    writes ``entry_hash`` (hash version 2). Does not persist the audit DB.
    """
    body = _merge_audit_detail(
        detail,
        scoring=scoring,
        nodes_visited=nodes_visited,
        pipeline_success=pipeline_success,
    )
    digest = archive_entry_hash(
        prev_hash=prev_hash,
        doc_id=doc_id,
        entry_id=entry_id,
        matter_id=matter_id,
        actor=actor,
        timestamp=timestamp,
        event=event,
        detail=body,
        hash_version=hash_version,
    )
    return {
        "entry_id": entry_id,
        "doc_id": doc_id,
        "matter_id": matter_id,
        "seq": seq,
        "event": event,
        "actor": actor,
        "timestamp": timestamp,
        "prev_hash": prev_hash,
        "entry_hash": digest,
        "detail": body,
    }


def verify_entry_hash(
    entry: Mapping[str, Any],
    *,
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> bool:
    """True when ``entry_hash`` matches hash version 2 of the row."""
    expected = entry.get("entry_hash")
    if not isinstance(expected, str) or not expected:
        return False
    recomputed = archive_entry_hash(
        prev_hash=str(entry.get("prev_hash") or ""),
        doc_id=str(entry.get("doc_id") or ""),
        entry_id=str(entry.get("entry_id") or ""),
        matter_id=str(entry.get("matter_id") or ""),
        actor=str(entry.get("actor") or ""),
        timestamp=str(entry.get("timestamp") or ""),
        event=str(entry.get("event") or ""),
        detail=dict(entry.get("detail") or {}),
        hash_version=int(hash_version),
    )
    return expected == recomputed


def _revision(code: str, message: str) -> dict[str, str]:
    """Build one ``{code, message}`` revision item."""
    return {"code": code, "message": message}


def _nodes_in_order(visited: Sequence[str], required: Sequence[str]) -> bool:
    """True when every ``required`` node appears in ``visited``, in order."""
    last = -1
    for name in required:
        try:
            found = visited.index(name)
        except ValueError:
            return False
        if found <= last:
            return False
        last = found
    return True


def validate_audit_entry(
    entry: Mapping[str, Any],
    *,
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> list[dict[str, str]]:
    """Revisions the archivist must make before signing off.

    Empty list means the templated row matches the hash-chain contract
    and the pipeline steps for this document. Does not mutate ``entry``.
    """
    revisions: list[dict[str, str]] = []
    for key in ("entry_id", "doc_id", "matter_id", "timestamp"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            revisions.append(
                _revision("missing_identity", f"{key} is required on the audit row")
            )
    prev_hash = entry.get("prev_hash")
    if prev_hash is None:
        revisions.append(_revision("missing_identity", "prev_hash is required (empty string for the first row)"))
    elif prev_hash != "" and not _is_sha256_hex(prev_hash):
        revisions.append(
            _revision("bad_prev_hash", "prev_hash must be '' or a 64-char sha256 hex digest")
        )

    event = entry.get("event")
    actor = entry.get("actor")
    if event != "archived":
        revisions.append(
            _revision("bad_event", "terminal archive row must use event='archived'")
        )
    if actor != "archivist":
        revisions.append(
            _revision("bad_actor", "archived row actor must be 'archivist'")
        )

    if not verify_entry_hash(entry, hash_version=hash_version):
        revisions.append(
            _revision("hash_mismatch", "entry_hash does not match hash version 2 of the row")
        )

    detail = entry.get("detail")
    if not isinstance(detail, Mapping):
        revisions.append(_revision("missing_detail", "detail object is required"))
        return revisions

    for key in AUDIT_DETAIL_KEYS:
        if key not in detail:
            revisions.append(
                _revision("missing_detail_key", f"detail.{key} is required on the archived row")
            )

    pipeline_success = detail.get("pipeline_success")
    if not isinstance(pipeline_success, bool):
        revisions.append(
            _revision("missing_pipeline_success", "detail.pipeline_success must be a bool")
        )

    raw_nodes = detail.get("nodes_visited")
    if not isinstance(raw_nodes, (list, tuple)):
        revisions.append(
            _revision("missing_nodes_visited", "detail.nodes_visited must be a list")
        )
        nodes: list[str] = []
    else:
        nodes = normalize_audit_nodes(raw_nodes)
        unknown = [n for n in nodes if n not in AUDIT_PIPELINE_NODES]
        if unknown:
            revisions.append(
                _revision(
                    "unknown_pipeline_node",
                    "nodes_visited contains names not on the #236 pipeline: "
                    + ", ".join(unknown),
                )
            )
        if nodes and nodes[-1] != "archivist":
            revisions.append(
                _revision("archivist_not_terminal", "archivist must be the last node on an archived row")
            )
        if not _nodes_in_order(nodes, ARCHIVED_REQUIRED_TAIL):
            revisions.append(
                _revision(
                    "pipeline_order",
                    "archived row must visit deterministic_reporter, then "
                    "deterministic_judge, then archivist",
                )
            )
        if pipeline_success is True and not _nodes_in_order(nodes, HAPPY_PATH_NODES):
            revisions.append(
                _revision(
                    "happy_path_incomplete",
                    "successful jobs require intake → sorter → "
                    "document_type_specialist → deterministic_reporter → "
                    "deterministic_judge → archivist",
                )
            )

    scoring = detail.get("scoring")
    if not isinstance(scoring, Mapping):
        revisions.append(_revision("missing_scoring", "detail.scoring is required"))
    else:
        missing = [
            k
            for k in ARCHIVE_SCORING_KEYS
            if k not in scoring and k not in ARCHIVE_SCORING_POST_V021_KEYS
        ]
        if missing:
            revisions.append(
                _revision(
                    "scoring_keys_incomplete",
                    "detail.scoring is missing " + ", ".join(missing),
                )
            )

    verdict = detail.get("judge_verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        revisions.append(
            _revision("missing_judge_verdict", "detail.judge_verdict is required after the deterministic judge")
        )

    judge_score = detail.get("judge_score")
    overall = None
    if isinstance(scoring, Mapping):
        overall = scoring.get("overall_score")
    if overall is not None and judge_score is None:
        revisions.append(
            _revision(
                "missing_judge_score",
                "detail.judge_score is required when scoring.overall_score is filed",
            )
        )

    if not isinstance(detail.get("doc_type"), str) or not str(detail.get("doc_type") or "").strip():
        revisions.append(_revision("missing_doc_type", "detail.doc_type is required"))

    for path_key in ("report_path", "archive_path"):
        value = detail.get(path_key)
        if not isinstance(value, str) or not value.strip():
            revisions.append(
                _revision("missing_path", f"detail.{path_key} is required on the archived row")
            )

    file_sha = detail.get("file_sha256")
    if not _is_sha256_hex(file_sha):
        revisions.append(
            _revision("bad_file_sha256", "detail.file_sha256 must be a 64-char sha256 hex digest")
        )

    return revisions


def archivist_sign_off(
    entry: Mapping[str, Any],
    *,
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> dict[str, Any]:
    """Validate the templated row against every pipeline step for this document.

    Returns a verdict the archivist records beside the hashed entry. Sign-off
    never mutates ``entry`` (adding a field to ``detail`` would change the
    hash). ``signed_off`` is True only when there is nothing to revise.
    """
    revisions = validate_audit_entry(entry, hash_version=hash_version)
    hash_ok = verify_entry_hash(entry, hash_version=hash_version)
    codes = {item["code"] for item in revisions}
    pipeline_ok = not codes.intersection(
        {
            "missing_nodes_visited",
            "unknown_pipeline_node",
            "archivist_not_terminal",
            "pipeline_order",
            "happy_path_incomplete",
            "missing_pipeline_success",
        }
    )
    scoring_ok = not codes.intersection(
        {
            "missing_scoring",
            "scoring_keys_incomplete",
            "missing_judge_verdict",
            "missing_judge_score",
        }
    )
    signed_off = not revisions
    return {
        "status": "signed_off" if signed_off else "revision_required",
        "signed_off": signed_off,
        "revisions": revisions,
        "entry_hash": entry.get("entry_hash"),
        "hash_ok": hash_ok,
        "pipeline_ok": pipeline_ok,
        "scoring_ok": scoring_ok,
    }


def prepare_archivist_handoff(
    *,
    doc_id: str,
    matter_id: str,
    entry_id: str,
    timestamp: str,
    prev_hash: str = "",
    seq: int | None = None,
    event: str = "archived",
    actor: str = "archivist",
    scoring: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
    nodes_visited: Sequence[str] | None = None,
    pipeline_success: bool | None = None,
    hash_version: int = ARCHIVE_HASH_VERSION,
) -> dict[str, Any]:
    """Format the audit row and hand it to the archivist for sign-off.

    The templated ``entry`` is always passed through. The archivist files it
    as final only when :func:`archivist_sign_off` returns ``signed_off``.
    """
    entry = format_audit_entry(
        doc_id=doc_id,
        matter_id=matter_id,
        entry_id=entry_id,
        timestamp=timestamp,
        prev_hash=prev_hash,
        seq=seq,
        event=event,
        actor=actor,
        scoring=scoring,
        detail=detail,
        nodes_visited=nodes_visited,
        pipeline_success=pipeline_success,
        hash_version=hash_version,
    )
    verdict = archivist_sign_off(entry, hash_version=hash_version)
    return {
        "entry": entry,
        "sign_off": verdict,
        "file_as_final": bool(verdict["signed_off"]),
    }


def upsert_archive_scoring(
    log: MutableMapping[str, dict[str, Any]],
    doc_id: str,
    scoring: Mapping[str, Any],
    *,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace ``detail.scoring`` for ``doc_id``; never append a second row.

    Dojo does not persist the audit DB. Consumers (llm-mailroom archivist
    tests) use this contract: a second score of the same ``doc_id``
    overwrites the scoring block inside the existing row.
    """
    row = log.get(doc_id)
    if row is None:
        row = {
            "doc_id": doc_id,
            "event": "archived",
            "actor": "archivist",
            "detail": dict(detail or {}),
        }
        log[doc_id] = row
    elif detail:
        merged = dict(row.get("detail") or {})
        merged.update(detail)
        row["detail"] = merged
    row.setdefault("detail", {})
    row["detail"]["scoring"] = dict(scoring)
    return row
