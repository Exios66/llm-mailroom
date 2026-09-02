import os
import signal
import time
import threading
import structlog
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .env import load_env

load_env()
from pipeline.env import default_environment

default_environment("live")

from .logging import setup_logging

setup_logging()

logger = structlog.get_logger(__name__)

# O-1: kick the score-config warm-up off the document path at startup.
from observability.scores import warmup_score_configs
from observability.tracing import install_on_dropped

install_on_dropped()  # O-3: dropped trace events log a warning, never vanish

warmup_score_configs(blocking=False)
from observability.field_scoring import warm_embedding_model

warm_embedding_model(blocking=False)  # O-10: load embeddings off the document path

from .bins import (
    inbox_dir,
    ensure_dirs,
    list_inbox_files,
    get_worker_id,
    claim_file,
    is_ingestion_paused,
    list_stale_processing_files,
    reconcile_stale_processing_file,
    accepted_extensions,
    read_inbox_meta,
    touch_watcher_heartbeat,
)
from graph.build_graph import build_graph, run_pipeline

logger = structlog.get_logger(__name__)


def _finalize_claimed_on_error(
    claimed: Path | None,
    matter_id: str,
    reason: str,
    intake_meta: dict | None = None,
) -> None:
    """Move a claimed file to failed/ if run_pipeline raised outside the graph.

    The graph's ``_finalize_aborted`` already handles crashes inside
    ``_execute_run``. This covers the watcher wrapper itself (import/setup
    failures after ``claim_file``).
    """
    if claimed is None:
        return
    try:
        from graph.build_graph import _finalize_aborted

        _finalize_aborted(
            {
                "file_path": str(claimed),
                "original_filename": claimed.name,
                "matter_id": matter_id or "DEFAULT",
                **({"intake_meta": dict(intake_meta)} if intake_meta else {}),
            },
            reason,
        )
    except Exception:
        logger.exception("watcher_finalize_failed", file=str(claimed))

# In-flight processing guard: a file name may be claimed by only one thread at
# a time (watchdog's on_created + the startup scan race on the same inbox
# file, which produced duplicate pipeline runs in the pilot). Keyed by file
# name because `claim_file` moves the file (path changes mid-run).
_active_files: set[str] = set()
_active_lock = threading.Lock()

TERMINAL_STAGES = ("archived", "failed", "review")

# Stale-claim cutoff for startup reconciliation (L-1/A-18): claims older than
# this are presumed orphaned by a crashed process and re-queued.
STALE_CLAIM_MINUTES = int(os.environ.get("WATCHER_STALE_CLAIM_MINUTES", "60"))

WATCHER_LOCK_NAME = "watcher.lock"

# Default 1s so The-Mailroom's live floor sees inbox drops within one poll
# tick. Override with WATCHER_POLL_INTERVAL_SECONDS.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# In-process guard: flock is per-fd, so the same process can take the lock
# twice. The API lifespan and a nested start() must not double-run.
_watcher_owned = False
_watcher_owned_lock = threading.Lock()


class WatcherLockHeld(RuntimeError):
    """Another watcher already holds ``watcher.lock`` (or this process already started)."""


class _WatcherLock:
    """Exclusive file lock so the API-embedded watcher and ``python -m pipeline.watcher`` cannot both drain the inbox."""

    def __init__(self, path: Path, fh):
        self.path = path
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass


def acquire_watcher_lock() -> _WatcherLock | None:
    """Non-blocking exclusive lock on ``<MAILROOM_BASE_DIR>/watcher.lock``.

    Returns ``None`` when another process already holds it. Best-effort on
    platforms without ``fcntl`` (lock skipped, in-process flag still applies).
    """
    from pipeline.bins import get_base_dir

    path = get_base_dir() / WATCHER_LOCK_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        logger.warning("watcher_lock_open_failed", path=str(path), exc_info=True)
        return None
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    except ImportError:
        # Non-Unix: skip the kernel lock; in-process ownership still holds.
        pass
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return _WatcherLock(path, fh)


