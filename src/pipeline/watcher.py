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
    requeue_stale_processing,
    accepted_extensions,
    read_inbox_meta,
    touch_watcher_heartbeat,
)
from graph.build_graph import build_graph, run_pipeline

logger = structlog.get_logger(__name__)

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

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        cfg = inbox_dir()
        if not str(path).startswith(str(cfg)):
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

    def _process(self, path: Path):
        if not _mark_active(path.name):
            logger.info("file_already_processing", file=str(path))
            return
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
            matter_id = self._infer_matter_id(path)
            logger.info("file_claimed", file=str(claimed), matter_id=matter_id)
            result = run_pipeline(claimed, matter_id)
            logger.info("pipeline_complete", doc_id=result.get("doc_id"), matter_id=matter_id)
        except Exception:
            logger.exception("pipeline_failed", file=str(path))
        finally:
            _unmark_active(path.name)

    def _infer_matter_id(self, path: Path) -> str:
        # Upload metadata wins: `/upload` writes a `<file>.meta` sidecar
        # carrying the submitted matter_id, so the document is filed under the
        # matter the caller chose instead of a filename heuristic. The sidecar
        # is read while the file is still in the inbox (before claim moves it).
        meta = read_inbox_meta(path)
        if meta and meta.get("matter_id"):
            return str(meta["matter_id"])
        parent_matter = path.parent.name
        if parent_matter and parent_matter != inbox_dir().name:
            return parent_matter
        stem = path.stem
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and parts[1].upper() == parts[1] and len(parts[1]) <= 10:
            return parts[1]
        return "DEFAULT"


class Watcher:
    def __init__(self):
        self.worker_id = get_worker_id()
        self.observer = Observer()
        self._running = False

    def start(self):
        inbox = inbox_dir()
        ensure_dirs(inbox)
        logger.info("watcher_starting", inbox=str(inbox), worker_id=self.worker_id)

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

    def _reconcile_stale_claims(self) -> None:
        """Re-queue processing/<worker_id>/ files orphaned by a crashed
        process (L-1/A-18). Runs once at startup before the inbox scan."""
        stale = list_stale_processing_files(stale_minutes=STALE_CLAIM_MINUTES)
        for f in stale:
            try:
                requeue_stale_processing(f)
                logger.warning("stale_claim_requeued", file=str(f))
            except Exception:
                logger.exception("stale_claim_requeue_failed", file=str(f))

    def _rescan_loop(self):
        import time as _time

        poll = float(os.environ.get("WATCHER_POLL_INTERVAL_SECONDS", "5"))
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
        if self._running:
            self.observer.stop()
            self.observer.join()
            self._running = False
            logger.info("watcher_stopped")

    def _process_existing(self, path: Path):
        if not _mark_active(path.name):
            logger.info("file_already_processing", file=str(path))
            return
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
            matter_id = self._infer_matter_id(path)
            logger.info("existing_file_claimed", file=str(claimed), matter_id=matter_id)
            run_pipeline(claimed, matter_id)
        except Exception:
            logger.exception("existing_file_failed", file=str(path))
        finally:
            _unmark_active(path.name)


if __name__ == "__main__":
    import atexit

    from observability.tracing import register_atexit_flush

    watcher = Watcher()
    _shutdown = threading.Event()

    def _signal_handler(signum, frame):
        logger.info("watcher_signal_received", signal=signum)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        watcher.start()
        register_atexit_flush()
        while not _shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        from observability.tracing import flush

        flush()
