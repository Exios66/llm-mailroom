"""Durable SQLite outbox for Gmail completion echoes (HUB-037).

An echo is first ``enqueue``d under a stable ``dedup_key`` (``INSERT OR IGNORE``
so a key is accepted exactly once, across restarts and in any state, including
``dead`` — a dead row is never auto-revived; an operator can delete it), then
delivered by ``drain_once`` — inline by ``send_intake_echo`` and in the
background by ``OutboxWorker``. A failed send is retried with exponential
backoff (``min(3600, 30 * 2**attempts)`` seconds) and goes ``dead`` after
``MAX_ATTEMPTS``.

Delivery is AT-LEAST-ONCE: a crash between SMTP success and the ``sent``
mark can resend one echo after the lease expires (same Message-ID, which Gmail
typically collapses).

Concurrency model: one connection per call (WAL + ``timeout=``), every DB
access under ``_DB_LOCK`` (in-process), and a whole-drain ``_DRAIN_LOCK`` so
the background worker and an inline drain in one process never overlap.
Across processes (two drainers on one DB) a row is claimed with a lease:
``UPDATE ... SET state='sending', lease_until=now+LEASE_SECONDS WHERE
dedup_key=? AND (state='pending' OR (state='sending' AND lease_until<now))``
and sent only when rowcount == 1; an expired lease is retried. SMTP I/O never
happens under the DB lock.
"""

from __future__ import annotations

import contextlib
import dataclasses
import email.message
import email.utils
import json
import sqlite3
import threading
import time

import structlog

from .bins import get_base_dir

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 8
POLL_SECONDS = 30.0
LEASE_SECONDS = 300.0
MAX_DRAIN_ROUNDS = 20
_DB_TIMEOUT = 15.0

_DB_LOCK = threading.RLock()
_DRAIN_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    dedup_key TEXT PRIMARY KEY,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    headers_json TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL,
    html TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL,
    lease_until REAL
)
"""


@dataclasses.dataclass
class OutboxRow:
    dedup_key: str
    to_addr: str
    subject: str
    headers: dict[str, str]
    text: str
    html: str | None
    attempts: int


def db_path():
    return get_base_dir() / "mail_outbox.sqlite"


@contextlib.contextmanager
def _db():
    """Yield a fresh connection under the module lock (closed on exit)."""
    with _DB_LOCK:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=_DB_TIMEOUT)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            conn.execute(_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if "lease_until" not in cols:  # DB created before the lease column existed
                conn.execute("ALTER TABLE outbox ADD COLUMN lease_until REAL")
            conn.execute("PRAGMA user_version=2")
            with conn:
                yield conn
        finally:
            conn.close()


def enqueue(
    dedup_key: str,
    *,
    to_addr: str,
    subject: str,
    text: str,
    html: str | None,
    headers: dict[str, str] | None = None,
) -> bool:
    """Queue a message. False when ``dedup_key`` already exists (any state)."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outbox (dedup_key, to_addr, subject, headers_json, text, html,"
            " state, attempts, next_attempt_at, created_at) VALUES (?,?,?,?,?,?,'pending',0,0,?)",
            (dedup_key, to_addr, subject, json.dumps(headers or {}), text, html, time.time()),
        )
        return cur.rowcount == 1


def row_state(dedup_key: str) -> str | None:
    """State of a row (``pending``/``sent``/``dead``) or None when absent."""
    with _db() as conn:
        r = conn.execute("SELECT state FROM outbox WHERE dedup_key=?", (dedup_key,)).fetchone()
    return r[0] if r else None


def count_recent(to_addr: str, window_s: float, now: float | None = None) -> int:
    """Rows created for ``to_addr`` within the last ``window_s`` seconds."""
    now = time.time() if now is None else now
    with _db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE to_addr=? AND created_at>=?",
            (to_addr, now - window_s),
        ).fetchone()
    return int(r[0])


def _build_message(row: OutboxRow, from_addr: str) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = row.to_addr
    msg["Subject"] = row.subject
    msg["Date"] = email.utils.formatdate(localtime=False)
    for k, v in row.headers.items():
        msg[k] = v
    msg.set_content(row.text)
    if row.html:
        msg.add_alternative(row.html, subtype="html")
    return msg


def _default_smtp_factory(cfg: dict):
    import smtplib

    from .gmail_intake import IMAP_TIMEOUT_SECONDS

    return lambda: smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=IMAP_TIMEOUT_SECONDS)


def _quit(client) -> None:
    if client is not None:
        try:
            client.quit()
        except Exception:
            pass


def drain_once(smtp_factory=None, now: float | None = None, limit: int = 10) -> dict:
    """Send due rows over ONE SMTP connection. Never raises."""
    result = {"sent": 0, "failed": 0, "dead": 0}
    try:
        with _DRAIN_LOCK:
            _drain_locked(result, smtp_factory, time.time() if now is None else now, limit)
    except Exception:
        logger.exception("mail_outbox_drain_failed")
        result["failed"] += 1
    return result