def embed_watcher_enabled() -> bool:
    """Whether the API lifespan should drain the inbox itself.

    Default ON outside pytest so ``python -m api.main`` does not leave
    uploads sitting until someone starts ``python -m pipeline.watcher``.
    Set ``MAILROOM_EMBED_WATCHER=0`` when a dedicated watcher process holds
    ``watcher.lock``. Tests stay off unless the env is explicitly ``1``.
    """
    raw = os.environ.get("MAILROOM_EMBED_WATCHER")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return not os.environ.get("PYTEST_CURRENT_TEST")


def _mark_active(name: str) -> bool:
    with _active_lock:
        if name in _active_files:
            return False
        _active_files.add(name)
        return True


def _unmark_active(name: str) -> None:
    with _active_lock:
        _active_files.discard(name)


def _is_already_processed(path: Path) -> bool:
    """Skip files that already reached a terminal stage.

    The pipeline persists a manifest per document (`manifests/<doc_id>.json`);
    if a manifest for this filename already shows archived/failed/review, the
    file was handled and must not be claimed again (pilot: watcher re-claimed
    files after crashes, producing 2-3 full pipeline runs per document and
    10-20x inflated trace latencies).
    """
    try:
        import json as _json
        from pipeline.bins import manifests_dir

        mdir = manifests_dir()
        if not mdir.exists():
            return False
        for mf in mdir.glob("*.json"):
            try:
                data = _json.loads(mf.read_text())
            except Exception:
                continue
            if data.get("original_filename") != path.name:
                continue
            if data.get("stage") in TERMINAL_STAGES:
                return True
    except Exception:
        logger.exception("manifest_scan_failed", file=str(path))
    return False


# ---------------------------------------------------------------------------
# Intake provenance (HUB-037) — shared by BOTH handler classes.
#
# `_infer_matter_id` is module-level on purpose: `Watcher._process_existing`
# calls it too, and historically only `InboxHandler` carried the method (a
# latent bug — any inbox file found by the startup scan / periodic rescan
# instead of a watchdog event crashed into `existing_file_failed`).
# ---------------------------------------------------------------------------

_MATTER_ID_SUFFIX_MAX = 10


def _infer_matter_id(path: Path) -> str:
    """Matter id for an inbox file: meta sidecar > parent folder > name suffix.

    Upload metadata wins: `/upload` (and the Gmail poller) write a
    `<file>.meta` sidecar carrying the submitted matter_id, so the document is
    filed under the matter the caller chose instead of a filename heuristic.
    The sidecar is read while the file is still in the inbox (before claim
    moves it).
    """
    meta = read_inbox_meta(path)
    if meta and meta.get("matter_id"):
        return str(meta["matter_id"])
    parent_matter = path.parent.name
    if parent_matter and parent_matter != inbox_dir().name:
        return parent_matter
    stem = path.stem
    parts = stem.rsplit("_", 1)
    if (
        len(parts) == 2
        and parts[1].upper() == parts[1]
        and len(parts[1]) <= _MATTER_ID_SUFFIX_MAX
    ):
        return parts[1]
    return "DEFAULT"


# Intake-provenance keys copied from the inbox `<file>.meta` sidecar into the
# document manifest (`DocumentManifest.intake`). A whitelist — never the raw
# sidecar. A sidecar without a `source` key is the API `/upload` route;
# Gmail sidecars carry `source: gmail`.
_INTAKE_META_KEYS = (
    "source",
    "sender",
    "subject",
    "message_id",
    "received_at",
    "upload_id",
    "uploaded_at",
    "original_filename",
)


def _intake_meta_from_sidecar(meta: dict | None) -> dict:
    if not meta:
        return {}
    intake = {k: meta[k] for k in _INTAKE_META_KEYS if meta.get(k) is not None}
    if "source" not in intake:
        intake["source"] = "upload"
    return intake


