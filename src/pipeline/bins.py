import os
import json
import shutil
import uuid
from pathlib import Path
from typing import Optional
from .config import load_config


_config = None


def _get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_base_dir() -> Path:
    return Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()


def list_stale_processing_files(stale_minutes: int = 60) -> list[Path]:
    """Files stranded in processing/<worker_id>/ by a crashed process (L-1/A-18).

    A file claimed by a worker that died mid-run stays in its worker dir
    forever (no SIGTERM handler, no reclaim path). Anything older than
    ``stale_minutes`` is presumed orphaned — return it for re-queueing or
    finalizing as failed.
    """
    import time as _time

    cutoff = _time.time() - stale_minutes * 60
    stale: list[Path] = []
    proc_root = processing_dir()
    if not proc_root.exists():
        return stale
    for worker_dir in proc_root.iterdir():
        if not worker_dir.is_dir():
            continue
        for f in worker_dir.iterdir():
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    stale.append(f)
            except OSError:
                continue
    return stale


def requeue_stale_processing(file_path: Path) -> Path:
    """Move a stale processing claim back to the inbox (L-1/A-18).

    The watcher's ``_is_already_processed`` will skip it if a terminal
    manifest already exists; otherwise it is re-claimed and re-run.
    """
    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / file_path.name
    shutil.move(str(file_path), str(dest))
    return dest


def mark_processing_dead(worker_id: str, file_name: str) -> Path:
    """Move a stale processing file to the failed bin (finalize path).

    Used by startup reconciliation when a manifest shows the document already
    reached a terminal stage: the stranded copy is retired to failed/ so it
    never resurfaces, and the terminal manifest stays authoritative.
    """
    src = processing_dir(worker_id) / file_name
    if not src.exists():
        raise FileNotFoundError(f"no such processing file: {src}")
    dest_dir = failed_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_name
    if dest.exists():
        dest = dest_dir / f"{Path(file_name).stem}--stale{Path(file_name).suffix}"
    shutil.move(str(src), str(dest))
    return dest


def _resolve(path_template: str) -> Path:
    cfg = _get_config()
    base = get_base_dir()
    return Path(str(path_template).format(base_dir=str(base)))


def inbox_dir() -> Path:
    cfg = _get_config()
    return _resolve(cfg["pipeline"]["bins"]["inbox"])


def processing_dir(worker_id: str | None = None) -> Path:
    cfg = _get_config()
    base = _resolve(cfg["pipeline"]["bins"]["processing"])
    if worker_id:
        return base / worker_id
    return base


def classified_dir(doc_type: str | None = None) -> Path:
    cfg = _get_config()
    base = _resolve(cfg["pipeline"]["bins"]["classified"])
    if doc_type:
        return base / doc_type
    return base


def review_dir() -> Path:
    cfg = _get_config()
    return _resolve(cfg["pipeline"]["bins"]["review"])


def failed_dir() -> Path:
    cfg = _get_config()
    return _resolve(cfg["pipeline"]["bins"]["failed"])


def archive_dir(matter_id: str = "", doc_type: str = "") -> Path:
    cfg = _get_config()
    base = _resolve(cfg["pipeline"]["bins"]["archive"])
    return base / matter_id / doc_type


def manifests_dir() -> Path:
    cfg = _get_config()
    return _resolve(cfg["pipeline"]["bins"]["manifests"])


def ensure_dirs(*dirs: Path):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def claim_file(file_path: Path, worker_id: str) -> Path:
    processing = processing_dir(worker_id)
    processing.mkdir(parents=True, exist_ok=True)
    dest = processing / file_path.name
    shutil.move(str(file_path), str(dest))
    return dest


def move_to_classified(file_path: Path, doc_type: str) -> Path:
    dest_dir = classified_dir(doc_type)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    shutil.move(str(file_path), str(dest))
    return dest


def requeue_from_review(file_path: Path, worker_id: str) -> Path:
    """Move a review-bin file back into a worker's processing dir (resume flow).

    Used by the review-resume path: a human-approved document leaves the review
    bin and is re-extracted from the file, then archived. Mirrors claim_file
    (processing/<worker_id>/<name>) so all file movement stays in bins.py.
    """
    processing = processing_dir(worker_id)
    processing.mkdir(parents=True, exist_ok=True)
    dest = processing / file_path.name
    shutil.move(str(file_path), str(dest))
    return dest


def move_to_review(file_path: Path, manifest) -> Path:
    dest_dir = review_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    shutil.move(str(file_path), str(dest))
    _save_manifest(manifest)
    return dest


def move_to_failed(file_path: Path) -> Path:
    dest_dir = failed_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    shutil.move(str(file_path), str(dest))
    return dest


def move_to_archive(file_path: Path, matter_id: str, doc_type: str, doc_id: str = "") -> Path:
    """Move a file to the archive with a collision-safe name (audit A-20).

    POSIX rename silently overwrites a same-named target — re-processing could
    destroy a previously archived legal document. When a collision exists, the
    incoming file gets a ``<stem>--<doc_id><suffix>`` name so nothing is ever
    overwritten."""
    dest_dir = archive_dir(matter_id, doc_type)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    if dest.exists():
        stem, suffix = file_path.stem, file_path.suffix
        if doc_id:
            dest = dest_dir / f"{stem}--{doc_id}{suffix}"
        else:
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{stem}--{counter}{suffix}"
                counter += 1
    shutil.move(str(file_path), str(dest))
    return dest


