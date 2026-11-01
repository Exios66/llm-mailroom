"""Opt-in release awareness; importing Dojo and scoring never start checks.

Only application startup or this module's explicit CLI may use the checker.
The cache contains release versions and local notice markers, never documents.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
from typing import Callable, Iterator

from packaging.version import InvalidVersion, Version

from . import __version__

API_HOST = "api.github.com"
API_PATH = "/repos/Exios66/llm-dojo-scoring/releases/latest"
RELEASE_URL = "https://github.com/Exios66/llm-dojo-scoring/releases/latest"
CHECK_INTERVAL = 86400
ERROR_BACKOFF = 3600
NOTICE_INTERVAL = 86400
HTTP_TIMEOUT = 2.0
CHECK_TIMEOUT = 3.0
MAX_RESPONSE_BYTES = 131072
MAX_CACHE_BYTES = 32768
MAX_NOTICES = 128
_OPT_OUT = {"0", "false", "no", "off"}
_TRANSPORT_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReleaseResponse:
    """A bounded HTTP response supplied by the fixed-host transport or a test."""

    status: int
    body: bytes
    retry_after: str | None = None


@dataclass(frozen=True)
class UpdateStatus:
    """Release knowledge with explicit freshness and safe, categorical errors."""

    status: str
    installed_version: str = __version__
    latest_version: str | None = None
    checked_at: float | None = None
    stale: bool = True
    source: str = "none"
    error: str | None = None
    cache_persisted: bool = False
    release_url: str = RELEASE_URL


@dataclass(frozen=True)
class StartupStatus:
    """Immediate cached state and an optional notice; refresh never blocks it."""

    update: UpdateStatus
    notice: str | None = None
    refresh_scheduled: bool = False


def _enabled() -> bool:
    """Honor the explicit environment opt-out even for forced checks."""
    return os.environ.get("LLM_DOJO_UPDATE_CHECK", "1").strip().lower() not in _OPT_OUT


def _version(value: object, *, stable: bool = False) -> Version:
    """Parse bounded ASCII PEP 440 versions without accepting display text."""
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
        r"[vV]?[0-9][0-9A-Za-z.!+_-]*", value, flags=re.ASCII
    ):
        raise ValueError("invalid_version")
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise ValueError("invalid_version") from exc
    if stable and (parsed.is_prerelease or parsed.is_devrelease or parsed.local):
        raise ValueError("unstable_release")
    return parsed


def _timestamp(value: object, now: float) -> bool:
    """Reject nonfinite, boolean, negative, and future cache timestamps."""
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now


def _default_cache_dir() -> Path:
    """Resolve one per-user directory lazily, shared by every consumer."""
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    if xdg and Path(xdg).is_absolute():
        base = Path(xdg)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "llm-dojo-scoring"


def _http_transport() -> ReleaseResponse:
    """GET one public endpoint, with no credentials, redirects, or proxy lookup.

    Socket operations have a two-second timeout. The calling daemon worker
    imposes a separate three-second wait budget, including DNS delays.
    """
    connection = http.client.HTTPSConnection(API_HOST, timeout=HTTP_TIMEOUT)
    try:
        connection.request("GET", API_PATH, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "llm-dojo-scoring-update-check",
            "X-GitHub-Api-Version": "2026-03-10",
        })
        response = connection.getresponse()
        # No need to read untrusted error pages, nor follow Location headers.
        body = response.read(MAX_RESPONSE_BYTES + 1) if response.status == 200 else b""
        return ReleaseResponse(response.status, body, response.getheader("Retry-After"))
    finally:
        connection.close()


def _fetch(transport: Callable[[], ReleaseResponse]) -> tuple[ReleaseResponse | None, str | None]:
    """Bound caller wait and allow at most one active HTTP worker per process.

    A stuck DNS/system call cannot be cancelled in Python. Its daemon thread
    retains the slot until it ends; later calls fail softly instead of spawning
    more workers. No thread is joined at shutdown.
    """
    if not _TRANSPORT_LOCK.acquire(blocking=False):
        return None, "check_busy"
    done = threading.Event()
    output: list[ReleaseResponse | str] = []

    def run() -> None:
        """Capture only safe errors and always release the transport slot."""
        try:
            output.append(transport())
        except (TimeoutError, OSError, http.client.HTTPException):
            output.append("network_error")
        except Exception:
            # A custom transport must not break application startup either.
            output.append("transport_error")
        finally:
            _TRANSPORT_LOCK.release()
            done.set()

    try:
        threading.Thread(target=run, name="dojo-release-http", daemon=True).start()
    except (RuntimeError, OSError):
        _TRANSPORT_LOCK.release()
        return None, "worker_unavailable"
    if not done.wait(CHECK_TIMEOUT):
        return None, "check_timeout"
    if not output or not isinstance(output[0], ReleaseResponse):
        return None, output[0] if output else "transport_error"
    return output[0], None


def _release(response: ReleaseResponse, now: float) -> str:
    """Accept only a published stable release; ignore all remote display URLs."""
    if not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    data = json.loads(response.body)
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        raise ValueError("invalid_release")
    published = data.get("published_at")
    if not isinstance(published, str) or len(published) > 40:
        raise ValueError("unpublished_release")
    date = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if date.tzinfo is None or date.timestamp() < 0 or date.timestamp() > now:
        raise ValueError("unpublished_release")
    return str(_version(data.get("tag_name"), stable=True))


def _clean(data: object, now: float) -> dict:
    """Validate the entire small cache; corrupt/future state cannot throttle checks."""
    if not isinstance(data, dict) or data.get("schema") != 1:
        return {}
    checked, attempted = data.get("checked_at"), data.get("attempted_at")
    if attempted is not None and not _timestamp(attempted, now):
        return {}
    if checked is not None and (not _timestamp(checked, now) or attempted is None or checked > attempted):
        return {}
    latest = data.get("latest")
    if latest is not None:
        try:
            latest = str(_version(latest, stable=True))
        except ValueError:
            return {}
        if checked is None:
            return {}
    elif checked is not None:
        return {}
    error = data.get("error")
    if error not in {None, "network_error", "transport_error", "check_timeout", "check_busy",
                     "worker_unavailable", "invalid_response", "http_error", "rate_limited"}:
        return {}
    retry = data.get("retry_at")
    if retry is not None and (attempted is None or not _timestamp(retry, attempted + CHECK_INTERVAL)
                              or retry < attempted):
        return {}
    notices = {}
    raw_notices = data.get("notices", {})
    if not isinstance(raw_notices, dict) or len(raw_notices) > MAX_NOTICES:
        return {}
    for key, at in raw_notices.items():
        consumer, separator, version = key.partition("@")
        if not separator or not _consumer(consumer) or not _timestamp(at, now):
            continue
        try:
            version = str(_version(version, stable=True))
        except ValueError:
            continue
        notices[f"{consumer}@{version}"] = at
    return {"schema": 1, "checked_at": checked, "attempted_at": attempted,
            "latest": latest, "error": error, "retry_at": retry, "notices": notices}


def _consumer(value: object) -> bool:
    """Allow a bounded application identifier, never arbitrary user display text."""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is not None


def _lock_file(fd: int) -> Callable[[], None]:
    """Acquire one immediate OS lock and return its matching unlock operation.

    Windows CRT byte locks begin at the current file offset and may extend
    beyond EOF, so an empty stable lock file needs no initialization write.
    LK_NBLCK is deliberately used instead of the CRT's retrying LK_LOCK.
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

        def unlock_windows() -> None:
            """Unlock the same byte range before closing the CRT descriptor."""
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

        return unlock_windows
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def unlock_posix() -> None:
            """Release the short advisory lock on the stable inode."""
            fcntl.flock(fd, fcntl.LOCK_UN)

        return unlock_posix
    raise OSError("unsupported file locking")


