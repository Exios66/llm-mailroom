"""Free-model quota circuit breaker.

OpenRouter's shared free pool rate-limits for minutes at a time. Without a
breaker every free call in that window burns its full retry ladder before
failing. ``FreeQuotaBreaker`` trips after ``free_quota.trip_after``
consecutive 429s (default 3) and short-circuits free calls for a cooldown:
``Retry-After`` when the server sends it, else ``free_quota.cooldown_s``
(default 300), doubling per consecutive trip and capped at 3600 seconds. One
success closes it and resets the trip count.

Paid models never touch the breaker (``llm/retry.py``).
"""

from __future__ import annotations

import threading
import time

DEFAULT_TRIP_AFTER = 3
DEFAULT_COOLDOWN_S = 300.0
MAX_COOLDOWN_S = 3600.0


class FreeQuotaExhausted(RuntimeError):
    """Raised instead of calling a free model while the breaker is open."""

    def __init__(self, open_until: float):
        self.open_until = open_until
        super().__init__(f"free-model quota breaker open until {open_until:.0f}")


def _quota_config() -> dict:
    try:
        from pipeline.config import load_config

        return load_config().get("free_quota", {}) or {}
    except Exception:
        return {}


class FreeQuotaBreaker:
    def __init__(self, trip_after: int | None = None, cooldown_s: float | None = None):
        cfg = _quota_config()
        self.trip_after = max(1, int(trip_after if trip_after is not None else cfg.get("trip_after", DEFAULT_TRIP_AFTER)))
        self.cooldown_s = float(cooldown_s if cooldown_s is not None else cfg.get("cooldown_s", DEFAULT_COOLDOWN_S))
        self.open_until = 0.0
        self._consecutive = 0
        self._trips = 0
        self._lock = threading.Lock()

    def note_rate_limit(self, retry_after_s: float | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._consecutive += 1
            if self._consecutive < self.trip_after:
                return
            if retry_after_s is not None and retry_after_s > 0:
                cooldown = float(retry_after_s)
            else:
                cooldown = self.cooldown_s * (2 ** self._trips)
            self._trips += 1
            self._consecutive = 0
            self.open_until = max(self.open_until, now + min(MAX_COOLDOWN_S, cooldown))

    def note_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._trips = 0
            self.open_until = 0.0

    def is_open(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return now < self.open_until


_BREAKER: FreeQuotaBreaker | None = None
_BREAKER_LOCK = threading.Lock()


def get_breaker() -> FreeQuotaBreaker:
    global _BREAKER
    with _BREAKER_LOCK:
        if _BREAKER is None:
            _BREAKER = FreeQuotaBreaker()
        return _BREAKER


def reset_breaker() -> None:
    """Drop the singleton (tests; config reload)."""
    global _BREAKER
    with _BREAKER_LOCK:
        _BREAKER = None