def _save_manifest(manifest) -> Path:
    mdir = manifests_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    path = mdir / f"{manifest.doc_id}.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path


def save_manifest(manifest) -> Path:
    return _save_manifest(manifest)


def load_manifest(doc_id: str):
    from schemas.manifest import DocumentManifest

    path = manifests_dir() / f"{doc_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return DocumentManifest(**data)


def load_taxonomy():
    return _get_config()


def get_worker_id() -> str:
    return str(uuid.uuid4())[:8]


PAUSE_FILE_NAME = "ops_monitor_paused"
_PAUSE_TTL_SECONDS = int(os.environ.get("MAILROOM_PAUSE_TTL_SECONDS", "3600"))


def _pause_file_path() -> Path:
    return get_base_dir() / PAUSE_FILE_NAME


def set_ingestion_paused(actor: str = "ops_monitor", reason: str = "", ttl_seconds: int | None = None) -> bool:
    """Write the pause flag as JSON {actor, reason, expires_at, set_at} (L-4).

    The pause now has a TTL: a transient incident (or an abuse-induced pause)
    auto-expires instead of halting ingestion indefinitely. Returns True when
    the pause was applied.
    """
    import time as _time

    ttl = ttl_seconds if ttl_seconds is not None else _PAUSE_TTL_SECONDS
    try:
        payload = {
            "actor": actor,
            "reason": reason or "",
            "set_at": _time.time(),
            "expires_at": _time.time() + ttl,
        }
        _pause_file_path().parent.mkdir(parents=True, exist_ok=True)
        _pause_file_path().write_text(json.dumps(payload))
        return True
    except Exception:
        return False


def get_pause_info() -> dict | None:
    """Return the pause metadata (actor/reason/expiry) or None when not paused."""
    import time as _time

    path = _pause_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"actor": "unknown", "reason": "unreadable pause file"}
    expires_at = data.get("expires_at", 0)
    if expires_at and _time.time() > expires_at:
        # L-4: TTL expired — clear the pause automatically.
        try:
            path.unlink()
        except Exception:
            pass
        return None
    return data


def clear_ingestion_paused() -> bool:
    try:
        path = _pause_file_path()
        if path.exists():
            path.unlink()
        return True
    except Exception:
        return False


def is_ingestion_paused() -> bool:
    """Check if the ops monitor has paused ingestion (auto-expiring TTL)."""
    return get_pause_info() is not None


def list_inbox_files() -> list[Path]:
    inbox = inbox_dir()
    if not inbox.exists():
        return []
    cfg = _get_config()
    extensions = cfg.get("file_extensions", None)
    if extensions is None:
        extensions = [".txt", ".pdf", ".docx", ".md"]
    return sorted(
        p for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )


def accepted_extensions() -> list[str]:
    """Accepted inbox file extensions from taxonomy.yaml (with defaults)."""
    cfg = _get_config()
    extensions = cfg.get("file_extensions", None)
    if extensions is None:
        extensions = [".txt", ".pdf", ".docx", ".md"]
    return [str(e).lower() for e in extensions]


def inbox_meta_path(file_path: Path) -> Path:
    """Path of the upload-metadata sidecar for an inbox file.

    The sidecar carries the metadata the API accepted on `/upload`
    (matter_id, upload_id, uploaded_at, ...) so the watcher can file the
    document under the submitted matter instead of inferring it from the
    filename. `<file>.meta` never matches `accepted_extensions()`, so it is
    never claimed or processed as a document itself.
    """
    return Path(str(file_path) + ".meta")


def write_inbox_meta(file_path: Path, **meta) -> Path | None:
    """Write the upload-metadata sidecar for an inbox file (best-effort).

    Only meaningful while the file is still in the inbox: once claimed the
    sidecar is left behind (harmless — it is not a processable extension) and
    the metadata has already been read. Returns the sidecar path on success.
    """
    try:
        path = inbox_meta_path(file_path)
        path.write_text(json.dumps(meta, default=str))
        return path
    except Exception:
        return None


def read_inbox_meta(file_path: Path) -> dict | None:
    """Read the upload-metadata sidecar for an inbox file (or None)."""
    try:
        path = inbox_meta_path(file_path)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


HEARTBEAT_FILE_NAME = "watcher_heartbeat"
_HEARTBEAT_FILE_PATH = None


def _heartbeat_file_path() -> Path:
    global _HEARTBEAT_FILE_PATH
    if _HEARTBEAT_FILE_PATH is None:
        _HEARTBEAT_FILE_PATH = get_base_dir() / HEARTBEAT_FILE_NAME
    return _HEARTBEAT_FILE_PATH


def touch_watcher_heartbeat() -> None:
    """Liveness beacon: the watcher touches this file every rescan cycle.

    `/health` reports how stale the heartbeat is so an operator can tell
    whether uploads are actually being drained (a missing/stale heartbeat
    means files will pile up in the inbox). Best-effort, never raises.
    """
    import time as _time

    try:
        path = _heartbeat_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": _time.time()}))
    except Exception:
        pass


def watcher_heartbeat_age() -> float | None:
    """Seconds since the watcher last beat, or None when no heartbeat exists.

    A missing heartbeat means the watcher has never run (uploads will sit in
    the inbox). The value is only meaningful when the watcher is alive enough
    to keep touching the file.
    """
    import time as _time

    try:
        path = _heartbeat_file_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return max(0.0, _time.time() - float(data.get("ts", 0)))
    except Exception:
        return None