class UpdateChecker:
    """Shared release policy with injectable transport and clock for offline tests.

    Cache mutations use nonblocking POSIX flock or Windows CRT locking and atomic replacement. No
    lock is held during HTTP. Concurrent processes may duplicate HTTP requests;
    they cannot produce torn JSON or overwrite each other's notice markers.
    Without either supported OS lock, checks work but persistence is unavailable.
    """

    def __init__(self, cache_dir: Path | None = None, *,
                 transport: Callable[[], ReleaseResponse] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        """Store dependencies only; construction performs no I/O."""
        self._cache_dir = cache_dir
        self._transport = transport or _http_transport
        self._clock = clock

    @property
    def cache_dir(self) -> Path:
        """Resolve the cache location only when explicitly used."""
        return Path(self._cache_dir) if self._cache_dir is not None else _default_cache_dir()

    def _read(self, now: float) -> dict:
        """Read at most 32 KiB from a regular non-symlink cache file."""
        try:
            fd = os.open(self.cache_dir / "updates-v1.json", os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return {}
                payload = handle.read(MAX_CACHE_BYTES + 1)
            if len(payload) > MAX_CACHE_BYTES:
                return {}
            return _clean(json.loads(payload), now)
        except (OSError, ValueError, TypeError, RecursionError, RuntimeError, OverflowError):
            return {}

    @contextmanager
    def _lock(self) -> Iterator[bool]:
        """Try a short local write lock; unavailable caches never block callers."""
        fd = None
        try:
            self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.cache_dir / "updates-v1.lock", os.O_CREAT | os.O_RDWR
                         | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular lock")
            unlock = _lock_file(fd)
        except (OSError, ImportError, RuntimeError):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
            yield False
            return
        try:
            yield True
        finally:
            with suppress(OSError):
                unlock()
            with suppress(OSError):
                os.close(fd)

    def _write(self, data: dict) -> bool:
        """Atomically replace a private bounded cache while the caller holds its lock."""
        temporary = None
        try:
            payload = json.dumps(data, allow_nan=False).encode("utf-8")
            if len(payload) > MAX_CACHE_BYTES:
                return False
            fd, temporary = tempfile.mkstemp(prefix=".updates-", dir=self.cache_dir)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(temporary, self.cache_dir / "updates-v1.json")
            return True
        except (OSError, ValueError, RuntimeError):
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _status(self, data: dict, now: float, *, source: str = "cache",
                error: str | None = None, persisted: bool = False) -> UpdateStatus:
        """Compare release knowledge with the loaded code, never distribution metadata."""
        checked, latest = data.get("checked_at"), data.get("latest")
        stale = checked is None or now - checked >= CHECK_INTERVAL
        failure = error or data.get("error")
        status = "unavailable" if failure else "unknown"
        if latest and not failure:
            if _version(latest) > _version(__version__):
                status = "update_available"
            elif not stale:
                status = "up_to_date"
        return UpdateStatus(status, __version__, latest, checked, stale, source if data else "none",
                            failure, persisted)

    def _due(self, data: dict, now: float) -> bool:
        """Apply the daily success interval or capped error backoff."""
        if data.get("error"):
            return now >= (data.get("retry_at") or 0)
        checked = data.get("checked_at")
        return checked is None or now - checked >= CHECK_INTERVAL

    def cached(self) -> UpdateStatus:
        """Read cached knowledge without HTTP, cache creation, or notice writes."""
        if not _enabled():
            return UpdateStatus("disabled")
        now = self._clock()
        return self._status(self._read(now), now)

    def check(self, *, force: bool = False, offline: bool = False) -> UpdateStatus:
        """Explicitly check, returning unavailable on actual failures, even with old data.

        Force bypasses both intervals, never the environment opt-out. Offline
        reads only. Neither mode upgrades packages or edits dependency pins.
        """
        if force and offline:
            raise ValueError("force and offline are mutually exclusive")
        if not _enabled():
            return UpdateStatus("disabled")
        now = self._clock()
        data = self._read(now)
        if offline or (not force and not self._due(data, now)):
            return self._status(data, now)
        response, error = _fetch(self._transport)
        latest = None
        delay = ERROR_BACKOFF
        if response is not None:
            if response.status == 200:
                try:
                    latest = _release(response, now)
                except (ValueError, TypeError, RecursionError, OverflowError):
                    error = "invalid_response"
            else:
                error = "rate_limited" if response.status in (403, 429) else "http_error"
                retry = response.retry_after
                if isinstance(retry, str) and len(retry) <= 10 and retry.isascii() and retry.isdigit():
                    delay = min(CHECK_INTERVAL, max(ERROR_BACKOFF, int(retry)))
        if error == "check_busy":
            # A concurrent request is already doing useful work. Do not poison
            # its cache with an artificial backoff observation.
            return self._status(data, now, source="network", error=error)
        fields = {"attempted_at": now, "error": error,
                  "retry_at": now + delay if error else None}
        if latest is not None:
            fields.update(latest=latest, checked_at=now)
        observed = data
        data = {**data, **fields}
        persisted = False
        with self._lock() as locked:
            if locked:
                current = self._read(self._clock())
                # Merge under lock so delayed requests cannot erase notice claims
                # or newer observations from another process.
                attempted = current.get("attempted_at")
                tied = attempted == now
                lower_release = (latest is not None and current.get("latest") is not None
                                 and _version(latest) < _version(current["latest"]))
                # A concurrent failure must retain its backoff. An explicit
                # retry that already saw that observation can still recover.
                concurrent_failure = current.get("error") and any(
                    current.get(key) != observed.get(key)
                    for key in ("attempted_at", "checked_at", "latest", "error", "retry_at")
                )
                if (attempted is None or attempted < now
                        or (tied and not lower_release and not concurrent_failure)):
                    current.update(fields)
                    current.setdefault("schema", 1)
                    current.setdefault("notices", {})
                    persisted = self._write(current)
                data = current
        return self._status(data, now, source="network", error=error, persisted=persisted)

    def _notice(self, consumer: str, update: UpdateStatus, now: float) -> str | None:
        """Claim a daily per-consumer/release notice only if the marker is persisted."""
        if not update.latest_version or _version(update.latest_version) <= _version(__version__):
            return None
        with self._lock() as locked:
            if not locked:
                return None
            data = self._read(self._clock())
            notices = {key: at for key, at in data.get("notices", {}).items()
                       if now - at < NOTICE_INTERVAL}
            key = f"{consumer}@{update.latest_version}"
            if key in notices or len(notices) >= MAX_NOTICES:
                # Preserve active consumer/release pairs rather than evicting them.
                return None
            notices[key] = now
            data["notices"] = notices
            data["schema"] = 1
            if not self._write(data):
                return None
        prefix = "Cached " if update.stale or update.error else ""
        return (f"{prefix}Dojo update available: installed {__version__}, "
                f"latest {update.latest_version}. {RELEASE_URL}")

    def startup(self, consumer: str) -> StartupStatus:
        """Return cached status now and schedule one best-effort in-process refresh.

        New discoveries are visible on the next cached read/startup. A short
        CLI may exit before refresh completes; no exit handler waits for it.
        Invalid consumer IDs and unavailable caches cannot break application startup.
        """
        if not _enabled():
            return StartupStatus(UpdateStatus("disabled"))
        if not _consumer(consumer):
            return StartupStatus(UpdateStatus("unknown", error="invalid_consumer"))
        now = self._clock()
        data = self._read(now)
        update = self._status(data, now)
        notice = self._notice(consumer, update, now)
        if not self._due(data, now) or not _REFRESH_LOCK.acquire(blocking=False):
            return StartupStatus(update, notice)

        def refresh() -> None:
            """Warm the shared cache without leaking background failures to consumers."""
            try:
                self.check()
            except Exception:
                pass
            finally:
                _REFRESH_LOCK.release()

        try:
            threading.Thread(target=refresh, name="dojo-release-refresh", daemon=True).start()
        except (RuntimeError, OSError):
            _REFRESH_LOCK.release()
            return StartupStatus(update, notice)
        return StartupStatus(update, notice, True)


def main(argv: list[str] | None = None) -> int:
    """Print code identity or explicitly check; unavailable knowledge exits one."""
    parser = argparse.ArgumentParser(prog="dojo-version", description="Loaded Dojo version and optional release check")
    parser.add_argument("--check", action="store_true", help="Check the canonical published stable release")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--force", action="store_true", help="Bypass daily/error cache intervals")
    policy.add_argument("--offline", action="store_true", help="Read cache only; never make HTTP requests")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    args = parser.parse_args(argv)
    if (args.force or args.offline) and not args.check:
        parser.error("--force/--offline require --check")
    if not args.check:
        print(json.dumps({"installed_version": __version__}) if args.json else __version__)
        return 0
    result = UpdateChecker().check(force=args.force, offline=args.offline)
    if args.json:
        print(json.dumps(asdict(result), allow_nan=False))
    else:
        print(f"Dojo installed {result.installed_version}; {result.status}; "
              f"latest {result.latest_version or 'unknown'}; stale={str(result.stale).lower()}. "
              f"{RELEASE_URL}" + (f" ({result.error})" if result.error else ""))
    return 1 if result.status in {"unknown", "unavailable"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
