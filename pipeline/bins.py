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


def move_to_archive(file_path: Path, matter_id: str, doc_type: str) -> Path:
    dest_dir = archive_dir(matter_id, doc_type)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
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


def is_ingestion_paused() -> bool:
    """Check if the ops monitor has paused ingestion."""
    pause_file = get_base_dir() / "ops_monitor_paused"
    return pause_file.exists()


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
