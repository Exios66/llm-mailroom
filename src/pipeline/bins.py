import os
import json
import hashlib
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path, PureWindowsPath
from typing import Optional

import structlog

from .config import load_config

logger = structlog.get_logger(__name__)


_config = None


def _get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_base_dir() -> Path:
    return Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()


# Claims made by THIS process, keyed by the pre-claim source path. An abort
# between ``claim_file`` and the state update carries only the stale source
# path; the registry maps it back to the exact file this run claimed, so
# recovery never depends on a same-named claim of another worker (whose
# mtime heartbeats keep refreshing). Bounded: entries are a few bytes each.
_CLAIMS_MAX = 4096
_claims: "OrderedDict[str, Path]" = OrderedDict()
_claims_lock = threading.Lock()


def _claim_key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return str(Path(path).absolute())


def _remember_claim(source: Path, claimed: Path) -> None:
    key = _claim_key(source)
    with _claims_lock:
        _claims.pop(key, None)
        _claims[key] = claimed
        while len(_claims) > _CLAIMS_MAX:
            _claims.popitem(last=False)


def find_claimed_file(original_filename: str, source_path: str | Path | None = None) -> Path | None:
    """The claimed copy of a file under processing/<worker>/.

    The abort path may hold a STALE pre-claim path (intake claims the file
    into a worker dir before the run completes; a crash in between leaves
    the reported path missing — PR #202 review).

    When ``source_path`` (the pre-claim path the run was started with) was
    claimed by this process, the recorded claim is authoritative: it is
    returned if it still exists, else ``None`` — another worker's same-named
    claim is never substituted. Only when no claim is recorded does the
    lookup fall back to scanning by name, where several same-named claims
    resolve to the newest (mtime)."""
    if source_path:
        with _claims_lock:
            recorded = _claims.get(_claim_key(source_path))
        if recorded is not None:
            try:
                return recorded if recorded.is_file() else None
            except OSError:
                return None
    return _claimed_file_scan(original_filename, processing_dir())


def _claimed_file_scan(original_filename: str, proc_root: Path) -> Path | None:
    if not original_filename:
        return None
    if not proc_root.exists():
        return None
    best: Path | None = None
    best_mtime = -1.0
    for worker_dir in proc_root.iterdir():
        if not worker_dir.is_dir():
            continue
        candidate = worker_dir / _safe_bin_component(original_filename, "original_filename")
        try:
            # A concurrent mover can take the claim between the check and the
            # stat; a vanished candidate is simply not a candidate.
            if not candidate.is_file():
                continue
            mtime = candidate.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    return best


def list_stale_processing_files(stale_minutes: int = 60) -> list[Path]:
    """Files stranded in processing/<worker_id>/ by a crashed process (L-1/A-18).

    A file claimed by a worker that died mid-run stays in its worker dir
    forever (no SIGTERM handler, no reclaim path). Anything whose claim is
    older than ``stale_minutes`` is presumed orphaned — return it for
    re-queueing or finalizing as failed. Age is the file's mtime, which
    ``claim_file`` / ``requeue_from_review`` reset to the claim time; callers
    that run documents in-process must still skip the names they are running.
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

    Idempotent: if the inbox already has the same bytes, drop the processing
    copy instead of double-queuing. A different file at the same name gets a
    ``--stale`` suffix so the original inbox document is not overwritten.
    """
    inbox = inbox_dir()
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("inbox_mkdir_failed", path=str(inbox))
        raise
    dest = inbox / file_path.name
    if dest.exists():
        if _same_file_bytes(file_path, dest):
            file_path.unlink(missing_ok=True)
            logger.info("stale_requeue_idempotent", dest=str(dest))
            return dest
        stem, suffix = file_path.stem, file_path.suffix
        dest = inbox / f"{stem}--stale{suffix}"
        counter = 2
        while dest.exists():
            dest = inbox / f"{stem}--stale{counter}{suffix}"
            counter += 1
        logger.warning(
            "stale_requeue_collision",
            source=str(file_path),
            dest=str(dest),
        )
    shutil.move(str(file_path), str(dest))
    return dest


