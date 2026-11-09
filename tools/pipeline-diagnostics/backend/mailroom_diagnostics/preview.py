"""Fresh-process workspace previews with atomic last-good publication.

The preview is always built by a separate interpreter so a long-lived server
never shares module state, caches or tracing configuration with the process
that compiles the graph. Writes are atomic; a failed rebuild keeps the last
valid preview and publishes a visible error instead of a half-written file.

This is a preview of edited files, not a running worker: ``git_sha`` and the
source hashes remain those of the build that produced the preview.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pydantic import ValidationError

from .models import DiagnosticsError, Snapshot
from .provenance import HASH_INPUTS

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_DEBOUNCE_SECONDS = 0.3


def default_base() -> Path:
    """Preview directory: explicit override, else the mailroom base, else data/."""
    explicit = os.environ.get("MAILROOM_DIAGNOSTICS_PREVIEW_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("MAILROOM_BASE_DIR", "").strip()
    return (Path(base).expanduser() if base else Path("data")) / "diagnostics"


def watched_paths(workspace: Path) -> list[Path]:
    return [
        workspace / relative
        for group in HASH_INPUTS.values()
        for relative in group
        if (workspace / relative).is_file()
    ]


def source_mtime(workspace: Path) -> int:
    """Newest mtime (ns) among the declared source/config seams, 0 when absent."""
    mtimes = [path.stat().st_mtime_ns for path in watched_paths(workspace)]
    return max(mtimes, default=0)


@dataclass(frozen=True)
class PreviewState:
    preview: dict | None
    last_good: dict | None
    error: dict | None
    stale: bool

    def to_dict(self) -> dict:
        return {
            "preview": self.preview,
            "last_good": self.last_good,
            "error": self.error,
            "stale": self.stale,
        }


def _paths(base: Path) -> tuple[Path, Path, Path]:
    return base / "preview.json", base / "last-good.json", base / "error.json"


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_error(base: Path, code: str, message: str, *, at: str | None = None) -> None:
    payload = {
        "code": code,
        "message": message,
        "at": at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write(_paths(base)[2], json.dumps(payload, indent=2).encode("utf-8"))


def _clear_error(base: Path) -> None:
    try:
        _paths(base)[2].unlink()
    except FileNotFoundError:
        pass


def build_in_fresh_process(
    workspace: Path, *, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> bytes:
    """Run the installed CLI in a new interpreter and return validated JSON."""
    environment = dict(os.environ, OBSERVABILITY_PROVIDER="none")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mailroom_diagnostics", "inspect", "--json"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticsError(
            "preview-failed", f"preview process could not start ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0:
        raise DiagnosticsError(
            "preview-failed", f"preview process exited {completed.returncode}"
        )
    raw = completed.stdout
    try:
        Snapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise DiagnosticsError(
            "invalid-preview", "preview process did not emit a valid snapshot"
        ) from exc
    return raw


def read_state(base: Path, workspace: Path) -> PreviewState:
    preview_path, last_good_path, error_path = _paths(base)
    preview = _read_json(preview_path)
    last_good = _read_json(last_good_path)
    error = _read_json(error_path)
    stale = False
    if preview is not None:
        try:
            stale = source_mtime(workspace) > preview_path.stat().st_mtime_ns
        except OSError:
            stale = True
    return PreviewState(preview=preview, last_good=last_good, error=error, stale=stale)


def refresh(
    workspace: Path,
    base: Path,
    *,
    builder: Callable[[Path], bytes] = build_in_fresh_process,
) -> PreviewState:
    """Build, validate and atomically publish a preview; keep last good on error."""
    preview_path, last_good_path, _ = _paths(base)
    try:
        raw = builder(workspace)
        Snapshot.model_validate_json(raw)
    except DiagnosticsError as exc:
        _write_error(base, exc.code, exc.message)
        return read_state(base, workspace)
    except Exception:
        _write_error(base, "preview-failed", "unable to build a workspace preview")
        return read_state(base, workspace)
    _atomic_write(preview_path, raw)
    _atomic_write(last_good_path, raw)
    _clear_error(base)
    return read_state(base, workspace)


def watch(
    workspace: Path,
    base: Path,
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    debounce: float = DEFAULT_DEBOUNCE_SECONDS,
    cycles: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    mtime: Callable[[Path], int] = source_mtime,
    refresh_fn: Callable[..., PreviewState] = refresh,
) -> int:
    """Rebuild after a source/config change settles; run until interrupted.

    ``cycles`` bounds the loop for tests. A change starts the debounce window;
    a further change restarts it, so a half-written save is never built.
    """
    current = mtime(workspace)
    pending: float | None = None
    completed = 0
    while cycles is None or completed < cycles:
        completed += 1
        observed = mtime(workspace)
        if observed != current:
            current = observed
            pending = clock()
        elif pending is not None and clock() - pending >= debounce:
            refresh_fn(workspace, base)
            pending = None
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return 0
    return 0
