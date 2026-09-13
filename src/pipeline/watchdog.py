"""External watcher watchdog (HUB-050): 🔴 down alerts + 🟠 reminders only.

The watcher cannot report its own death — this tiny companion process is the
outside pair of eyes. It polls the watcher heartbeat (fed by the watcher's
rescan loop, default every second) and emails the human's status inbox
(`MAILROOM_STATUS_EMAIL`, default axios337@gmail.com):

* heartbeat missing/stale AND the watcher pid is gone  → immediate 🔴 DOWN.
* stale but pid alive (hang or laptop sleep)           → 🔴 only after two
  consecutive stale checks (sleep/wake false-positive guard).
* while down: 🟠 reminder every ``MAILROOM_WATCHDOG_REMIND_MINUTES``.

Human anti-spam directive 2026-09-04: NO periodic healthy-status emails —
emergency scenarios and watcher start/relaunch only (the 🟢 relaunch
confirmation is sent by the watcher itself at boot).

Run: ``uv run python -m pipeline.watchdog`` (own process, e.g. log to
``data/watchdog.out``). Kill-switch: ``MAILROOM_WATCHDOG=0``.
"""
import os
import socket
import time

import structlog

from .bins import HEARTBEAT_FILE_NAME, get_base_dir, watcher_heartbeat_age
from .env import load_env
from .logging import setup_logging

load_env()
setup_logging()

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 20.0
DEFAULT_STALE_SECONDS = 30.0
DEFAULT_REMIND_MINUTES = 60.0
HUNG_STREAK = 2  # consecutive stale checks (pid alive) before alerting


def _env_float(name: str, default: float, floor: float = 1.0) -> float:
    try:
        return max(floor, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def read_heartbeat() -> dict | None:
    """Raw heartbeat payload (``ts`` + enriched pid/host/started_at/sha)."""
    import json

    try:
        data = json.loads((get_base_dir() / HEARTBEAT_FILE_NAME).read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (TypeError, ValueError):
        return None
    return True


def _uptime_line(started_at: str | None, now: float) -> str:
    if not started_at:
        return "unknown"
    from datetime import datetime, timezone

    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
        seconds = max(0, now - started.timestamp())
        hours, rem = divmod(int(seconds), 3600)
        minutes = rem // 60
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {rem % 60:02d}s"
    except Exception:
        return "unknown"


def _down_rows(hb: dict | None, pid_alive: bool | None, age: float | None) -> list[tuple[str, str]]:
    pid = hb.get("pid") if hb else None
    last_seen = f"{age:.0f}s ago" if age is not None else "no heartbeat file — watcher never beat"
    return [
        ("Last heartbeat", last_seen),
        ("Watcher PID", f"{pid if pid is not None else 'unknown'} ({'process gone' if pid_alive is False else 'process still listed'})"),
        ("Host", str((hb or {}).get("host") or socket.gethostname())),
        ("Started at", str((hb or {}).get("started_at") or "unknown")),
        ("Code", f"git {(hb or {}).get('sha')}" if (hb or {}).get("sha") else "unknown"),
        ("Heartbeat file", str(get_base_dir() / HEARTBEAT_FILE_NAME)),
    ]


_DOWN_NOTE = (
    "Uploads are NOT being drained while the watcher is down. Relaunch with "
    "`uv run python -m pipeline.watcher` from packages/llm-mailroom "
    "(log: data/watcher.out). A 🟢 confirmation arrives on relaunch."
)


def evaluate(
    hb: dict | None,
    pid_alive: bool | None,
    state: dict,
    now: float,
    *,
    stale_s: float,
    remind_min: float,
) -> tuple[str, dict]:
    """Pure transition core. Returns (action, new_state) with action in
    {"none", "down", "remind"}.

    Ladder: pid gone (or heartbeat missing) → alert on the first stale
    check; pid alive but stale → alert after ``HUNG_STREAK`` consecutive
    stale checks (laptop sleep / brief hang false-positive guard). While
    down, re-alert on the reminder cadence. Recovery resets quietly.
    """
    age = watcher_heartbeat_age()
    fresh = age is not None and age <= stale_s
    state = dict(state)
    if fresh:
        # Quiet recovery — the 🟢 relaunch confirmation comes from the
        # watcher's own startup notice, so the watchdog stays silent here.
        state.update({"down_alerted": False, "stale_streak": 0, "last_alert": None})
        return "none", state

    state["stale_streak"] = int(state.get("stale_streak", 0)) + 1
    definitely_dead = pid_alive is False or hb is None
    confident = definitely_dead or state["stale_streak"] >= HUNG_STREAK
    if not confident:
        return "none", state
    if not state.get("down_alerted"):
        state.update({"down_alerted": True, "last_alert": now})
        return "down", state
    remind_after = remind_min * 60.0
    last_alert = state.get("last_alert") or 0.0
    if remind_min > 0 and now - last_alert >= remind_after:
        state["last_alert"] = now
        return "remind", state
    return "none", state


def main() -> None:
    if str(os.environ.get("MAILROOM_WATCHDOG", "1")).strip().lower() in ("0", "false", "no", "off"):
        logger.info("watchdog_disabled")
        return
    poll = _env_float("MAILROOM_WATCHDOG_POLL_SECONDS", DEFAULT_POLL_SECONDS)
    stale_s = _env_float("MAILROOM_WATCHDOG_STALE_SECONDS", DEFAULT_STALE_SECONDS)
    remind_min = _env_float("MAILROOM_WATCHDOG_REMIND_MINUTES", DEFAULT_REMIND_MINUTES, floor=0.0)
    from .status_notify import send_status_email

    logger.info(
        "watchdog_running",
        poll_seconds=poll,
        stale_seconds=stale_s,
        remind_minutes=remind_min,
    )
    state: dict = {"down_alerted": False, "stale_streak": 0, "last_alert": None}
    while True:
        try:
            hb = read_heartbeat()
            pid_alive = _pid_alive(hb.get("pid") if hb else None)
            action, state = evaluate(
                hb, pid_alive, state, time.time(), stale_s=stale_s, remind_min=remind_min
            )
            if action in ("down", "remind"):
                age = watcher_heartbeat_age()
                kind = "down" if action == "down" else "still_down"
                title = (
                    "watcher is DOWN — heartbeat lost"
                    if action == "down"
                    else "watcher is STILL DOWN — no heartbeat"
                )
                sent = send_status_email(kind, title, _down_rows(hb, pid_alive, age), note=_DOWN_NOTE)
                if not sent:
                    # The human must not silently lose the alert because SMTP
                    # hiccuped: revert the latch so the NEXT poll re-fires the
                    # 🔴 `down` alert instead of waiting out the reminder
                    # cadence. Worst case (outage AND broken SMTP) is a
                    # log-only retry every poll — never inbox spam.
                    state["down_alerted"] = False
                    state["last_alert"] = None
                logger.warning(
                    "watchdog_alerted" if action == "down" else "watchdog_reminded",
                    sent=sent,
                    pid_alive=pid_alive,
                    heartbeat_age=age,
                )
        except Exception:
            logger.exception("watchdog_loop_error")
        time.sleep(poll)


if __name__ == "__main__":
    main()