def _intake_context(path: Path) -> tuple[str, dict]:
    """(matter_id, intake_meta) for an inbox file, read BEFORE claim_file moves it."""
    return _infer_matter_id(path), _intake_meta_from_sidecar(read_inbox_meta(path))


def _notify_intake_reaction(intake_meta: dict, *, async_mode: bool = True) -> None:
    """React to the source email with a check emoji once it is claimed.

    Fires ONLY for Gmail-channel documents (``source: gmail`` in the intake
    sidecar) at the moment the watcher picks the attachment up for
    processing. Best-effort and asynchronous (daemon thread): a reaction can
    never delay or fail a claim. Disable with MAILROOM_GMAIL_REACTIONS=0.
    """
    if not intake_meta or intake_meta.get("source") != "gmail":
        return
    message_id = intake_meta.get("message_id")
    if not message_id:
        return
    try:
        from .gmail_intake import reactions_enabled, react_to_message

        if not reactions_enabled():
            return
        if async_mode:
            threading.Thread(
                target=react_to_message,
                args=(str(message_id),),
                name="gmail-reaction",
                daemon=True,
            ).start()
        else:
            react_to_message(str(message_id))
    except Exception:
        logger.exception("gmail_reaction_dispatch_failed", message_id=str(message_id))


def _notify_intake_triage(file_path: Path, intake_meta: dict) -> None:
    """Pre-pipeline intake triage for Gmail-channel documents (HUB-037).

    Runs BEFORE ``run_pipeline`` on Gmail-intake attachments: reads the same
    document text the pipeline will see and produces the advisory intake log
    (primary doc class, subclass when discernible, confidence, gist,
    keywords) that rides ``intake_meta["triage"]`` into the terminal manifest
    and the completion echo. Advisory by design — the read never influences
    the pipeline's own classification or routing. Fails soft: a missing key,
    rate limit, or provider error must never block a claim (logged; the
    pipeline proceeds without a triage record). Disable with
    ``MAILROOM_GMAIL_TRIAGE=0``.
    """
    if not intake_meta or intake_meta.get("source") != "gmail":
        return
    try:
        from .gmail_intake import triage_enabled

        if not triage_enabled():
            return
        from agents.gmail_triage import GmailTriageAgent
        from graph.build_graph import _read_file_text

        doc_text, _ = _read_file_text(file_path)
        agent = GmailTriageAgent()
        result = agent.triage(doc_text, filename=file_path.name)
        if isinstance(result, dict) and result:
            intake_meta["triage"] = result
            logger.info(
                "gmail_triage_complete",
                file=str(file_path),
                doc_class=result.get("primary_doc_class"),
                confidence=result.get("confidence"),
            )
    except Exception:
        logger.exception("gmail_triage_failed", file=str(file_path))


