"""Operator SMS pager host: snapshot sources, scheduler, settings, CLI.

The pure decisions (rules, frequency state machine, SMS copy, JSON payload)
live in `pager_core.py`; this module collects a `RunSnapshot` from the
catalog, bins, heartbeat, passive provider health, Modal and OpenRouter,
and sends through `sms_notify`. Run as `python -m pipeline.pager <verb>`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

from pipeline.bins import get_base_dir
from pipeline.pager_core import PagerConfig, ProviderStatus, RunProgress, RunSnapshot

logger = structlog.get_logger(__name__)

# ── research run progress ────────────────────────────────────────────


def _runs_dir() -> Path:
    return get_base_dir() / "pager_runs"


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")  # CLI and pager never share
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def report_run_progress(run_id: str, *, label: str, total: int, done: int, metrics: dict | None = None,
                        finished: bool = False) -> None:
    """Record a research run's progress for the pager (atomic; never raises)."""
    try:
        path = _runs_dir() / (re.sub(r"[^A-Za-z0-9_.-]", "_", run_id) + ".json")
        now = time.time()
        try:
            started = json.loads(path.read_text())["started_at"]
        except Exception:
            started = now
        _atomic_write(path, {"run_id": run_id, "label": label, "total": int(total), "done": int(done),
                             "started_at": started, "updated_at": now, "finished": bool(finished),
                             "metrics": metrics or {}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("pager_run_progress_failed", run_id=run_id, error=str(exc)[:200])


def read_runs(max_age_s: float = 86400) -> list[RunProgress]:
    out = []
    for p in sorted(_runs_dir().glob("*.json")) if _runs_dir().exists() else []:
        try:
            d = json.loads(p.read_text())
            if time.time() - d["updated_at"] <= max_age_s:
                out.append(RunProgress(**{k: d[k] for k in RunProgress.__dataclass_fields__}))
        except Exception:
            continue
    return out


# ── providers ────────────────────────────────────────────────────────

_OR_CACHE: dict = {}


def openrouter_info(cfg: PagerConfig, client: httpx.Client | None = None) -> dict | None:
    """OpenRouter key status (`GET /key`), cached `providers.openrouter_cache_minutes`."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    if _OR_CACHE and time.time() - _OR_CACHE["at"] < cfg.providers.openrouter_cache_minutes * 60:
        return _OR_CACHE["info"]
    from langchain_agents.openrouter_utils import openrouter_base_url

    base = openrouter_base_url().rstrip("/")
    u = urlparse(base)
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
        info = {"error": "insecure base URL"}  # never send the API key in cleartext
        _OR_CACHE.update(at=time.time(), info=info)
        return info
    try:
        c = client or httpx.Client(timeout=5.0)
        r = c.get(f"{base}/key", headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
        if r.status_code != 200:
            info = {"error": f"HTTP {r.status_code}"}
        else:
            d = r.json().get("data") or {}
            info = {k: d.get(k) for k in ("limit_remaining", "usage", "is_free_tier")}
    except Exception as exc:  # noqa: BLE001
        info = {"error": type(exc).__name__}
    _OR_CACHE.update(at=time.time(), info=info)
    return info


def modal_warm(app: str = "mailroom-vllm", run=subprocess.run) -> dict[str, int] | None:
    """Warm containers per tier via the read-only `modal container list --json` (never wakes a GPU)."""
    if not shutil.which("modal"):
        return None
    try:
        r = run(["modal", "container", "list", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        warm: dict[str, int] = {}
        for c in json.loads(r.stdout or "[]"):
            fn = str(c.get("Function") or c.get("function") or "")
            if str(c.get("App Name") or c.get("app_name") or "") == app and fn.startswith("serve_"):
                warm[fn[6:]] = warm.get(fn[6:], 0) + 1
        return warm
    except Exception:
        return None


def _breaker_open() -> bool:
    try:
        from llm.quota import get_breaker

        return bool(get_breaker().is_open())
    except Exception:
        return False


def _modal_tiers() -> list[str]:
    try:
        from pipeline.config import load_config

        tiers = (load_config().get("gateway") or {}).get("tiers") or {}
        if isinstance(tiers, dict) and tiers:
            return [t for t in tiers if t != "api"]  # `api` is the gateway's OpenRouter tier, not Modal
    except Exception:
        pass
    return ["fast", "extract", "vision"]


def provider_statuses(cfg: PagerConfig, health: dict, or_info: dict | None, warm: dict | None) -> list[ProviderStatus]:
    """Pure: map passive health + Modal warm counts + OpenRouter /key onto provider states."""
    rd, warm = cfg.rules["provider_degraded"], warm or {}
    tiers = list(dict.fromkeys(_modal_tiers() + [k[6:] for k in health if k.startswith("modal:")]))
    out = []
    for t in tiers:
        h = health.get(f"modal:{t}") or {}
        calls, errs = h.get("calls", 0), h.get("errors", 0)
        if h.get("consecutive_errors", 0) >= 3:
            st, det = "down", f"HTTP {h.get('last_status')}" if h.get("last_status") else "no response"
        elif calls >= rd["min_calls"] and errs / calls >= rd["error_rate"]:
            st, det = "degraded", ""
        elif h.get("count_503", 0) >= rd["min_calls"] and not h.get("ok_calls"):
            st, det = "degraded", f"cold start: {h['count_503']}× 503, no success yet"
        elif h.get("count_503"):
            st, det = "up", f"cold start: {h['count_503']}× 503"
        elif not calls:
            st, det = ("warm", _n_containers(warm[t])) if warm.get(t) else ("idle", "")
        else:
            st, det = "up", ""
        out.append(ProviderStatus(f"modal:{t}", st, det, calls, errs))
    h = health.get("openrouter") or {}
    info = or_info or {}
    if "error" in info and info["error"] in ("HTTP 401", "HTTP 403"):
        st, det = "down", info["error"]
    elif h.get("consecutive_errors", 0) >= 3:
        st, det = "down", f"HTTP {h.get('last_status')}" if h.get("last_status") else "no response"
    elif _breaker_open():
        st, det = "degraded", "free pool rate-limited"
    elif or_info and "error" not in info:
        left = info.get("limit_remaining")
        st, det = "up", f"${left:.2f} left" if left is not None else "no limit"
    elif h.get("calls"):
        st, det = "up", ""
    else:
        return out  # no key and no traffic: OpenRouter is not in use here
    out.append(ProviderStatus("openrouter", st, det, h.get("calls", 0), h.get("errors", 0)))
    return out


def _n_containers(n: int) -> str:
    return f"{n} container{'' if n == 1 else 's'}"


# ── snapshot ─────────────────────────────────────────────────────────


def _source(name: str, fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one dead source never blanks the digest
        logger.warning("pager_snapshot_source_failed", source=name, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return default


def _run_async(coro):
    box: dict = {}

    def target():
        try:
            box["v"] = asyncio.run(asyncio.wait_for(coro, timeout=20))
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(25)
    if "e" in box:
        raise box["e"]
    if "v" not in box:
        raise TimeoutError("catalog query timed out")
    return box["v"]


def _local_midnight(now: float, tz: str) -> datetime:
    from zoneinfo import ZoneInfo

    d = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now).astimezone()
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                           cwd=Path(__file__).resolve().parent)
        return r.stdout.strip() or None
    except Exception:
        return None


def collect_snapshot(cfg: PagerConfig, *, window_minutes: float, now: float | None = None) -> RunSnapshot:
    """Gather everything the pager reports. Each source fails soft on its own."""
    from pipeline import bins
    from storage import catalog

    now = time.time() if now is None else now
    since = datetime.fromtimestamp(now - window_minutes * 60, timezone.utc)
    hb = _source("heartbeat", lambda: __import__("pipeline.watchdog", fromlist=["x"]).read_heartbeat(), None)
    age = _source("heartbeat_age", bins.watcher_heartbeat_age, None)
    alive = None
    if age is not None:
        pid_ok = _source("pid", lambda: __import__("pipeline.watchdog", fromlist=["x"])._pid_alive(
            (hb or {}).get("pid")) if (hb or {}).get("host", socket.gethostname()) == socket.gethostname() else None, None)
        alive = age <= bins.watcher_stale_seconds() and pid_ok is not False

    def count(d):
        return sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith(".")) if d.exists() else 0

    queues = {"inbox": _source("inbox", bins.count_inbox_pending, 0),
              "processing": _source("processing", lambda: sum(1 for p in bins.processing_dir().rglob("*") if p.is_file()), 0),
              "review": _source("review", lambda: count(bins.review_dir()), 0),
              "failed": _source("failed", lambda: count(bins.failed_dir()), 0)}
    window = _source("catalog_window", lambda: {"minutes": window_minutes,
                                                **_run_async(catalog.window_stats(since))}, {})
    today_raw = _source("catalog_today", lambda: _run_async(
        catalog.window_stats(_local_midnight(now, cfg.timezone).astimezone(timezone.utc))), None)
    today = {"processed": today_raw["archived"] + today_raw["review"] + today_raw["failed"],
             "review": today_raw["review"], "failed": today_raw["failed"],
             "cost_usd": today_raw["cost_usd"]} if today_raw else None
    stuck = _source("stuck", lambda: len(_run_async(catalog.get_stuck_documents(
        int(cfg.rules["stuck_docs"]["minutes"])))), 0)
    fails = _source("failures", lambda: _run_async(catalog.recent_failures(since)), [])
    from llm.provider_health import read_provider_health

    health = _source("provider_health", lambda: read_provider_health(now), {})
    or_info = _source("openrouter", lambda: openrouter_info(cfg), None)
    warm = _source("modal", modal_warm, None)
    started = (hb or {}).get("started_at")
    uptime = _source("uptime", lambda: now - datetime.fromisoformat(started).timestamp(), None) if started else None
    return RunSnapshot(
        taken_at=now, mode=cfg.run_mode, watcher_alive=alive, heartbeat_age_s=age, uptime_s=uptime,
        sha=(hb or {}).get("sha") or _git_sha(), queues=queues, window=window, stuck=stuck,
        paused=_source("pause", bins.get_pause_info, None),
        outbox_dead=_source("outbox", lambda: __import__("pipeline.mail_outbox", fromlist=["x"]).count_dead(), None),
        providers=_source("providers", lambda: provider_statuses(cfg, health, or_info, warm), []),
        runs=_source("runs", read_runs, []),
        failures=[{"name": f["filename"], "error": f["reason"], "at": f["at"]} for f in reversed(fails)],
        openrouter=or_info if or_info and "error" not in or_info else None,
        today=today, host=socket.gethostname(), repo="llm-mailroom")


# ── settings, state, log ─────────────────────────────────────────────

LOG_MAX_BYTES = 5 * 1024 * 1024
BAD_NUMBER = "not a valid number — use E.164, e.g. +16085551234"
NO_NUMBER = "no number saved — use: pager on --to +1…"


def _file(name: str) -> Path:
    return get_base_dir() / name


def read_settings() -> dict:
    try:
        d = json.loads(_file("pager_settings.json").read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_settings(*, enabled: bool | None = None, recipients: list[str] | None = None, by: str) -> dict:
    s = read_settings()
    if enabled is not None:
        s["enabled"] = bool(enabled)
    if recipients is not None:
        s["recipients"] = list(dict.fromkeys(recipients))
    s.update(updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), updated_by=by)
    _atomic_write(_file("pager_settings.json"), s)
    return s


def load_pager_config(env=None) -> PagerConfig:
    from pipeline.config import load_config

    env = os.environ if env is None else env
    block = _source("taxonomy", lambda: load_config().get("pager"), None)
    cfg = PagerConfig.resolve(block, env, environment=env.get("OBSERVABILITY_ENVIRONMENT", ""), settings=read_settings())
    for w in cfg.warnings:
        logger.warning("pager_config", detail=w)
    return cfg


def load_state():
    from pipeline.pager_core import PagerState

    try:
        return PagerState.from_json(json.loads(_file("pager_state.json").read_text()))
    except Exception:
        return PagerState()


def save_state(state) -> None:
    _atomic_write(_file("pager_state.json"), state.to_json())


def _mask(to: str) -> str:
    from pipeline.sms_notify import mask_number

    return mask_number(to)


def _log(payload: dict) -> None:
    path = _file("pager_log.jsonl")
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    with path.open("a") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ── the sender ───────────────────────────────────────────────────────


class Pager:
    """Scans every `scan.seconds`, digests on the clock, texts every recipient.

    One sender per data dir (flock on `pager.lock`); a second Pager stays
    passive. State lives in `pager_state.json`, so restarts and the CLI
    (`mute`) share it. `tick` never raises into its host."""

    def __init__(self, cfg: PagerConfig | None = None, *, transport=None):
        self._own_cfg = cfg is None
        self.cfg = cfg or load_pager_config()
        self.transport, self.http, self._lock, self._last_scan = transport, None, None, None
        self._settings_sig = self._sig()
        self.state = load_state()  # in memory from here on: the disk copy only feeds `mute` back in
        self._mutex, self._stop = threading.Lock(), threading.Event()

    @staticmethod
    def _sig():
        try:
            st = _file("pager_settings.json").stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def _can_deliver(self) -> bool:
        """True if an injected transport/override exists or the configured transport has its credentials."""
        from pipeline import sms_notify

        return bool(self.transport or sms_notify._OVERRIDE is not None or not _missing_env(self.cfg))

    def acquire(self) -> bool:
        import fcntl

        if self._lock:
            return True
        fh = open(_file("pager.lock"), "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._lock = fh
        return True

    def release(self) -> None:
        if self._lock:
            self._lock.close()
            self._lock = None

    def tick(self, now: float | None = None, *, force_scan: bool = False) -> list:
        try:
            with self._mutex:
                return self._tick(time.time() if now is None else now, force_scan=force_scan)
        except Exception as exc:  # noqa: BLE001 — never raise into the watchdog/watcher/API
            logger.warning("pager_tick_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return []

    def flush(self, now: float | None = None) -> list:
        """One forced scan now (end of a `--page` run), so a finished run's text is not left unsent."""
        return self.tick(now, force_scan=True)

    def stop(self) -> None:
        self._stop.set()

    def page_now(self, alert, now: float | None = None) -> list:
        """Page an alert the host already knows about (watchdog: watcher DOWN), through the normal gates."""
        from dataclasses import replace

        try:
            now = time.time() if now is None else now
            override = {"watcher_alive": False} if alert.key == "watcher_down" else {}
            with self._mutex:
                return self._tick(now, force_scan=True, override=override)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pager_page_now_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return []

    def _tick(self, now: float, force_scan: bool = False, override: dict | None = None) -> list:
        from dataclasses import replace

        from pipeline.pager_core import plan_digest, plan_scan

        sig = self._sig()
        if sig != self._settings_sig:  # `pager on/off/number` reach every host, explicit cfg or not
            self._settings_sig = sig
            self.cfg = load_pager_config() if self._own_cfg else self.cfg.apply_settings(read_settings())
        if self.cfg.active and not self._can_deliver():
            self.release()  # a sender that can't deliver must not hold the lock from one that can
            return []
        if not self.cfg.active or not self.acquire():
            return []
        self.state.muted_until = load_state().muted_until  # only `mute` writes state from outside
        cfg, msgs, snap = self.cfg, [], None
        window = cfg.digest_minutes(cfg.run_mode)

        def snapshot():
            s = collect_snapshot(cfg, window_minutes=window or 10, now=now)
            return replace(s, **override) if override else s
        if force_scan or self._last_scan is None or now - self._last_scan >= cfg.scan.seconds:
            snap, self._last_scan = snapshot(), now
            out, self.state = plan_scan(snap, cfg, self.state, now)
            msgs += out.messages
        out, self.state = plan_digest(lambda: snap or snapshot(), cfg, self.state, now)  # snapshots only if due
        msgs += out.messages
        results = [r for m in msgs for r in self._deliver(m, now)]
        _source("pager_state", lambda: save_state(self.state), None)
        return results

    def _deliver(self, msg, now: float) -> list:
        from pipeline.pager_core import _recent, mark_sent
        from pipeline.sms_notify import build_transport

        transport = self.transport or build_transport(self.cfg.transport)
        track = bool(msg.alert_keys)  # digests carry no alert keys: a new digest is new text, so no carried-over state
        st = self.state
        # incident identity: an alert that cleared and re-fired has a new `since`, so it never inherits old delivery state
        def ident(k: str) -> str:  # resolved messages: plan_scan moved the alert from st.open to st.recovered
            e = st.open.get(k) or st.recovered.get(k)
            return f"{k}@{e.get('since')}" if e else k
        mid = f"{msg.kind}|{','.join(sorted(ident(k) for k in msg.alert_keys))}"
        h = lambda to: hashlib.sha1(to.encode()).hexdigest()[:10]

        def live(k: str, v: dict) -> bool:  # kept while the incident is unacked; a 24h safety TTL otherwise
            if now - v.get("at", 0) >= 86400:
                return False
            kind, _, ids = k.partition("|")
            if kind not in ("alert", "resolved"):
                return True
            return all(ident(x.partition("@")[0]) == x for x in ids.split(","))
        st.delivered = {k: v for k, v in st.delivered.items() if live(k, v)}
        done = set(st.delivered.get(mid, {}).get("to", [])) if track else set()
        todo = [to for to in self.cfg.recipients if h(to) not in done]
        critical = msg.kind == "alert" and any(st.open.get(k, {}).get("severity") == "critical" for k in msg.alert_keys)
        if not critical:  # the hourly cap counts every send, so text only as many recipients as it still allows
            todo = todo[:max(0, self.cfg.alerts.max_texts_per_hour - _recent(st, now))]
        sent = [(to, transport.send(to, msg.text)) for to in todo] if transport else []
        done |= {h(to) for to, r in sent if r.ok}
        everyone = bool(self.cfg.recipients) and all(h(to) in done for to in self.cfg.recipients)
        acked = everyone if track else any(r.ok for _, r in sent)
        # ack only once every recipient has the text; each successful send still counts toward the hourly cap
        self.state = mark_sent(st, msg.kind, msg.alert_keys, now, acked, sends=sum(r.ok for _, r in sent))
        keep = {k: v for k, v in self.state.delivered.items() if k != mid}
        self.state.delivered = {**keep, mid: {"to": sorted(done), "at": now}} if track and done and not everyone else keep
        partial = track and bool(done) and not everyone
        payload = {**msg.payload, **({"delivery_partial": True} if partial else {}), "delivery":
                   [{"to": _mask(to), "ok": r.ok, "transport": r.transport,
                     "segments": r.segments, "detail": r.detail} for to, r in sent]}
        if self.cfg.machine.log:
            _source("pager_log", lambda: _log(payload), None)
        if self.cfg.machine.webhook_url:
            self._webhook(payload)
        if partial:
            logger.warning("pager_delivery_partial", kind=msg.kind, ref=msg.ref, ok=sum(r.ok for _, r in sent), to=len(sent))
        logger.info("pager_sent", kind=msg.kind, ref=msg.ref, ok=sum(r.ok for _, r in sent), to=len(sent))
        return [r for _, r in sent]

    def _webhook(self, payload: dict) -> None:
        import hashlib
        import hmac

        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get("MAILROOM_PAGER_WEBHOOK_SECRET", "")
        if secret:
            headers["X-Mailroom-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            if self.http is not None:
                self.http.post(self.cfg.machine.webhook_url, content=body, headers=headers, timeout=5.0)
            else:
                with httpx.Client(timeout=5.0) as c:
                    c.post(self.cfg.machine.webhook_url, content=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 — fail-soft, no retries
            logger.warning("pager_webhook_failed", error=type(exc).__name__)

    def start_background(self, *, idle_ok: bool = False) -> threading.Thread | None:
        """Tick on a daemon thread. `idle_ok` keeps an inactive (or lock-blocked) pager polling so `pager on` or the lock holder exiting takes effect."""
        if not self.cfg.active and not idle_ok:
            return None
        if self.cfg.active and self._can_deliver() and not self.acquire():
            logger.info("pager_passive", reason="another process holds pager.lock")
            if not idle_ok:
                return None  # idle_ok hosts keep polling: _tick retries the lock if its holder exits

        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(min(self.cfg.scan.seconds, 15))
        t = threading.Thread(target=loop, name="mailroom-pager", daemon=True)
        t.start()
        if self.cfg.run_mode == "research":
            import atexit

            atexit.register(self.flush)
        logger.info("pager_started", recipients=[_mask(r) for r in self.cfg.recipients], transport=self.cfg.transport)
        return t


# ── test text + start prompt ─────────────────────────────────────────


def send_test(cfg: PagerConfig, recipients) -> list:
    from pipeline.pager_core import _clock
    from pipeline.sms_notify import build_transport

    body = f"✅ MAILROOM PAGER TEST · {socket.gethostname()} · {_clock(time.time(), cfg.timezone, zone=True)}"
    t = build_transport(cfg.transport)
    if t is None:
        return []
    results = [(to, t.send(to, body)) for to in recipients]
    _source("pager_log", lambda: _log({"schema": "mailroom.pager/v1", "ref": None, "kind": "test",
                                        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "status": "healthy", "text": body, "delivery": [
                                            {"to": _mask(to), "ok": r.ok, "transport": r.transport,
                                             "segments": r.segments, "detail": r.detail} for to, r in results]}), None)
    return results


def _tty() -> bool:
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(input_fn, question: str, timeout_s: float) -> str | None:
    """One question; None on timeout/EOF so the caller takes the default."""
    import builtins
    import select
    import sys

    try:
        if input_fn is builtins.input:
            print(question, end="", flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
            if not ready:
                print()
                return None
            return sys.stdin.readline().strip()
        return str(input_fn(question)).strip()
    except (TimeoutError, EOFError, KeyboardInterrupt, StopIteration):
        return None


def _yes(answer: str | None, default: bool) -> bool:
    return default if not answer else answer.lower().startswith("y")


def prompt_on_start(cfg: PagerConfig, *, flag: bool | None, input_fn=input, isatty=None,
                    timeout_s: float = 30.0) -> PagerConfig:
    """Live-production start: ask whether to text alerts (default: leave as is)."""
    import sys

    from pipeline.pager_core import normalize_recipient

    if flag is not None:
        if flag and not cfg.recipients:
            print(f"--pager: {NO_NUMBER}", file=sys.stderr)
            raise SystemExit(2)
        write_settings(enabled=flag, by="flag")
        return load_pager_config()
    if cfg.run_mode != "production" or os.environ.get("MAILROOM_PAGER_PROMPT", "1") == "0":
        return cfg
    if not (isatty if isatty is not None else _tty()):
        logger.info("pager_prompt_skipped", alerts="ON" if cfg.active else "OFF",
                    hint="enable with: python -m pipeline.pager on --to +1…")
        return cfg
    if cfg.enabled and cfg.recipients:
        who = ", ".join(_mask(r) for r in cfg.recipients)
        if not _yes(_ask(input_fn, f"📱 Text alerts are ON → {who}. Keep them on? [Y/n]: ", timeout_s), True):
            write_settings(enabled=False, by="prompt")
        return load_pager_config()
    if not _yes(_ask(input_fn, "📱 Text alerts are OFF for this live run. Enable them? [y/N]: ", timeout_s), False):
        return cfg
    recips = list(cfg.recipients)
    if not recips:
        n = normalize_recipient(_ask(input_fn, "   Number to text (mobile or Google Voice, e.g. +16085551234): ",
                                     timeout_s) or "", cfg.transport)
        if not n:
            print(f"   {BAD_NUMBER} — text alerts stay OFF.")
            return cfg
        recips = [n]
    write_settings(enabled=True, recipients=recips, by="prompt")
    cfg = load_pager_config()
    if _yes(_ask(input_fn, "   Send a test text now? [Y/n]: ", timeout_s), True):
        for to, r in send_test(cfg, cfg.recipients):
            print(f"   {_mask(to)}: {'sent' if r.ok else 'FAILED — ' + r.detail}")
    return cfg


# ── CLI ──────────────────────────────────────────────────────────────


def _missing_env(cfg: PagerConfig) -> list[str]:
    if cfg.transport == "twilio":
        miss = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not os.environ.get(k)]
        if not (os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")):
            miss.append("TWILIO_FROM_NUMBER|TWILIO_MESSAGING_SERVICE_SID")
        return miss
    if cfg.transport == "email_gateway":
        return [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD") if not os.environ.get(k)]
    return []


def _read_log() -> list[dict]:
    path = _file("pager_log.jsonl")
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _summary_line(e: dict, tz: str) -> str:
    from pipeline.pager_core import _clock

    try:
        ts = datetime.fromisoformat(str(e.get("sent_at")).replace("Z", "+00:00")).timestamp()
        when = _clock(ts, tz)
    except ValueError:
        when = "?"
    lines = str(e.get("text", "")).splitlines()
    verdict = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    return f"{when} · {e.get('kind')} · {e.get('status')} · {e.get('ref')} · {verdict}"


def _duration(raw: str) -> float | None:
    m = re.fullmatch(r"(\d+)\s*([hm])", raw.strip().lower())
    return int(m[1]) * (3600 if m[2] == "h" else 60) if m else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from pipeline.pager_core import evaluate_rules, normalize_recipient, render_alert, render_digest

    ap = argparse.ArgumentParser(prog="python -m pipeline.pager", description="Operator SMS pager (text alerts).")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("status").add_argument("--full", action="store_true")
    sub.add_parser("on").add_argument("--to")
    sub.add_parser("off")
    num = sub.add_parser("number").add_subparsers(dest="op", required=True)
    for op in ("set", "add", "remove"):
        num.add_parser(op).add_argument("number")
    num.add_parser("list").add_argument("--show", action="store_true")
    sub.add_parser("test").add_argument("--to")
    pv = sub.add_parser("preview")
    pv.add_argument("--mode", choices=("production", "research"))
    pv.add_argument("--alert", action="store_true")
    pv.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=10)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--ref")
    sub.add_parser("mute").add_argument("duration", help="2h | 30m | off")
    sub.add_parser("run")
    a = ap.parse_args(argv)
    cfg = load_pager_config()

    def number(raw):
        n = normalize_recipient(raw, cfg.transport)
        if not n:
            print(BAD_NUMBER, file=sys.stderr)
        return n

    if a.verb == "status":
        miss = _missing_env(cfg)
        st, last = load_state(), (_read_log() or [None])[-1]
        print(f"Text alerts: {'ON' if cfg.active else 'OFF'}" + ("" if cfg.recipients or not cfg.enabled else f" ({NO_NUMBER})"))
        print(f"Recipients: {', '.join(_mask(r) for r in cfg.recipients) or 'none'}")
        print(f"Transport: {cfg.transport} — " + (f"not deliverable, missing {', '.join(miss)}" if miss else "deliverable"))
        muted = st.muted_until and st.muted_until > time.time()
        print(f"Mode: {cfg.run_mode} · digest every {cfg.digest_minutes(cfg.run_mode):g} min · scan every {cfg.scan.seconds:g}s"
              + (" · MUTED" if muted else "") + (f" · quiet {cfg.quiet_hours.window}" if cfg.quiet else ""))
        print("Last text: " + (_summary_line(last, cfg.timezone) if last else "none"))
        if a.full:
            for k, src in sorted(cfg.sources.items()):
                print(f"  {k} = {_getdotted(cfg, k)!r}  ({src})")
        return 0
    if a.verb == "on":
        recips = list(cfg.recipients)
        if a.to:
            n = number(a.to)
            if not n:
                return 2
            recips = [n]
        if not recips and _tty():
            n = number(input("Number to text (mobile or Google Voice, e.g. +16085551234): "))
            recips = [n] if n else []
        if not recips:
            print(NO_NUMBER, file=sys.stderr)
            return 2
        write_settings(enabled=True, recipients=recips, by="cli")
        print(f"Text alerts ON → {', '.join(_mask(r) for r in recips)}")
        if _missing_env(cfg):
            print(f"warning: not deliverable yet — set {', '.join(_missing_env(cfg))}", file=sys.stderr)
        return 0
    if a.verb == "off":
        write_settings(enabled=False, by="cli")
        print("Text alerts OFF (numbers kept).")
        return 0
    if a.verb == "number":
        saved = list(read_settings().get("recipients") or [])
        if a.op == "list":
            print(f"{len(saved)} saved number(s):")
            for r in saved:
                print(f"  {r if a.show else _mask(r)}")
            return 0
        n = number(a.number)
        if not n:
            return 2
        saved = [n] if a.op == "set" else (saved + [n] if a.op == "add" else [r for r in saved if r != n])
        write_settings(recipients=saved, by="cli")
        print(f"{'removed' if a.op == 'remove' else 'saved'} {n}")
        return 0
    if a.verb == "test":
        to = [number(a.to)] if a.to else list(cfg.recipients)
        if not to or None in to:
            print(NO_NUMBER if not a.to else BAD_NUMBER, file=sys.stderr)
            return 2
        results = send_test(cfg, to)
        if not results:
            print(f"not deliverable — missing {', '.join(_missing_env(cfg)) or 'transport'}", file=sys.stderr)
            return 1
        for t, r in results:
            print(f"{_mask(t)}: ok={r.ok} transport={r.transport} segments={r.segments} {r.detail}")
        return 0 if all(r.ok for _, r in results) else 1
    if a.verb == "preview":
        from dataclasses import replace

        mode = a.mode or cfg.run_mode
        snap = collect_snapshot(replace(cfg, run_mode=mode), window_minutes=cfg.digest_minutes(mode) or 10)

        snap, state = replace(snap, mode=mode), load_state()
        if a.alert:
            alerts = sorted(evaluate_rules(snap, cfg, {}), key=lambda x: x.severity != "critical")
            if not alerts:
                print("No alerts are firing — nothing would be texted.")
                return 0
            msg = render_alert(alerts, snap, cfg, state, snap.taken_at)
        else:
            from pipeline.pager_core import plan_scan

            _, state = plan_scan(snap, cfg, state, snap.taken_at)  # fold in this snapshot's firing rules (nothing is sent or saved)
            msg = render_digest(snap, cfg, state, snap.taken_at)
        print(json.dumps(msg.payload, indent=1, ensure_ascii=False) if a.json else msg.text)
        return 0
    if a.verb == "log":
        entries = [e for e in _read_log() if not a.ref or e.get("ref") == a.ref][-a.n:]
        for e in entries:
            print(json.dumps(e, ensure_ascii=False) if a.json else _summary_line(e, cfg.timezone))
        return 0
    if a.verb == "mute":
        st = load_state()
        secs = None if a.duration == "off" else _duration(a.duration)
        if a.duration != "off" and secs is None:
            print("use: pager mute 2h | 30m | off", file=sys.stderr)
            return 2
        st.muted_until = time.time() + secs if secs else None
        save_state(st)
        print("Alerts unmuted." if secs is None else f"Muted (criticals still text) for {a.duration}.")
        return 0
    if a.verb == "run":
        p = Pager()
        if not p.cfg.active:
            print(f"Text alerts are OFF — enable with: pager on --to +1…", file=sys.stderr)
            return 2
        if not p.acquire():
            print("another process is already the pager sender for this data dir", file=sys.stderr)
            return 1
        while True:
            p.tick()
            time.sleep(min(p.cfg.scan.seconds, 15))
    return 2


def _getdotted(cfg: PagerConfig, key: str):
    obj = cfg
    for part in key.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part)
    return obj


if __name__ == "__main__":
    import sys

    from pipeline.logging import setup_logging

    setup_logging()  # once, so later imports (watchdog, watcher) cannot re-point it at stdout
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))  # stdout stays clean for --json
    raise SystemExit(main())
