"""Relations — the mailroom's research clerk (HUB-040).

Deterministic-first association layer over the archive: links associated
topics, documents, and matters the way a lawyer builds a matter file, stores
every association in its own hash-chained ledger, and feeds a bounded
advisory context block back to the pipeline agents and the completion echo —
the longitudinal loop that lets zero-shot processing inherit everything the
archive already knows.

Two triggers:
- Post-archive dispatch (``dispatch_relations_scan``): fired at every
  terminal manifest (archive/review/failed) by the graph, off the document
  path (daemon thread — the completion-echo pattern). Never delays or breaks
  a document run.
- Background sweep (``RelationsSweeper`` / ``start_embedded_relations_scanner``):
  a watermark-incremental pass over the whole archive on a timer, embedded in
  the watcher (the Gmail-poller pattern) or standalone
  (``python -m pipeline.relations_scan``).

Deterministic signal mix (all free — no LLM):
- ``same_matter`` — same matter_id (capped by recency).
- ``topic_overlap`` — keyword Jaccard over the extracted_data keywords.
- ``party_overlap`` — shared party-ish extracted fields (parties, sender,
  recipient, insured_party, entity_name, ...).
- ``semantic_similarity`` — embedding cosine via the dojo's
  ``get_embedding_model`` (sentence-transformers, local); embeddings are
  cached per (doc, model) in ``relation_embeddings`` so a document is
  embedded exactly once. Unavailable model ⇒ the cosine signal is skipped
  and the other signals still flow (fail-soft).
- Temporal proximity boosts evidence (never a standalone edge).

The LLM judgment pass (``agents/relations.py``) is CODE-COMPLETE but OFF
until production: ``relations.llm: false`` (taxonomy) keeps the pilot
deterministic-only; flips on with the production model + the free-only
guardrail unset.

Sync surface for daemon threads wraps the async storage with ``asyncio.run``
(house pattern — the Gmail echo's audit read).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone

import structlog

from .env import load_env

logger = structlog.get_logger(__name__)

DEFAULTS = {
    "similarity_threshold": 0.62,
    "keyword_jaccard_threshold": 0.25,
    "max_edges_per_doc": 50,
    "top_k_llm_candidates": 5,
    "llm": False,
    "context_injection": True,
    "scan_batch_size": 25,
    "graphs": True,
    "same_matter_cap": 25,
    "temporal_boost": 0.05,
    "context_limit": 6,
    "embedding_text_chars": 8000,
}

# Party-ish fields in the extraction schemas (v7/v8 lineage) — any shared
# non-empty value is a party_overlap candidate signal.
_PARTY_FIELDS = (
    "parties",
    "party",
    "sender",
    "recipient",
    "entity_name",
    "insured_party",
    "insurer",
    "policyholder",
    "claimant",
    "adjuster",
    "signatories",
)

# Date-ish fields for the temporal evidence boost (best-effort ISO parse).
_DATE_FIELDS = (
    "effective_date",
    "communication_date",
    "date_of_loss",
    "date_filed",
    "filing_date",
)

_EMBEDDER = None
_EMBEDDER_LOCK = threading.Lock()

# Hard bound on the real embedder call — the first dojo model use can trigger
# a multi-minute SentenceTransformer download (O-10 is why field_scoring
# preloads OFF the document path); a hung download must degrade to "cosine
# signal skipped", never stall the sweeper/scan thread.
_EMBED_TIMEOUT_SECONDS = 90.0


# ---------------------------------------------------------------------------
# Configuration


def relations_config() -> dict:
    """Effective ``relations:`` block from taxonomy.yaml with defaults."""
    from pipeline.config import load_config

    cfg = dict(DEFAULTS)
    try:
        block = (load_config().get("relations") or {}) or {}
        cfg.update({k: v for k, v in block.items() if v is not None})
    except Exception:
        logger.debug("relations_config_load_failed")
    return cfg


def relations_enabled() -> bool:
    """Kill-switch for the whole layer: taxonomy ``relations.enabled`` AND
    the ``MAILROOM_RELATIONS`` env override (default ON — deterministic + free)."""
    load_env()
    if str(os.environ.get("MAILROOM_RELATIONS", "1")).strip().lower() in (
        "0",
        "false",
        "no",
        "off",
        "",
    ):
        return False
    try:
        return bool(relations_config().get("enabled", True))
    except Exception:
        return True


def context_injection_enabled() -> bool:
    load_env()
    return relations_config().get("context_injection", True) and str(
        os.environ.get("MAILROOM_RELATIONS_CONTEXT", "1")
    ).strip().lower() not in ("0", "false", "no", "off", "")


def llm_pass_enabled() -> bool:
    """The LLM judgment pass is opt-in via taxonomy ``relations.llm`` (OFF in
    the pilot per the human directive) plus an env kill-switch."""
    load_env()
    return bool(relations_config().get("llm", False)) and str(
        os.environ.get("MAILROOM_RELATIONS_LLM", "1")
    ).strip().lower() not in ("0", "false", "no", "off", "")


def embedding_model_name() -> str:
    """The embedding model (matches taxonomy ``field_scoring.embedding_model``
    so the dojo's cached/warm model is reused, never a second download)."""
    try:
        from pipeline.config import load_config

        return str(
            (load_config().get("field_scoring") or {}).get(
                "embedding_model", "sentence-transformers/all-MiniLM-L6-v2"
            )
        )
    except Exception:
        return "sentence-transformers/all-MiniLM-L6-v2"


def set_embedder(embedder) -> None:
    """Inject an embedder callable (test seam): ``list[str] -> list[list[float]]``.
    Pass None to reset to the dojo model."""
    global _EMBEDDER
    with _EMBEDDER_LOCK:
        _EMBEDDER = embedder


def embeddings_enabled() -> bool:
    """Whether the REAL embedder (dojo sentence-transformers) may be used.

    The injected test seam bypasses this check. Kill it in hermetic test runs
    (conftest) and anywhere the model download is unwanted — the cosine
    signal then simply skips and the other signals flow."""
    load_env()
    return str(os.environ.get("MAILROOM_RELATIONS_EMBEDDINGS", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed texts via the injected seam or the dojo model. Returns None when
    embeddings are unavailable (fail-soft — the cosine signal just skips)."""
    with _EMBEDDER_LOCK:
        embedder = _EMBEDDER
    if embedder is None:
        if not embeddings_enabled():
            return None
        try:
            from observability.field_scoring import get_embedding_model

            model = get_embedding_model()
            if model is None:
                return None
            embedder = lambda vals: model.encode(vals, show_progress_bar=False).tolist()  # noqa: E731
        except Exception:
            logger.debug("relations_embedder_unavailable")
            return None
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(embedder, texts)
            return future.result(timeout=_EMBED_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("relations_embed_failed")
        return None


# ---------------------------------------------------------------------------
# Sync wrappers over the async storage (daemon-thread house pattern)


def _run(coro):
    return asyncio.run(coro)


def verify_ledger() -> tuple[bool, int]:
    """Verify the relations hash chain. Returns (ok, entries)."""
    from schemas.audit import AuditLogEntry, verify_chain
    from storage import relations as R

    chain = _run(R.get_relation_chain())
    entries = [
        AuditLogEntry(
            entry_id=c["entry_id"],
            doc_id="__relations__",
            matter_id="relations",
            event=c["event"],
            actor=c["actor"],
            detail=c["detail"],
            prev_hash=c["prev_hash"],
            entry_hash=c["entry_hash"],
            timestamp=c["timestamp"],
        )
        for c in chain
    ]
    return verify_chain(entries), len(entries)


def _log_event(event: str, detail: dict, actor: str = "relations") -> None:
    """Best-effort ledger append — a ledger failure never breaks a scan."""
    from storage import relations as R

    try:
        _run(R.write_relation_log_entry(event, detail, actor=actor))
    except Exception:
        logger.warning("relations_ledger_write_failed", event=event)


# ---------------------------------------------------------------------------
# Signal extraction


def _extracted(record_row: dict) -> dict:
    raw = record_row.get("extracted_data")
    return raw if isinstance(raw, dict) else {}


def _keywords(extracted: dict) -> set[str]:
    kws = extracted.get("keywords") or []
    if isinstance(kws, str):
        try:
            kws = json.loads(kws)
        except Exception:
            kws = [kws]
    return {str(k).strip().lower() for k in kws if str(k).strip()} if isinstance(kws, list) else set()


def _parties(extracted: dict) -> set[str]:
    out: set[str] = set()
    for field in _PARTY_FIELDS:
        val = extracted.get(field)
        if not val:
            continue
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            token = str(v).strip().lower()
            if token:
                out.add(token)
    return out


def _first_date(extracted: dict) -> datetime | None:
    for field in _DATE_FIELDS:
        raw = extracted.get(field)
        if not raw:
            continue
        text = str(raw).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _doc_text(record_row: dict, cfg: dict) -> str:
    """Embedding input: the archived file's text when resolvable, else the
    bounded extraction metadata (filename + type + subject/keywords/report)."""
    from pathlib import Path

    from pipeline.bins import archive_dir

    matter = record_row.get("matter_id") or "DEFAULT"
    doc_type = record_row.get("doc_type") or "unknown"
    filename = record_row.get("original_filename") or ""
    candidates = [archive_dir(matter, doc_type) / filename]
    doc_id = record_row.get("doc_id") or ""
    if doc_id and filename:
        stem, suffix = Path(filename).stem, Path(filename).suffix
        candidates.append(archive_dir(matter, doc_type) / f"{stem}--{doc_id}{suffix}")
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                from graph.build_graph import _read_file_text

                text, _ = _read_file_text(path)
                if text and text.strip():
                    return text[: int(cfg.get("embedding_text_chars", 8000))]
        except Exception:
            continue
    extracted = _extracted(record_row)
    parts = [
        str(record_row.get("original_filename") or ""),
        str(record_row.get("doc_type") or ""),
        str(record_row.get("doc_subclass") or ""),
        str(extracted.get("subject_matter") or ""),
        ", ".join(sorted(_keywords(extracted))),
        str(extracted.get("_report") or ""),
    ]
    return " ".join(p for p in parts if p)[: int(cfg.get("embedding_text_chars", 8000))]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Scanning


def _catalog_doc(doc_id: str) -> dict | None:
    from sqlalchemy import select

    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    async def _q():
        ensure_schema()
        async with async_session() as session:
            row = (
                await session.execute(
                    select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "doc_id": row.doc_id,
                "matter_id": row.matter_id,
                "original_filename": row.original_filename,
                "doc_type": row.doc_type,
                "doc_subclass": row.doc_subclass,
                "extracted_data": row.extracted_data,
                "created_at": row.created_at,
            }

    return _run(_q())


def _archived_docs(exclude_doc_id: str | None = None, limit: int = 5000) -> list[dict]:
    from sqlalchemy import select

    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    async def _q():
        ensure_schema()
        async with async_session() as session:
            stmt = (
                select(DocumentRecord)
                .where(DocumentRecord.stage == "archived")
                .order_by(DocumentRecord.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "doc_id": r.doc_id,
                    "matter_id": r.matter_id,
                    "original_filename": r.original_filename,
                    "doc_type": r.doc_type,
                    "doc_subclass": r.doc_subclass,
                    "extracted_data": r.extracted_data,
                    "created_at": r.created_at,
                }
                for r in rows
                if r.doc_id != exclude_doc_id
            ]

    return _run(_q())


def scan_document(doc_id: str, *, run_id: str | None = None) -> dict:
    """One document's association pass: embedding cache, candidates, edges,
    ledger. Deterministic-only unless the LLM pass is enabled. Never raises."""
    from storage import relations as R

    if not relations_enabled():
        return {"skipped": "disabled"}
    run_id = run_id or R.new_scanner_run_id()
    cfg = relations_config()
    report: dict = {"doc_id": doc_id, "run_id": run_id, "edges_new": 0, "edges_updated": 0}
    try:
        row = _catalog_doc(doc_id)
        if row is None:
            report["skipped"] = "not_in_catalog"
            return report
        _log_event("relations_scan_started", {"kind": "document", "doc_id": doc_id, "run_id": run_id})

        # 1. Embedding cache (compute-once).
        model = embedding_model_name()
        vector = _run(R.get_embedding(doc_id, model))
        if vector is None:
            embedded = _embed([_doc_text(row, cfg)])
            if embedded and embedded[0]:
                _run(R.put_embedding(doc_id, model, embedded[0]))
                vector = embedded[0]

        # 2. Candidates: archived docs (same matter first).
        others = _archived_docs(exclude_doc_id=doc_id)
        extracted = _extracted(row)
        kws = _keywords(extracted)
        parties = _parties(extracted)
        doc_date = _first_date(extracted)

        same_matter = [o for o in others if o.get("matter_id") == row.get("matter_id")]
        candidates: dict[str, dict] = {}

        def add_candidate(other: dict, rtype: str, score: float, evidence: dict, method: str = "deterministic"):
            entry = candidates.setdefault(
                other["doc_id"], {"other": other, "signals": []}
            )
            entry["signals"].append({"relation_type": rtype, "score": score, "evidence": evidence, "method": method})

        for o in same_matter[: int(cfg.get("same_matter_cap", 25))]:
            add_candidate(o, "same_matter", 1.0, {"matter_id": row.get("matter_id")})

        # Cached embeddings for the cosine signal.
        embeddings = {}
        if vector:
            try:
                embeddings = _run(R.all_embeddings(model, exclude_doc_id=doc_id))
            except Exception:
                embeddings = {}

        for o in others:
            oid = o["doc_id"]
            o_extracted = _extracted(o)
            # keyword Jaccard
            o_kws = _keywords(o_extracted)
            if kws and o_kws:
                jaccard = len(kws & o_kws) / len(kws | o_kws)
                if jaccard >= float(cfg.get("keyword_jaccard_threshold", 0.25)):
                    add_candidate(
                        o,
                        "topic_overlap",
                        round(jaccard, 4),
                        {"jaccard": round(jaccard, 4), "shared": sorted(kws & o_kws)[:8]},
                    )
            # party overlap
            o_parties = _parties(o_extracted)
            if parties and o_parties:
                shared = parties & o_parties
                if shared:
                    add_candidate(
                        o,
                        "party_overlap",
                        round(len(shared) / len(parties | o_parties), 4),
                        {"shared": sorted(shared)[:8]},
                    )
            # embedding cosine
            o_vec = embeddings.get(oid)
            if vector and o_vec:
                cos = _cosine(vector, o_vec)
                if cos >= float(cfg.get("similarity_threshold", 0.62)):
                    add_candidate(
                        o,
                        "semantic_similarity",
                        round(cos, 4),
                        {"cosine": round(cos, 4), "model": model},
                    )
            # temporal evidence boost on existing candidates (never standalone)
            o_date = _first_date(o_extracted)
            if doc_date and o_date:
                delta = abs((doc_date - o_date).days)
                if delta <= 30 and oid in candidates:
                    for sig in candidates[oid]["signals"]:
                        sig["evidence"]["temporal_days"] = delta
                        sig["score"] = round(min(1.0, sig["score"] + float(cfg.get("temporal_boost", 0.05))), 4)

        # 3. Edge selection: best signal PER TYPE per candidate (a pair can
        # carry several typed edges — that's the graph's richness), then the
        # per-document cap keeps storage bounded.
        edges = []
        for oid, entry in candidates.items():
            best_by_type: dict[str, dict] = {}
            for sig in entry["signals"]:
                rtype = sig["relation_type"]
                if rtype not in best_by_type or sig["score"] > best_by_type[rtype]["score"]:
                    best_by_type[rtype] = sig
            for rtype, sig in best_by_type.items():
                edges.append(
                    {
                        "source_doc_id": doc_id,
                        "target_doc_id": oid,
                        "relation_type": rtype,
                        "score": sig["score"],
                        "method": sig["method"],
                        "source_matter_id": row.get("matter_id"),
                        "target_matter_id": entry["other"].get("matter_id"),
                        "evidence": sig["evidence"],
                        "scanner_run_id": run_id,
                    }
                )
        edges.sort(key=lambda e: e["score"], reverse=True)
        edges = edges[: int(cfg.get("max_edges_per_doc", 50))]

        result = _run(R.record_edges(edges))
        report["edges_new"] = result.get("inserted", 0)
        report["edges_updated"] = result.get("updated", 0)

        # 4. Ledger: one event per NEW edge (novelty from the upsert result —
        # bounded detail, never a full-text dump) + the scan-complete marker.
        inserted_keys = {tuple(k) for k in result.get("inserted_keys") or []}
        for e in edges:
            src, dst = R.normalize_edge(e["source_doc_id"], e["target_doc_id"])
            if (src, dst, e["relation_type"]) in inserted_keys:
                _log_event(
                    "relation_recorded",
                    {
                        "source_doc_id": e["source_doc_id"],
                        "target_doc_id": e["target_doc_id"],
                        "relation_type": e["relation_type"],
                        "score": e["score"],
                        "method": e["method"],
                        "evidence": e["evidence"],
                        "run_id": run_id,
                    },
                )
        # 5. Per-document audit chain: relations_linked (best-effort).
        if edges:
            _write_doc_audit_event(doc_id, row.get("matter_id") or "", edges, run_id)

        _log_event(
            "relations_scan_completed",
            {
                "kind": "document",
                "doc_id": doc_id,
                "run_id": run_id,
                "candidates": len(candidates),
                "edges_new": report["edges_new"],
                "edges_updated": report["edges_updated"],
            },
        )
        report["ok"] = True
        return report
    except Exception as exc:
        logger.warning("relations_scan_failed", doc_id=doc_id, error=f"{type(exc).__name__}: {exc}")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report


def _write_doc_audit_event(doc_id: str, matter_id: str, edges: list[dict], run_id: str) -> None:
    """Append a ``relations_linked`` event to the DOCUMENT's own audit chain —
    best-effort; a failure never disturbs the relations path."""
    try:
        from schemas.audit import build_audit_entry
        from storage.audit_log import get_latest_audit_hash, write_audit_entry

        async def _write():
            prev = await get_latest_audit_hash(doc_id)
            entry = build_audit_entry(
                doc_id,
                matter_id,
                "relations_linked",
                "relations",
                {
                    "run_id": run_id,
                    "edges": [
                        {
                            "target_doc_id": e["target_doc_id"],
                            "relation_type": e["relation_type"],
                            "score": e["score"],
                        }
                        for e in edges[:10]
                    ],
                },
                prev_hash=prev,
            )
            await write_audit_entry(entry)

        _run(_write())
    except Exception:
        logger.debug("relations_doc_audit_write_failed", doc_id=doc_id)


def sweep(*, limit: int | None = None) -> dict:
    """Watermark-incremental archive sweep: scan archived documents the
    ledger has never seen (plus anything newer than the watermark)."""
    from sqlalchemy import func, select

    from storage import relations as R
    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    if not relations_enabled():
        return {"skipped": "disabled"}
    cfg = relations_config()
    run_id = R.new_scanner_run_id()
    batch = int(limit or cfg.get("scan_batch_size", 25))
    report: dict = {"run_id": run_id, "scanned": 0, "edges_new": 0, "edges_updated": 0}

    async def _pending():
        ensure_schema()
        async with async_session() as session:
            stmt = (
                select(DocumentRecord)
                .where(DocumentRecord.stage == "archived")
                .order_by(DocumentRecord.created_at.asc(), DocumentRecord.doc_id.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {"doc_id": r.doc_id, "created_at": r.created_at}
                for r in rows
            ]

    docs = _run(_pending())
    done_ids = set(_run(_scanned_doc_ids()))
    # Never-scanned documents first, oldest first (the watermark is a progress
    # marker for operators; ledger novelty is the actual gate).
    pending = [d for d in docs if d["doc_id"] not in done_ids][:batch]
    _log_event("relations_scan_started", {"kind": "sweep", "run_id": run_id, "pending": len(pending)})
    for d in pending:
        result = scan_document(d["doc_id"], run_id=run_id)
        report["scanned"] += 1
        report["edges_new"] += result.get("edges_new", 0)
        report["edges_updated"] += result.get("edges_updated", 0)
    if pending:
        _run(R.set_scan_state("watermark_doc_id", pending[-1]["doc_id"]))
        _run(R.set_scan_state("last_sweep_at", datetime.now(timezone.utc).isoformat()))
    report["pending_remaining"] = max(0, len([d for d in docs if d["doc_id"] not in done_ids]) - report["scanned"])
    _log_event(
        "relations_scan_completed",
        {"kind": "sweep", "run_id": run_id, **{k: report[k] for k in ("scanned", "edges_new", "edges_updated", "pending_remaining")}},
    )
    return report


def _scanned_doc_ids() -> list[str]:
    """Document ids that have already had a document-level scan (ledger truth,
    not a second state store)."""
    from sqlalchemy import select

    from storage import relations as R
    from storage.db import async_session, ensure_schema

    async def _q():
        ensure_schema()
        async with async_session() as session:
            stmt = select(R.RelationLogRecord.detail).where(
                R.RelationLogRecord.event == "relations_scan_completed",
                R.RelationLogRecord.detail["kind"].as_string() == "document",
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [str(d.get("doc_id")) for d in rows if isinstance(d, dict) and d.get("doc_id")]

    return _q()


# ---------------------------------------------------------------------------
# Context injection (agents + echo)


def context_block(
    *, matter_id: str | None = None, doc_id: str | None = None
) -> str:
    """Bounded, advisory RELATED block for agent prompts / the echo. Empty
    string when nothing exists (no noise) or injection is off. Best-effort:
    storage failures render as empty, never raise."""
    if not context_injection_enabled() or not relations_enabled():
        return ""
    try:
        from storage import relations as R

        rows = _run(
            R.relations_context(matter_id=matter_id, doc_id=doc_id, limit=int(relations_config().get("context_limit", 6)))
        )
        if not rows:
            return ""
        lines = ["RELATED (advisory — from the relations ledger):"]
        for row in rows:
            other = row.get("other") or {}
            name = other.get("original_filename") or row.get("other_doc_id", "?")
            omatter = other.get("matter_id")
            matter_note = f", matter {omatter}" if omatter and matter_id and omatter != matter_id else ""
            evidence = row.get("evidence") or {}
            hint = ""
            for key in ("shared", "cosine", "jaccard"):
                if evidence.get(key):
                    hint = f" [{key}: {evidence[key] if not isinstance(evidence[key], list) else ', '.join(map(str, evidence[key][:4]))}]"
                    break
            lines.append(
                f"- {row['relation_type']} (score {row['score']:.2f}): {name}{matter_note}{hint}"
            )
        return "\n".join(lines)
    except Exception:
        logger.debug("relations_context_block_failed")
        return ""


# ---------------------------------------------------------------------------
# Dispatch + background sweep


def dispatch_relations_scan(manifest) -> None:
    """Fire the post-archive relations pass off the document path (daemon
    thread, never raises) — the completion-echo pattern."""
    try:
        if not relations_enabled():
            return
        if not isinstance(manifest, dict):
            manifest = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else dict(manifest)
        doc_id = str(manifest.get("doc_id") or "")
        if not doc_id:
            return
        threading.Thread(
            target=_dispatch_safe,
            args=(doc_id,),
            name="relations-scan",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("relations_scan_dispatch_failed")


def _dispatch_safe(doc_id: str) -> None:
    try:
        scan_document(doc_id)
    except Exception:
        logger.exception("relations_scan_dispatch_run_failed", doc_id=doc_id)


class RelationsSweeper(threading.Thread):
    """Background archive sweep; daemon thread, one sweep per interval."""

    def __init__(self, interval_seconds: float | None = None):
        super().__init__(name="relations-sweeper", daemon=True)
        load_env()
        try:
            self.interval = float(
                interval_seconds
                or os.environ.get("MAILROOM_RELATIONS_SCAN_SECONDS", 300)
            )
        except (TypeError, ValueError):
            self.interval = 300.0
        self.interval = max(5.0, self.interval)
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("relations_sweeper_started", interval=self.interval)
        try:
            while not self._stop_event.is_set():
                try:
                    report = sweep()
                    if report.get("scanned"):
                        logger.info("relations_sweep_complete", **{k: report.get(k) for k in ("scanned", "edges_new", "pending_remaining")})
                except Exception:
                    logger.exception("relations_sweep_error")
                self._stop_event.wait(self.interval)
        finally:
            logger.info("relations_sweeper_stopped")

    def stop(self) -> None:
        self._stop_event.set()


def start_embedded_relations_scanner() -> RelationsSweeper | None:
    """Start the sweeper inside the watcher process (enabled-only, never
    raises) — the Gmail-poller pattern; watcher.lock stays the authority."""
    try:
        if not relations_enabled():
            return None
        sweeper = RelationsSweeper()
        sweeper.start()
        return sweeper
    except Exception:
        logger.exception("relations_sweeper_start_failed")
        return None


def stop_embedded_relations_scanner(sweeper: RelationsSweeper | None) -> None:
    if sweeper is None:
        return
    try:
        sweeper.stop()
    except Exception:
        logger.exception("relations_sweeper_stop_failed")


def main(argv: list[str] | None = None) -> int:
    """CLI: ``PYTHONPATH=src python -m pipeline.relations_scan [--full]
    [--doc DOC_ID] [--verify-ledger]``"""
    import argparse

    load_env()
    parser = argparse.ArgumentParser(description="Relations scanner (HUB-040)")
    parser.add_argument("--full", action="store_true", help="sweep ALL archived documents")
    parser.add_argument("--doc", help="scan a single document id")
    parser.add_argument("--verify-ledger", action="store_true", help="verify the hash chain and exit")
    args = parser.parse_args(argv)

    if args.verify_ledger:
        ok, count = verify_ledger()
        print(f"relations ledger: {'OK — hash chain intact' if ok else 'BROKEN — investigate immediately'} ({count} entries)")
        return 0 if ok else 1
    if args.doc:
        report = scan_document(args.doc)
        print(json.dumps(report, indent=2, default=str))
        return 0
    report = sweep(limit=None if args.full else None)  # batch size from config; --full keeps sweeping
    if args.full:
        while report.get("pending_remaining", 0) > 0:
            more = sweep()
            report["scanned"] += more.get("scanned", 0)
            report["edges_new"] += more.get("edges_new", 0)
            report["edges_updated"] += more.get("edges_updated", 0)
            report["pending_remaining"] = more.get("pending_remaining", 0)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
