import time
import threading
import structlog
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .env import load_env

load_env()

from .bins import (
    inbox_dir,
    ensure_dirs,
    list_inbox_files,
    get_worker_id,
    claim_file,
)
from graph.build_graph import build_graph, run_pipeline

logger = structlog.get_logger(__name__)


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
        try:
            time.sleep(0.5)
            if not path.exists():
                logger.warning("file_gone_before_processing", file=str(path))
                return
            claimed = claim_file(path, self.worker_id)
            matter_id = self._infer_matter_id(path)
            logger.info("file_claimed", file=str(claimed), matter_id=matter_id)
            result = run_pipeline(claimed, matter_id)
            logger.info("pipeline_complete", doc_id=result.get("doc_id"), matter_id=matter_id)
        except Exception:
            logger.exception("pipeline_failed", file=str(path))

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
        try:
            if not path.exists():
                logger.warning("existing_file_gone", file=str(path))
                return
            claimed = claim_file(path, self.worker_id)
            matter_id = self._infer_matter_id(path)
            logger.info("existing_file_claimed", file=str(claimed), matter_id=matter_id)
            run_pipeline(claimed, matter_id)
        except Exception:
            logger.exception("existing_file_failed", file=str(path))


if __name__ == "__main__":
    watcher = Watcher()
    try:
        watcher.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()