def _drain_locked(result: dict, smtp_factory, now: float, limit: int) -> None:
    with _db() as conn:
        due = conn.execute(
            "SELECT dedup_key, to_addr, subject, headers_json, text, html, attempts FROM outbox"
            " WHERE (state='pending' AND next_attempt_at<=?)"
            " OR (state='sending' AND lease_until<?) ORDER BY created_at LIMIT ?",
            (now, now, limit),
        ).fetchall()
    if not due:
        return
    rows = [
        OutboxRow(k, to, subj, json.loads(h or "{}"), text, html, att)
        for (k, to, subj, h, text, html, att) in due
    ]

    from . import gmail_intake

    client = None
    cfg: dict = {}
    conn_error: Exception | None = None
    try:
        cfg = gmail_intake.load_config()
        factory = smtp_factory or gmail_intake._INJECTED_SMTP_FACTORY or _default_smtp_factory(cfg)
        client = factory()
        client.login(cfg["address"], cfg["password"])
    except Exception as exc:
        conn_error = exc
        _quit(client)
        client = None

    try:
        for row in rows:
            try:
                if not _claim(row.dedup_key, now):
                    continue  # another drainer holds a live lease
            except Exception:
                logger.exception("mail_outbox_claim_failed", dedup_key=row.dedup_key)
                result["failed"] += 1
                continue
            err = conn_error
            if err is None:
                try:
                    if client is None:  # reconnect after a mid-batch failure
                        try:
                            client = factory()
                            client.login(cfg["address"], cfg["password"])
                        except Exception as exc:
                            conn_error = exc  # do not retry connects for the rest of the batch
                            _quit(client)
                            client = None
                            raise
                    client.sendmail(
                        cfg["address"], [row.to_addr], _build_message(row, cfg["address"]).as_bytes()
                    )
                except Exception as exc:
                    err = exc
                    _quit(client)
                    client = None
            if err is None:
                try:
                    _mark_sent(row.dedup_key, now)
                except Exception:
                    # Mail is out: never abort the batch or resend in this drain.
                    logger.exception("gmail_echo_sent_unmarked", dedup_key=row.dedup_key)
                result["sent"] += 1
                with contextlib.suppress(Exception):
                    gmail_intake._record_status(echoes_sent=gmail_intake._STATUS["echoes_sent"] + 1)
                logger.info("gmail_echo_sent", dedup_key=row.dedup_key, to=row.to_addr)
            else:
                try:
                    dead = _mark_failed(row, now, f"{type(err).__name__}: {err}")
                except Exception:
                    logger.exception("mail_outbox_mark_failed_error", dedup_key=row.dedup_key)
                    dead = False
                result["dead" if dead else "failed"] += 1
                if dead:
                    logger.warning("gmail_echo_dead", dedup_key=row.dedup_key, to=row.to_addr)
                    with contextlib.suppress(Exception):
                        gmail_intake._record_status(echoes_dead=gmail_intake._STATUS.get("echoes_dead", 0) + 1)
                logger.warning(
                    "gmail_echo_failed", dedup_key=row.dedup_key, attempts=row.attempts + 1,
                    dead=dead, error=f"{type(err).__name__}: {err}",
                )
    finally:
        _quit(client)


def _claim(key: str, now: float) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "UPDATE outbox SET state='sending', lease_until=? WHERE dedup_key=?"
            " AND (state='pending' OR (state='sending' AND lease_until<?))",
            (now + LEASE_SECONDS, key, now),
        )
        return cur.rowcount == 1


def _mark_sent(key: str, now: float) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE outbox SET state='sent', sent_at=?, last_error=NULL, lease_until=NULL WHERE dedup_key=?", (now, key)
        )


def _mark_failed(row: OutboxRow, now: float, error: str) -> bool:
    attempts = row.attempts + 1
    dead = attempts >= MAX_ATTEMPTS
    with _db() as conn:
        conn.execute(
            "UPDATE outbox SET attempts=?, next_attempt_at=?, last_error=?, state=?, lease_until=NULL WHERE dedup_key=?",
            (attempts, now + min(3600, 30 * 2**attempts), error[:500], "dead" if dead else "pending", row.dedup_key),
        )
    return dead


class OutboxWorker(threading.Thread):
    """Daemon thread that drains the outbox on wake and every ``POLL_SECONDS``."""

    def __init__(self, poll_seconds: float = POLL_SECONDS):
        super().__init__(name="mail-outbox", daemon=True)
        self._poll = poll_seconds
        self._event = threading.Event()
        self._stop_flag = False

    def wake(self) -> None:
        self._event.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_flag = True
        self._event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def run(self) -> None:
        while not self._stop_flag:
            self._event.wait(self._poll)
            self._event.clear()
            if self._stop_flag:
                break
            try:
                for _ in range(MAX_DRAIN_ROUNDS):  # work off a backlog > limit
                    r = drain_once()
                    if self._stop_flag or not (r["sent"] or r["dead"]):
                        break
            except Exception:  # drain_once never raises; belt and braces
                logger.exception("mail_outbox_worker_error")


_WORKER: OutboxWorker | None = None


def get_worker() -> OutboxWorker | None:
    return _WORKER


def start_outbox_worker() -> OutboxWorker | None:
    """Start the worker (no-op when the Gmail channel is disabled; never raises).

    Runs whenever the channel is on — echoes, acknowledgments, reject replies
    and digests all drain through it.
    """
    global _WORKER
    try:
        if _WORKER is not None and _WORKER.is_alive():
            return _WORKER
        from .gmail_intake import gmail_intake_enabled

        if not gmail_intake_enabled():
            return None
        worker = OutboxWorker()
        worker.start()
        _WORKER = worker
        worker.wake()  # pick up rows left over from a previous run
        return worker
    except Exception:
        logger.exception("mail_outbox_worker_start_failed")
        return None


def stop_outbox_worker(worker: OutboxWorker | None) -> None:
    global _WORKER
    if worker is not None:
        with contextlib.suppress(Exception):
            worker.stop()
    if _WORKER is worker:
        _WORKER = None