class InboxHandler(FileSystemEventHandler):
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._debounce: dict[str, float] = {}

    def _is_processable(self, path: Path) -> bool:
        """Only processable documents enter the conveyor.

        Without this filter, watchdog fires for anything written into the
        inbox — including the upload-metadata `.meta` sidecar written by
        `/upload`, which would otherwise be claimed and processed as a
        document. The periodic rescan already restricts to
        `accepted_extensions()`.
        """
        return path.suffix.lower() in accepted_extensions()

    def _schedule(self, path: Path) -> None:
        """Debounced claim for created / modified / moved-into-inbox events.

        ``Path.write_bytes`` (API ``/upload``) typically fires created then
        modified. ``mv`` into the inbox fires moved, not created, on inotify.
        """
        cfg = inbox_dir()
        try:
            path = path.resolve()
            inbox = cfg.resolve()
        except OSError:
            path = Path(path)
            inbox = cfg
        try:
            if not path.is_relative_to(inbox):
                return
        except (ValueError, TypeError):
            if not str(path).startswith(str(inbox)):
                return
        if not self._is_processable(path):
            logger.debug("inbox_file_ignored_extension", file=str(path))
            return
        now = time.time()
        if path.name in self._debounce and (now - self._debounce[path.name]) < 1.0:
            return
        self._debounce[path.name] = now
        logger.info("inbox_file_detected", file=str(path))
        threading.Thread(target=self._process, args=(path,), daemon=True).start()

    def on_created(self, event):
        if event.is_directory:
            return
        self._schedule(Path(event.src_path))

    def on_modified(self, event):
        if event.is_directory:
            return
        self._schedule(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None) or event.src_path
        self._schedule(Path(dest))

    # Intake provenance is resolved by the module-level `_intake_context`
    # (shared with Watcher._process_existing — see its comment above).

    def _process(self, path: Path):
        if not _mark_active(path.name):
            logger.info("file_already_processing", file=str(path))
            return
        claimed = None
        matter_id = "DEFAULT"
        intake_meta: dict = {}
        try:
            if is_ingestion_paused():
                # Leave the file in the inbox (never claim it). The periodic
                # rescan re-attempts it once ingestion is resumed.
                logger.info("ingestion_paused_by_ops_monitor", file=str(path))
                return
            time.sleep(0.5)
            if not path.exists():
                logger.warning("file_gone_before_processing", file=str(path))
                return
            if _is_already_processed(path):
                logger.info("file_skipped_already_processed", file=str(path))
                return
            claimed = claim_file(path, self.worker_id)
            matter_id, intake_meta = _intake_context(path)
            _notify_intake_reaction(intake_meta)
            _notify_intake_triage(claimed, intake_meta)
            logger.info(
                "file_claimed",
                file=str(claimed),
                matter_id=matter_id,
                intake_source=intake_meta.get("source"),
            )
            result = run_pipeline(
                claimed,
                matter_id,
                source=intake_meta.get("source"),
                intake_meta=intake_meta or None,
            )
            logger.info("pipeline_complete", doc_id=result.get("doc_id"), matter_id=matter_id)
        except Exception:
            logger.exception("pipeline_failed", file=str(path))
            _finalize_claimed_on_error(
                claimed, matter_id, "watcher pipeline exception", intake_meta=intake_meta or None
            )
        finally:
            _unmark_active(path.name)

    def _infer_matter_id(self, path: Path) -> str:
        # Delegate to the module-level implementation (shared with Watcher).
        return _infer_matter_id(path)