def _same_file_bytes(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return hashlib.sha256(left.read_bytes()).digest() == hashlib.sha256(right.read_bytes()).digest()
    except OSError:
        logger.exception("stale_requeue_compare_failed", left=str(left), right=str(right))
        return False


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


TERMINAL_MANIFEST_STAGES = ("archived", "failed", "review")


def terminal_manifest_for(filename: str) -> bool:
    """True when a terminal-stage manifest already exists for this filename.

    A stranded processing copy of an already-archived/failed/reviewed
    document must be retired, not re-queued — otherwise it sits in the
    inbox forever (``_is_already_processed`` skips it).
    """
    mdir = manifests_dir()
    if not mdir.exists():
        return False
    for mf in mdir.glob("*.json"):
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        if (
            data.get("original_filename") == filename
            and data.get("stage") in TERMINAL_MANIFEST_STAGES
        ):
            return True
    return False


def reconcile_stale_processing_file(file_path: Path) -> tuple[str, Path]:
    """Retire or re-queue one stale processing claim.

    Returns ``(action, dest)`` where action is ``"failed"`` when a
    terminal manifest already exists, otherwise ``"requeue"``.
    """
    if terminal_manifest_for(file_path.name):
        dest = mark_processing_dead(file_path.parent.name, file_path.name)
        return "failed", dest
    dest = requeue_stale_processing(file_path)
    return "requeue", dest


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


def _safe_bin_component(value: str, label: str, *, allow_empty: bool = False) -> str:
    """Accept an identifier, never a relative/absolute filesystem path."""
    if value == "" and allow_empty:
        return value
    if (not isinstance(value, str) or not value.strip() or value in (".", "..")
            or any(char in value for char in ("/", "\\", "\0"))
            or PureWindowsPath(value).drive):
        raise ValueError(f"Invalid {label}: expected a single path-free identifier")
    return value


def validate_matter_id(value: str) -> str:
    """Validate upload metadata before accepting any document bytes."""
    return _safe_bin_component(value, "matter_id")


def archive_dir(matter_id: str = "", doc_type: str = "") -> Path:
    cfg = _get_config()
    base = _resolve(cfg["pipeline"]["bins"]["archive"])
    _safe_bin_component(matter_id, "matter_id", allow_empty=True)
    _safe_bin_component(doc_type, "doc_type", allow_empty=True)
    dest = base / matter_id / doc_type
    if not dest.resolve().is_relative_to(base.resolve()):
        raise ValueError("Invalid matter_id/doc_type: archive path escapes archive root")
    return dest


def manifests_dir() -> Path:
    cfg = _get_config()
    return _resolve(cfg["pipeline"]["bins"]["manifests"])


def ensure_dirs(*dirs: Path):
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("bin_mkdir_failed", path=str(d))
            raise


def claim_file(file_path: Path, worker_id: str) -> Path:
    processing = processing_dir(worker_id)
    processing.mkdir(parents=True, exist_ok=True)
    dest = processing / file_path.name
    # Reused workers must never replace an abandoned document's bytes.
    _move_review_file_no_clobber(file_path, dest)
    _stamp_claim_time(dest)
    _remember_claim(file_path, dest)
    return dest


def requeue_from_review(file_path: Path, worker_id: str, *, original_filename: str | None = None) -> Path:
    """Move a review-bin file back into a worker's processing dir (resume flow).

    Used by the review-resume path: a human-approved document leaves the review
    bin and is re-extracted from the file, then archived. Mirrors claim_file
    (processing/<worker_id>/<name>) so all file movement stays in bins.py.
    """
    processing = processing_dir(worker_id)
    processing.mkdir(parents=True, exist_ok=True)
    name = _safe_bin_component(original_filename or file_path.name, "original_filename")
    dest = processing / name
    _move_review_file_no_clobber(file_path, dest)
    _stamp_claim_time(dest)
    _remember_claim(file_path, dest)
    return dest


def _review_name(manifest) -> str:
    """Stable per-document bin identity while preserving the original label."""
    name = _safe_bin_component(manifest.original_filename, "original_filename")
    doc_id = _safe_bin_component(manifest.doc_id, "doc_id")
    path = Path(name)
    return f"{path.stem}--{doc_id}{path.suffix}"


def review_file_path(manifest) -> Path:
    """Resolve the owned review file, rejecting ambiguous legacy basenames."""
    return _owned_bin_file_path(manifest, review_dir())


def failed_file_path(manifest) -> Path:
    """Resolve the owned failed file, rejecting ambiguous legacy basenames.

    A path the manifest recorded (``file_location``) wins when it is a file
    in the failed bin: with both preferred names occupied, ``move_to_failed``
    stores the new bytes under a numbered name that no document-ID rule can
    derive, so the recorded path is the only exact locator for them."""
    base = failed_dir()
    recorded = _recorded_file_in(manifest, base)
    if recorded is not None:
        return recorded
    return _owned_bin_file_path(manifest, base)


def _recorded_file_in(manifest, base: Path) -> Path | None:
    location = getattr(manifest, "file_location", None)
    if not location:
        return None
    path = Path(location)
    # Only a name this document's own move could have produced is trusted:
    # ``<name>``, ``<stem>--<doc_id><suffix>`` or ``<stem>--<n><suffix>``. A
    # stale or edited manifest cannot point at another document's bytes.
    original = Path(str(getattr(manifest, "original_filename", "") or ""))
    if not original.name:
        return None
    name = path.name
    if name != original.name and not (
        name.startswith(f"{original.stem}--") and name.endswith(original.suffix)
    ):
        return None
    try:
        if path.parent.resolve() == base.resolve() and path.is_file():
            return path
    except (OSError, RuntimeError):
        return None
    return None


def archived_file_path(manifest) -> Path:
    """Resolve an archive path without treating mutable metadata as ownership."""
    return _owned_bin_file_path(manifest, archive_dir(manifest.matter_id, manifest.doc_type))


def processing_file_path(manifest) -> Path | None:
    """A bare processing filename has no worker identity in its manifest.

    Source lookup is best-effort: a transient unreadable processing root must
    not turn an API download/requeue into a 500 while another bin still holds
    the document. Only the directory listing is guarded; document identity
    validation still raises for unsafe manifest fields.
    """
    root = processing_dir()
    name = _review_name(manifest)
    try:
        if not root.is_dir():
            return None
        workers = [path for path in root.iterdir() if path.is_dir()]
        for worker in workers:
            owned = worker / name
            if owned.is_file():
                return owned
        legacy = [worker for worker in workers if (worker / manifest.original_filename).is_file()]
        if len(legacy) != 1:
            return None
        candidate = _owned_bin_file_path(manifest, legacy[0])
        return candidate if candidate.is_file() else None
    except OSError:
        logger.warning("processing_listing_failed", doc_id=manifest.doc_id, exc_info=True)
        return None


def _owned_bin_file_path(manifest, base: Path) -> Path:
    """Use document identity first; legacy ownership must be unambiguous."""
    owned = base / _review_name(manifest)
    if owned.is_file():
        return owned
    legacy = base / manifest.original_filename
    if not legacy.is_file():
        return owned
    # Older releases used bare names. Never assign an ambiguous legacy file
    # to another document merely because its basename happens to match.
    # A sibling's owned copy does not establish who owns a leftover bare name.
    # Stage/classification can change before a move, or without moving bytes;
    # current manifest metadata cannot rule out a sibling's stale legacy copy.
    for path in manifests_dir().glob("*.json"):
        try:
            other = load_manifest(path.stem)
            if other is not None:
                _review_name(other)  # Validate sibling identifiers before fallback.
            if (other is not None and other.doc_id != manifest.doc_id
                    and other.original_filename == manifest.original_filename):
                return owned
        except (OSError, ValueError):
            logger.warning("manifest_unreadable_legacy_ownership", path=str(path))
            # Unknown ownership cannot authorize exposing the bare-name file.
            # The document-ID path remains usable without scanning manifests.
            return owned
    return legacy


def touch_claim(file_path: str | Path | None) -> bool:
    """Refresh the claim time of a file in ``processing/<worker_id>/``.

    The stale-claim sweep treats a claim older than its cutoff as orphaned, and
    it may run in another process (a dedicated watcher beside the API), where no
    in-process "running" set protects the file. A run that is still making
    progress calls this at each node boundary, so only a claim that has stopped
    advancing can look stale. Files elsewhere (review, archive) are left alone.
    """
    if not file_path:
        return False
    path = Path(file_path)
    try:
        if path.parent.parent != processing_dir() or not path.is_file():
            return False
        os.utime(path, None)
        return True
    except OSError:
        logger.warning("claim_touch_failed", path=str(path))
        return False


def _stamp_claim_time(dest: Path) -> None:
    """Make the claimed file's mtime mean "claimed at", not "uploaded at".

    ``_move_review_file_no_clobber`` hard-links, so the claim keeps the
    upload's mtime. A document that waited in a backlog for longer than the
    stale cutoff would then look orphaned the moment it was claimed, and the
    periodic reconcile would move it back to the inbox mid-run.
    """
    try:
        os.utime(dest, None)
    except OSError:
        logger.warning("claim_time_stamp_failed", path=str(dest))


def _move_review_file_no_clobber(src: Path, dest: Path) -> None:
    """Publish complete bytes without ever replacing ``dest``, then retire ``src``.

    The fast path is an atomic hard link. When the link fails for any reason
    other than the name being taken (cross-device source, or a filesystem
    without hard links: EXDEV, EPERM, ENOTSUP, ...), the bytes are staged in
    a private destination-side temp file, fsynced, and published by
    reserving the name with ``O_CREAT|O_EXCL`` and renaming the staged file
    over our own reservation. An occupied destination always raises
    ``FileExistsError`` and is left untouched.
    """
    try:
        os.link(src, dest)
    except FileExistsError:
        raise
    except OSError:
        fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copyfile(src, temporary)
            try:
                shutil.copystat(src, temporary)
            except OSError:
                pass  # metadata is best effort on link-less filesystems
            # r+b: Windows refuses fsync on a read-only descriptor (EBADF).
            with open(temporary, "r+b") as staged:
                os.fsync(staged.fileno())
            _publish_staged_no_clobber(temporary, dest)
        finally:
            temporary.unlink(missing_ok=True)
    src.unlink()


def _publish_staged_no_clobber(staged: Path, dest: Path) -> None:
    try:
        os.link(staged, dest)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    # No hard links here: reserve the name atomically, then rename the staged
    # file over our own reservation (same directory, so the rename is atomic).
    # Until the rename lands, a reader can see the reservation as an empty
    # file; it is never another document's bytes, and it is never overwritten
    # by anyone but this call.
    reserved = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(reserved)
    try:
        os.replace(staged, dest)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _no_clobber_move_to_dir(file_path: Path, dest_dir: Path, doc_id: str = "") -> Path:
    """Resolve an unoccupied destination name, then publish via the atomic
    link+unlink move.

    ``os.link`` raising FileExistsError makes every step race-free: two
    concurrent movers (or a re-failed same-name / same-doc_id file) can
    never overwrite preserved bytes (PR #202 review on #194) — the old
    code checked ``exists()`` *before* moving, which is not atomic, and the
    doc-tagged fallback itself could overwrite."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _candidates():
        stem, suffix = file_path.stem, file_path.suffix
        yield dest_dir / file_path.name
        if doc_id:
            yield dest_dir / f"{stem}--{doc_id}{suffix}"
        counter = 1
        while True:
            yield dest_dir / f"{stem}--{counter}{suffix}"
            counter += 1

    for dest in _candidates():
        try:
            _move_review_file_no_clobber(file_path, dest)
            return dest
        except FileExistsError:
            continue
    raise FileExistsError(f"no free destination for {file_path.name} in {dest_dir}")  # unreachable


def park_for_review(file_path: Path, manifest) -> tuple[Path, bool]:
    """Move into the document-ID review path, replaying only identical bytes.

    LangGraph ``interrupt()`` restarts the node from the beginning on resume,
    so this must be an upsert: a second call with a missing processing-path
    source still succeeds when the review dest already exists. A distinct
    existing source with different bytes raises FileExistsError. If both
    source and owned destination are absent, an unambiguous legacy basename
    may be returned; unverifiable ownership fails closed.

    Returns ``(dest, newly_parked)``.
    """
    dest_dir = review_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = Path(file_path)
    dest = dest_dir / _review_name(manifest)
    dest_exists = dest.is_file()
    src_exists = src.exists()
    newly = False
    if dest_exists:
        if (src_exists and src.resolve() != dest.resolve()
                and not _same_file_bytes(src, dest)):
            raise FileExistsError(f"Different bytes already parked for document {manifest.doc_id}")
        newly = False
    elif src_exists:
        try:
            _move_review_file_no_clobber(src, dest)
            newly = True
        except FileExistsError:
            if not _same_file_bytes(src, dest):
                raise FileExistsError(
                    f"Different bytes already parked for document {manifest.doc_id}"
                ) from None
    else:
        legacy = review_file_path(manifest)
        if legacy.is_file():
            dest = legacy
        else:
            raise FileNotFoundError(
                f"Cannot park for review: source {src} and dest {dest} are both missing"
            )
    _save_manifest(manifest)
    return dest, newly


def move_to_review(file_path: Path, manifest) -> Path:
    dest, _ = park_for_review(file_path, manifest)
    return dest


def move_to_failed(file_path: Path, doc_id: str = "") -> Path:
    """Move a file to the failed bin with race-free collision resolution
    (PR #202 review on #194 — an occupied target is never overwritten)."""
    return _no_clobber_move_to_dir(file_path, failed_dir(), doc_id)


def move_to_archive(file_path: Path, matter_id: str, doc_type: str, doc_id: str = "") -> Path:
    """Move a file to the archive with race-free collision resolution
    (audit A-20; re-processing can never destroy a previously archived
    legal document, and the doc-tagged fallback no longer clobbers)."""
    return _no_clobber_move_to_dir(file_path, archive_dir(matter_id, doc_type), doc_id)


def _save_manifest(manifest) -> Path:
    mdir = manifests_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    path = mdir / f"{manifest.doc_id}.json"
    # Atomic publish: write a sibling temp file, then os.replace. A reader
    # (queue listing, a concurrent resolve) never sees a half-written manifest,
    # and the temp name never matches the ``*.json`` manifest glob.
    tmp = mdir / f".{manifest.doc_id}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(manifest.model_dump_json(indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def save_manifest(manifest) -> Path:
    return _save_manifest(manifest)


def load_manifest(doc_id: str):
    """Read an object-root manifest, rejecting other decoded JSON shapes."""
    from schemas.manifest import DocumentManifest

    path = manifests_dir() / f"{doc_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Document manifest root must be a JSON object")
    return DocumentManifest(**data)


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
    extensions = accepted_extensions()
    return sorted(
        p for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )


def count_inbox_pending() -> int:
    """Processable documents waiting in the inbox (excludes `.meta` sidecars).

    The-Mailroom hopper/`GET /api/pipeline` reads this via `/health`
    `checks.inbox_pending`. Counting every file would double-count uploads
    (document + sidecar).
    """
    return len(list_inbox_files())


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


def _heartbeat_file_path() -> Path:
    return get_base_dir() / HEARTBEAT_FILE_NAME


def touch_watcher_heartbeat(extra: dict | None = None) -> None:
    """Liveness beacon: the watcher touches this file every rescan cycle.

    `/health` reports how stale the heartbeat is so an operator can tell
    whether uploads are actually being drained (a missing/stale heartbeat
    means files will pile up in the inbox). ``extra`` merges operator-
    readable detail into the same JSON (pid/host/started_at — the HUB-050
    status channel reads them; the watchdog uses pid liveness as a fast
    down-signal); readers only depend on ``ts``. Written atomically
    (temp + replace) so the watchdog never reads a torn file. Best-effort,
    never raises.
    """
    import time as _time

    try:
        path = _heartbeat_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"ts": _time.time()}
        if extra:
            payload.update(extra)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
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


def watcher_stale_seconds() -> float:
    """Age at which a heartbeat is stale. Matches The-Mailroom `_WATCHER_STALE_S`."""
    try:
        return float(os.environ.get("WATCHER_STALE_SECONDS", "15"))
    except (TypeError, ValueError):
        return 15.0


def watcher_lamp(age: float | None = None) -> str:
    """Producer watcher lamp for The-Mailroom: ``live`` / ``stale`` / ``missing``.

    The visualizer prefers ``checks.watcher`` on ``GET /health`` and falls
    back to deriving the same three states from
    ``watcher_heartbeat_seconds_ago``.
    """
    if age is None:
        age = watcher_heartbeat_age()
    if age is None:
        return "missing"
    try:
        if float(age) > watcher_stale_seconds():
            return "stale"
    except (TypeError, ValueError):
        return "missing"
    return "live"
