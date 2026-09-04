"""Gmail pilot lab bench — one mailroom-corpus document, fired through the
agent mailbox, processed by the live watcher, verified end-to-end (HUB-053).

The document ALWAYS derives from the mailroom-corpus HuggingFace dataset
(``Lucius-Morningstar/mailroom-corpus``, schema v8): offline mode (default)
serves the committed, integrity-verified snapshot
(``notebooks/fixtures/gmail_pilot_corpus_snapshot.json`` — labels from the
``ground_truth`` config joined with ``doc_text`` from the blind ``default``
config on ``filename``); live mode (``MAILROOM_HF_LIVE`` or ``live=True``)
goes through the canonical ``pipeline/hf_corpus_loader`` (the repo's ONE
HF-loading path — /parquet + sha-verified join, never ad-hoc fetches).

The pilot is a human-supervised loop: this module never sends anything on
its own — the notebook's FIRE interlock cell decides. Everything is logged
to ``data/pilot_runs/<stamp>_<token>/`` (report.json + report.md).

Docstring provenance rule (mirrors pipeline_lab.py): each public helper
names the test-suite / script pattern it mirrors. Network imports live
INSIDE functions (notebook guard-suite Duty 4).
"""

from __future__ import annotations

import email.message
import email.utils
import json
import os
import smtplib
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Kernel-cwd-proof bootstrap (mirrors notebooks/pipeline_lab.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pipeline.env import load_env  # noqa: E402

load_env()

SNAPSHOT_PATH = REPO_ROOT / "notebooks" / "fixtures" / "gmail_pilot_corpus_snapshot.json"
TERMINAL_STAGES = ("archived", "failed", "review")


# ---------------------------------------------------------------------------
# Corpus selection (the data ALWAYS derives from mailroom-corpus)


def live_requested() -> bool:
    """Mirror notebooks/huggingface_lab.live_requested — the opt-in flag."""
    return str(os.environ.get("MAILROOM_HF_LIVE", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def select_document(
    *,
    doc_class: str | None = "insurance_claim",
    role: str | None = None,
    live: bool | None = None,
    max_chars: int | None = None,
    seed: int | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Pick ONE pilot document from mailroom-corpus.

    Offline (default): the committed snapshot, filtered by ``role`` or
    ``doc_class``. Live: the canonical ``pipeline.hf_corpus_loader.load_corpus``
    (labels + text joined on filename, content_sha256-verified) filtered by
    class/length with a deterministic ``seed``. Returns a dict with
    ``labels``, ``doc_text`` and ``provenance`` (source, dataset, sha).
    """
    if live is None:
        live = live_requested()
    if not live:
        data = json.loads(SNAPSHOT_PATH.read_text())
        rows = []
        for row in data.get("rows") or []:
            labels = row.get("labels") or {}
            if role and row.get("role") != role:
                continue
            if not role and doc_class and str(labels.get("expected")) != doc_class:
                continue
            text = str(row.get("doc_text") or "")
            if max_chars is not None and len(text) > max_chars:
                continue
            rows.append(row)
        if not rows:
            raise RuntimeError(
                f"no snapshot row matches role={role!r} doc_class={doc_class!r} "
                f"max_chars={max_chars!r} — refresh the snapshot (live cell) or widen the filter"
            )
        picked = rows[index] if index is not None else rows[0]
        picked = dict(picked)
        picked["provenance"] = {
            "source": "committed-snapshot",
            "dataset": data.get("dataset"),
            "hub_sha": data.get("hub_sha"),
            "snapshot_fetched_at": data.get("fetched_at"),
        }
        return picked

    from pipeline.hf_corpus_loader import load_corpus, pick_document

    merged, prov = load_corpus()
    row = pick_document(
        merged,
        doc_class=doc_class,
        index=index,
        seed=seed,
        max_chars=max_chars,
    )
    labels = {k: v for k, v in row.items() if k not in ("doc_text", "_selection")}
    return {
        "role": None,
        "labels": {
            k: (v if isinstance(v, str) else (None if v is None else str(v)))
            for k, v in labels.items()
        },
        "doc_text": str(row.get("doc_text") or ""),
        "provenance": {
            "source": "live-corpus-load",
            "dataset": prov.get("dataset"),
            "hub_sha_tip": prov.get("hub_sha_tip"),
            "integrity": prov.get("integrity"),
            "selection": row.get("_selection"),
        },
    }


# ---------------------------------------------------------------------------
# Attachment + email build (mirrors scripts/gmail_smoke_test.build_smoke_email)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_attachment(doc: dict, workdir: Path, *, stamp: str | None = None) -> tuple[Path, str]:
    """Write the corpus document to a pilot attachment file.

    The original corpus filename is kept when its extension is an accepted
    intake type (the Gmail poller's extension guard); otherwise the text is
    shipped as ``.txt`` — the content itself is unchanged either way.
    """
    from pipeline.bins import accepted_extensions

    stamp = stamp or _now_stamp()
    original = str((doc.get("labels") or {}).get("filename") or "corpus_doc")
    ext = Path(original).suffix.lower()
    if ext not in accepted_extensions():
        ext = ".txt"
    name = f"pilot_{stamp}_{Path(original).stem[:48]}{ext}"
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / name
    path.write_text(str(doc.get("doc_text") or ""), encoding="utf-8")
    return path, name


def mailbox_address() -> str:
    from pipeline.gmail_intake import gmail_address

    return gmail_address()


def build_pilot_email(
    attachment_path: Path,
    matter_id: str,
    *,
    stamp: str | None = None,
    recipient: str | None = None,
    sender: str | None = None,
    subject_prefix: str = "Corpus pilot",
) -> tuple[bytes, str, str]:
    """One single-document pilot email (subject carries the ``[M:<matter>]``
    tag) — the exact MIME shape the intake poller parses (smoke-test mirror)."""
    stamp = stamp or _now_stamp()
    address = sender or mailbox_address()
    message_id = f"<gmail-pilot-{uuid.uuid4().hex[:12]}@mailroom.local>"
    msg = email.message.EmailMessage()
    msg["From"] = address
    msg["To"] = recipient or address
    msg["Subject"] = f"{subject_prefix} {stamp} [M:{matter_id}]"
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg.set_content(
        "Mailroom corpus pilot — one document from the mailroom-corpus "
        "HuggingFace dataset for the watcher to process."
    )
    payload = Path(attachment_path).read_bytes()
    msg.add_attachment(
        payload,
        maintype="application",
        subtype="octet-stream",
        filename=Path(attachment_path).name,
    )
    return msg.as_bytes(), message_id, Path(attachment_path).name


# ---------------------------------------------------------------------------
# Fire paths (NEVER called without the notebook's FIRE interlock)


def send_via_smtp(raw: bytes, *, recipient: str | None = None) -> dict[str, Any]:
    """Real send through the Gmail SMTP channel (smoke-test --real mirror).

    Credentials come from .env (GMAIL_ADDRESS / GMAIL_APP_PASSWORD) and are
    never printed. The sender MUST be allowlisted — the watcher rejects
    non-allowlisted senders (gmail_message_sender_rejected) — so this fails
    fast with an actionable message instead of a silent pilot timeout.
    """
    from pipeline.gmail_intake import load_config

    cfg = load_config()
    sender = cfg["address"]
    if sender.strip().lower() not in cfg["allowed_senders"]:
        raise RuntimeError(
            f"sender {sender} is NOT in MAILROOM_GMAIL_ALLOWED_SENDERS — the watcher "
            f"would reject the message (gmail_message_sender_rejected). Allowlist it first."
        )
    to = recipient or sender
    with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
        server.login(sender, cfg["password"])
        refused = server.sendmail(sender, [to], raw)
    return {"to": to, "smtp_host": cfg["smtp_host"], "refused": refused}


def deliver_mock(
    attachment_path: Path, matter_id: str, message_id: str, *, stamp: str | None = None
) -> dict[str, Any]:
    """Network-free rehearsal: deliver the attachment into the inbox bin with
    the poller's EXACT sidecar meta (gmail_intake.deliver_attachment mirror)
    — the live watcher then processes it through the triage lane exactly as
    if the email had arrived (route: triage)."""
    from pipeline.gmail_intake import deliver_attachment, load_config

    content = Path(attachment_path).read_bytes()
    meta = {
        "matter_id": matter_id,
        "source": "gmail",
        "message_id": message_id,
        "sender": mailbox_address(),
        "subject": f"Corpus pilot {stamp or _now_stamp()} [M:{matter_id}]",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "route": "triage",
        "upload_id": uuid.uuid4().hex[:12],
        "size": len(content),
        "original_filename": Path(attachment_path).name,
        "_max_attachment_bytes": load_config()["max_attachment_bytes"],
    }
    delivered, reject_reason = deliver_attachment(Path(attachment_path).name, content, meta)
    return {"delivered": delivered, "reject_reason": reject_reason, "message_id": message_id}


# ---------------------------------------------------------------------------
# Watching (incremental watcher-log tail — no IMAP: the log is the record)


class WatchLog:
    """Incremental tail of ``data/watcher.out`` (byte-offset based), with
    event filtering by a correlation token (attachment stem / message-id).
    The offset is seeded at construction — drain() sees only what arrives
    after the WatchLog was created."""

    def __init__(self, path: Path | None = None):
        from pipeline.bins import get_base_dir

        self.path = Path(path) if path else get_base_dir() / "watcher.out"
        self.offset = self.path.stat().st_size if self.path.exists() else 0
        self.events: list[dict] = []

    def new_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # rotated/truncated — re-read from the top
            self.offset = 0
        with open(self.path, "r", errors="replace") as fh:
            fh.seek(self.offset)
            lines = fh.read().splitlines()
            self.offset = fh.tell()
        return lines

    def drain(self, token: str) -> list[dict]:
        """Collect new log lines mentioning ``token`` into ``self.events``
        (timestamp + text); returns what was added."""
        added = []
        for line in self.new_lines():
            if token in line:
                event = {"ts": _line_ts(line), "line": line}
                self.events.append(event)
                added.append(event)
        return added


def _line_ts(line: str) -> str | None:
    for token in line.split(" "):
        if token[:4].isdigit() and token[4:5] == "-" and "T" in token:
            return token
    return None


# ---------------------------------------------------------------------------
# Terminal-manifest wait (polls data/manifests — the durable record)


def await_terminal(
    attachment_name: str, *, timeout_s: float = 900.0, poll_s: float = 5.0
) -> tuple[dict | None, float]:
    """Poll the manifests dir until a TERMINAL manifest for
    ``original_filename == attachment_name`` appears. Returns (manifest, elapsed)."""
    from pipeline.bins import manifests_dir

    start = time.time()
    mdir = manifests_dir()
    while time.time() - start < timeout_s:
        if mdir.exists():
            for mf in sorted(mdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    data = json.loads(mf.read_text())
                except Exception:
                    continue
                if str(data.get("original_filename")) == attachment_name and str(
                    data.get("stage")
                ) in TERMINAL_STAGES:
                    return data, time.time() - start
        time.sleep(poll_s)
    return None, time.time() - start


# ---------------------------------------------------------------------------
# Preflight (watcher liveness + channel posture — the FIRE decision inputs)


def heartbeat_status(max_age_s: float = 120.0) -> dict[str, Any]:
    """Watcher heartbeat read: fresh/pid/age/sha (HUB-050 enriched format)."""
    from pipeline.bins import get_base_dir

    path = get_base_dir() / "watcher_heartbeat"
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {"fresh": False, "note": f"no heartbeat ({type(exc).__name__})"}
    age = time.time() - float(data.get("ts") or 0)
    return {
        "fresh": age <= max_age_s,
        "age_s": round(age, 1),
        "pid": data.get("pid"),
        "sha": data.get("sha"),
        "started_at": data.get("started_at"),
    }


def gmail_channel_status() -> dict[str, Any]:
    """The intake channel's posture: enabled, address, allowlist (incl. whether
    the mailbox itself may send), free-only guardrail — credentials never."""
    from pipeline.gmail_intake import load_config

    cfg = load_config()
    return {
        "address": cfg["address"],
        "enabled": os.environ.get("MAILROOM_GMAIL_ENABLED", "").lower() in ("1", "true", "yes", "on"),
        "allowed_senders": sorted(cfg["allowed_senders"]),
        "mailbox_allowlisted": cfg["address"].strip().lower() in cfg["allowed_senders"],
        "free_only_guardrail": os.environ.get("MAILROOM_LLM_FREE_ONLY", "") in ("1", "true", "yes", "on"),
        "smtp_host": cfg["smtp_host"],
        "poll_seconds": cfg["poll_seconds"],
    }


# ---------------------------------------------------------------------------
# Evidence collection (catalog / audit / relations / echo / debug I/O)


def _run_coro(coro):
    """Sync bridge for async storage reads that also works inside a Jupyter
    kernel. asyncio.run raises in a kernel (a loop is already running), and a
    fresh loop cannot run while the kernel's loop is running either — so the
    coroutine runs on a fresh thread, which always owns its own loop."""
    import asyncio
    import concurrent.futures

    try:
        return asyncio.run(coro)
    except RuntimeError:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


def collect_evidence(manifest: dict | None, watchlog: WatchLog, token: str = "") -> dict[str, Any]:
    """Everything the report needs about the fired document. Every leg is
    fail-soft: an unavailable surface degrades to a null/note, never raises
    (mirrors the smoke test's checks philosophy)."""
    evidence: dict[str, Any] = {}
    doc_id = str((manifest or {}).get("doc_id") or "")
    matter_id = str((manifest or {}).get("matter_id") or "")

    if doc_id:
        try:
            import asyncio

            from storage.catalog import get_document

            row = _run_coro(get_document(doc_id))
            evidence["catalog_row"] = (
                {
                    "doc_id": row.doc_id,
                    "stage": row.stage,
                    "doc_type": row.doc_type,
                    "doc_subclass": row.doc_subclass,
                    "classification_confidence": row.classification_confidence,
                    "file_sha256": row.file_sha256,
                }
                if row
                else None
            )
        except Exception as exc:
            evidence["catalog_row"] = None
            evidence["catalog_note"] = f"{type(exc).__name__}: {exc}"
        try:
            import asyncio

            from schemas.audit import AuditLogEntry, verify_chain
            from storage.audit_log import get_audit_chain

            entries = _run_coro(get_audit_chain(doc_id))
            audit = [
                AuditLogEntry(
                    entry_id=e["entry_id"],
                    doc_id=doc_id,  # the chain dicts omit doc_id — it is the key we queried by
                    matter_id=e["matter_id"],
                    event=e["event"],
                    actor=e["actor"],
                    detail=e["detail"],
                    prev_hash=e["prev_hash"],
                    entry_hash=e["entry_hash"],
                    timestamp=e["timestamp"],
                )
                for e in entries
            ]
            evidence["audit_events"] = [e.event for e in audit]
            evidence["audit_chain_ok"] = verify_chain(audit)
        except Exception as exc:
            evidence["audit_events"] = []
            evidence["audit_note"] = f"{type(exc).__name__}: {exc}"
        try:
            import asyncio

            from sqlalchemy import select

            from storage.db import async_session, ensure_schema
            from storage.relations import RelationEdgeRecord

            async def _edges():
                ensure_schema()
                async with async_session() as session:
                    stmt = select(RelationEdgeRecord).where(
                        (RelationEdgeRecord.source_doc_id == doc_id)
                        | (RelationEdgeRecord.target_doc_id == doc_id)
                    )
                    rows = (await session.execute(stmt)).scalars().all()
                    return [
                        {
                            "source": r.source_doc_id[:8],
                            "target": r.target_doc_id[:8],
                            "type": r.relation_type,
                            "score": r.score,
                            "method": r.method,
                        }
                        for r in rows
                    ]

            evidence["relations_edges"] = _run_coro(_edges())
        except Exception:
            evidence["relations_edges"] = []
        try:
            from pipeline.relations import context_block

            evidence["relations_context"] = context_block(matter_id=matter_id, doc_id=doc_id)
        except Exception:
            evidence["relations_context"] = ""
        try:
            from pipeline.bins import get_base_dir

            debug_root = get_base_dir() / "debug" / "triage"
            dirs = sorted(debug_root.iterdir(), key=lambda p: p.name) if debug_root.exists() else []
            evidence["triage_debug_dirs"] = [p.name for p in dirs if token and token in p.name][-3:]
        except Exception:
            evidence["triage_debug_dirs"] = []
    evidence["log_events"] = list(watchlog.events)
    return evidence


# ---------------------------------------------------------------------------
# Ground-truth comparison (the corpus-derived verdict)


def ground_truth_checks(manifest: dict | None, doc: dict) -> list[dict[str, Any]]:
    """Pipeline output vs the corpus row's ground truth (class / subclass /
    stage + insurance fields when the corpus row carries them)."""
    labels = doc.get("labels") or {}
    triage = ((manifest or {}).get("intake") or {}).get("triage") or {}
    extraction = triage.get("extraction") or {}
    checks: list[dict[str, Any]] = []

    def add(name: str, expected: Any, actual: Any, *, soft: bool = False) -> None:
        has_expected = str(expected or "").strip() != ""
        ok = has_expected and str(expected).strip().lower() == str(actual or "").strip().lower()
        checks.append(
            {
                "check": name,
                "expected": expected,
                "actual": actual,
                "ok": bool(ok or (soft and not has_expected)),
                "soft": soft,
            }
        )

    add(
        "doc_class",
        labels.get("expected"),
        (manifest or {}).get("doc_type") or triage.get("primary_doc_class"),
    )
    add(
        "doc_subclass",
        labels.get("expected_subclass"),
        (manifest or {}).get("doc_subclass") or triage.get("doc_subclass"),
        soft=True,
    )
    add("stage", labels.get("expected_stage"), (manifest or {}).get("stage"))
    for field in (
        "claim_number",
        "policy_number",
        "insurer",
        "insured_party",
        "claim_type",
        "date_of_loss",
        "coverage_determination",
    ):
        expected = labels.get(field)
        if str(expected or "").strip():
            add(f"extraction.{field}", expected, extraction.get(field))
    return checks


# ---------------------------------------------------------------------------
# Report


def render_report(run: dict) -> str:
    """Markdown pilot report (the human-readable log)."""
    checks = run.get("checks") or []
    fired = run.get("fired") or {}
    prov = (run.get("doc") or {}).get("provenance") or {}
    lines = [
        f"# Gmail corpus pilot — {run.get('token')}",
        "",
        f"- ran at: {run.get('stamp')} (UTC)",
        f"- mode: `{run.get('mode')}` | fired: `{fired.get('fired')}`",
        f"- document source: `{prov.get('source')}` "
        f"(dataset `{prov.get('dataset')}`, sha `{prov.get('hub_sha') or prov.get('hub_sha_tip')}`)",
        f"- attachment: `{run.get('attachment_name')}` | matter: `{run.get('matter_id')}`",
        "",
        "## Ground-truth checks",
        "",
        "| check | expected | actual | ok |",
        "|---|---|---|---|",
    ]
    for c in checks:
        lines.append(f"| {c['check']} | {c['expected']} | {c['actual']} | {'yes' if c['ok'] else 'NO'} |")
    manifest = run.get("manifest") or {}
    triage = (manifest.get("intake") or {}).get("triage") or {}
    elapsed = f" after {run.get('elapsed_s', 0):.0f}s" if manifest else ""
    lines += [
        "",
        "## Outcome",
        "",
        f"- terminal stage: `{manifest.get('stage') or 'NOT REACHED'}`{elapsed}",
        f"- classification: `{triage.get('primary_doc_class') or manifest.get('doc_type')}` "
        f"@ confidence `{triage.get('confidence') or manifest.get('classification_confidence')}`",
        f"- audit chain: `{(run.get('evidence') or {}).get('audit_chain_ok')}` "
        f"(events: {(run.get('evidence') or {}).get('audit_events')})",
        f"- relations edges: {len((run.get('evidence') or {}).get('relations_edges') or [])}",
        f"- watcher-log events correlated: {len((run.get('evidence') or {}).get('log_events') or [])}",
        "",
        "## Verdict",
        "",
        f"**{run.get('verdict')}**",
        "",
    ]
    return "\n".join(lines)


def write_report(run: dict) -> dict[str, Path]:
    """Persist report.json + report.md under data/pilot_runs/<stamp>_<token>/."""
    from pipeline.bins import get_base_dir

    out_dir = get_base_dir() / "pilot_runs" / f"{run.get('stamp')}_{run.get('token')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(run, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md_path.write_text(render_report(run), encoding="utf-8")
    return {"json": json_path, "md": md_path}