class Watcher:
    def __init__(self):
        self.worker_id = get_worker_id()
        self.observer = Observer()
        self._running = False
        self._lock: _WatcherLock | None = None
        self._gmail_poller = None

    def start(self):
        global _watcher_owned
        inbox = inbox_dir()
        ensure_dirs(inbox)
        logger.info("watcher_starting", inbox=str(inbox), worker_id=self.worker_id)

        with _watcher_owned_lock:
            if _watcher_owned:
                raise WatcherLockHeld("watcher already running in this process")
            lock = acquire_watcher_lock()
            if lock is None:
                raise WatcherLockHeld(
                    f"another process holds {WATCHER_LOCK_NAME} — "
                    "the API-embedded watcher or `python -m pipeline.watcher` is already draining the inbox"
                )
            self._lock = lock
            _watcher_owned = True

        try:
            self._reconcile_stale_claims()

            for f in list_inbox_files():
                logger.info("existing_inbox_file", file=str(f))
                threading.Thread(
                    target=self._process_existing, args=(f,), daemon=True
                ).start()

            handler = InboxHandler(self.worker_id)
            self.observer.schedule(handler, str(inbox), recursive=False)
            self.observer.start()
            self._running = True
            touch_watcher_heartbeat()  # immediate liveness beacon before the first rescan
            logger.info("watcher_running", inbox=str(inbox))

            # Periodic inbox rescan: catches files skipped while ingestion was
            # paused (the pause path leaves them in the inbox) and any file that
            # appeared between watchdog events. Cheap and idempotent — already-
            # processed files are skipped by `_is_already_processed`.
            threading.Thread(target=self._rescan_loop, daemon=True).start()

            # Gmail intake channel (HUB-037): when MAILROOM_GMAIL_ENABLED=1,
            # the mailbox poller runs INSIDE this watcher process so the
            # watcher.lock holder remains the single intake authority. The
            # poller only drops attachments into the inbox — the drain path
            # below (watchdog events + rescan) is unchanged.
            from .gmail_intake import start_embedded_poller

            self._gmail_poller = start_embedded_poller()
        except Exception:
            self.stop()
            raise

    def _reconcile_stale_claims(self) -> None:
        """Re-queue (or retire) processing/<worker_id>/ files orphaned by a
        crashed process (L-1/A-18). Runs once at startup before the inbox
        scan. Terminal manifests (archived/failed/review) retire the copy
        to failed/ so it cannot orphan in the inbox.
        """
        stale = list_stale_processing_files(stale_minutes=STALE_CLAIM_MINUTES)
        for f in stale:
            try:
                action, dest = reconcile_stale_processing_file(f)
                logger.warning(
                    "stale_claim_reconciled",
                    file=str(f),
                    action=action,
                    dest=str(dest),
                )
            except Exception:
                logger.exception("stale_claim_requeue_failed", file=str(f))

    def _rescan_loop(self):
        import time as _time

        poll = float(os.environ.get("WATCHER_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)))
        while self._running:
            _time.sleep(poll)
            # Liveness beacon for /health: proves the watcher is alive and
            # draining the inbox (uploads only move once this process runs).
            touch_watcher_heartbeat()
            if is_ingestion_paused():
                continue
            for f in list_inbox_files():
                if f.name in _active_files:
                    continue
                threading.Thread(
                    target=self._process_existing, args=(f,), daemon=True
                ).start()

    def stop(self):
        global _watcher_owned
        from .gmail_intake import stop_embedded_poller

        stop_embedded_poller(self._gmail_poller)
        self._gmail_poller = None
        if self._running:
            self.observer.stop()
            self.observer.join(timeout=5)
            self._running = False
            logger.info("watcher_stopped")
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        with _watcher_owned_lock:
            _watcher_owned = False

    def _process_existing(self, path: Path):
        if not _mark_active(path.name):
            logger.info("file_already_processing", file=str(path))
            return
        claimed = None
        matter_id = "DEFAULT"
        intake_meta: dict = {}
        try:
            if is_ingestion_paused():
                logger.info("ingestion_paused_by_ops_monitor", file=str(path))
                return
            if not path.exists():
                logger.warning("existing_file_gone", file=str(path))
                return
            if _is_already_processed(path):
                logger.info("file_skipped_already_processed", file=str(path))
                return
            claimed = claim_file(path, self.worker_id)
            matter_id, intake_meta = _intake_context(path)
            _notify_intake_reaction(intake_meta)
            _notify_intake_triage(claimed, intake_meta)
            logger.info(
                "existing_file_claimed",
                file=str(claimed),
                matter_id=matter_id,
                intake_source=intake_meta.get("source"),
            )
            run_pipeline(
                claimed,
                matter_id,
                source=intake_meta.get("source"),
                intake_meta=intake_meta or None,
            )
        except Exception:
            logger.exception("existing_file_failed", file=str(path))
            _finalize_claimed_on_error(
                claimed, matter_id, "watcher existing-file exception", intake_meta=intake_meta or None
            )
        finally:
            _unmark_active(path.name)


if __name__ == "__main__":
    from observability.tracing import ensure_process_tracing

    watcher = Watcher()
    _shutdown = threading.Event()

    def _signal_handler(signum, frame):
        logger.info("watcher_signal_received", signal=signum)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        watcher.start()
        ensure_process_tracing()
        while not _shutdown.is_set():
            time.sleep(1)
    except WatcherLockHeld as exc:
        logger.error("watcher_not_started", reason=str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        from observability.tracing import flush

        flush()
