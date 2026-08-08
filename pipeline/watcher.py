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

from .bins import (
    inbox_dir,
    ensure_dirs,
    list_inbox_files,
    get_worker_id,
    claim_file,
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

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        cfg = inbox_dir()
        if not str(path).startswith(str(cfg)):
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

        for f in list_inbox_files():
            logger.info("existing_inbox_file", file=str(f))
            threading.Thread(
                target=self._process_existing, args=(f,), daemon=True
            ).start()

        handler = InboxHandler(self.worker_id)
        self.observer.schedule(handler, str(inbox), recursive=False)
        self.observer.start()
        self._running = True
        logger.info("watcher_running", inbox=str(inbox))

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
    watcher = Watcher()
    try:
        watcher.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()
