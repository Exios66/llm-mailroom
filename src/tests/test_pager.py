"""Operator SMS pager host: snapshot sources, run progress, provider status
table. Hermetic — no network, no Modal CLI, no real SMS."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pipeline.pager_core import PagerConfig

CFG = PagerConfig.resolve({}, {}, environment="live")


def _docs(rows):
    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    async def go():
        ensure_schema()
        async with async_session() as s:
            for r in rows:
                s.add(DocumentRecord(matter_id="M", original_filename=r.get("fn", r["doc_id"] + ".pdf"), **{
                    k: v for k, v in r.items() if k != "fn"}))
            await s.commit()
    asyncio.run(go())


def test_window_stats_counts_terminal_in_window(temp_base_dir):
    from storage.catalog import recent_failures, window_stats

    now = datetime.now(timezone.utc)
    old, new = now - timedelta(hours=2), now - timedelta(minutes=3)
    _docs([
        dict(doc_id="a", stage="archived", created_at=new, updated_at=new, cost_usd=0.10, latency_s=40.0,
             scores={"success_rate": 1.0}),
        dict(doc_id="b", stage="archived", created_at=new, updated_at=new, cost_usd=0.05, latency_s=60.0,
             scores={"success_rate": 0.0}),
        dict(doc_id="c", stage="review", created_at=new, updated_at=new),
        dict(doc_id="d", stage="failed", created_at=old, updated_at=new, escalation_reason="extraction timed out"),
        dict(doc_id="e", stage="failed", created_at=old, updated_at=old),
        dict(doc_id="f", stage="processing", created_at=new, updated_at=new),
    ])
    w = asyncio.run(window_stats(now - timedelta(minutes=10)))
    assert (w["received"], w["archived"], w["review"], w["failed"]) == (4, 2, 1, 1)
    assert w["cost_usd"] == pytest.approx(0.15) and w["p50_latency_s"] == 50.0 and w["first_pass_rate"] == 0.5
    f = asyncio.run(recent_failures(now - timedelta(minutes=10)))
    assert f == [{"filename": "d.pdf", "stage": "failed", "reason": "extraction timed out",
                  "at": pytest.approx(new.timestamp())}]


def test_window_stats_filters_run_id(temp_base_dir):
    from storage.catalog import window_stats

    now = datetime.now(timezone.utc)
    _docs([dict(doc_id="a", stage="archived", run_id="r1", created_at=now, updated_at=now),
           dict(doc_id="b", stage="archived", run_id="r2", created_at=now, updated_at=now)])
    assert asyncio.run(window_stats(now - timedelta(minutes=1), run_id="r1"))["archived"] == 1
    empty = asyncio.run(window_stats(now + timedelta(minutes=1)))
    assert empty["received"] == 0 and empty["p50_latency_s"] is None and empty["first_pass_rate"] is None


def test_progress_roundtrip_and_stale(temp_base_dir):
    from pipeline.pager import read_runs, report_run_progress

    report_run_progress("pilot/real 1", label="run_pilot --real", total=10, done=0)
    first = read_runs()[0]
    report_run_progress("pilot/real 1", label="run_pilot --real", total=10, done=4, metrics={"acc": 0.9})
    (r,) = read_runs()
    assert r.run_id == "pilot/real 1" and r.done == 4 and r.started_at == first.started_at
    assert r.metrics == {"acc": 0.9} and not r.finished
    path = next((temp_base_dir / "pager_runs").glob("*.json"))
    data = json.loads(path.read_text())
    data["updated_at"] -= 3600
    path.write_text(json.dumps(data))
    assert read_runs()[0].updated_at < time.time() - 3000      # stale but still listed → run_stalled can fire


@pytest.mark.parametrize("name,health,or_info,warm,state,detail", [
    ("modal:extract", {"calls": 5, "errors": 5, "consecutive_errors": 5, "last_status": 502}, None, {}, "down", "HTTP 502"),
    ("modal:extract", {"calls": 8, "errors": 2, "consecutive_errors": 0, "count_503": 0}, None, {}, "degraded", ""),
    ("modal:extract", {"calls": 3, "errors": 0, "consecutive_errors": 0, "count_503": 1, "ok_calls": 2}, None, {}, "up", "cold start: 1× 503"),
    ("modal:extract", {"calls": 5, "errors": 0, "consecutive_errors": 0, "count_503": 5, "ok_calls": 0}, None, {}, "degraded", "cold start: 5× 503, no success yet"),
    ("modal:extract", None, None, {"extract": 1}, "warm", "1 container"),
    ("modal:extract", None, None, {}, "idle", ""),
    ("modal:extract", {"calls": 4, "errors": 0, "consecutive_errors": 0, "count_503": 0}, None, {}, "up", ""),
    ("openrouter", None, {"error": "HTTP 401"}, {}, "down", "HTTP 401"),
    ("openrouter", {"calls": 3, "errors": 3, "consecutive_errors": 3, "last_status": 500}, None, {}, "down", "HTTP 500"),
    ("openrouter", None, {"limit_remaining": 4.12, "usage": 1.0, "is_free_tier": False}, {}, "up", "$4.12 left"),
    ("openrouter", None, {"limit_remaining": None, "usage": 1.0, "is_free_tier": False}, {}, "up", "no limit"),
])
def test_provider_status_table(name, health, or_info, warm, state, detail, monkeypatch):
    from pipeline import pager

    monkeypatch.setattr(pager, "_breaker_open", lambda: False)
    st = {p.name: p for p in pager.provider_statuses(CFG, {name: health} if health else {}, or_info, warm)}
    assert st[name].state == state and st[name].detail == detail


def test_openrouter_breaker_degraded(monkeypatch):
    from pipeline import pager

    monkeypatch.setattr(pager, "_breaker_open", lambda: True)
    st = {p.name: p for p in pager.provider_statuses(CFG, {}, {"limit_remaining": 3.0}, {})}
    assert st["openrouter"].state == "degraded" and st["openrouter"].detail == "free pool rate-limited"


def test_openrouter_info_cached(monkeypatch):
    from pipeline import pager

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(pager, "_OR_CACHE", {})
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": {"limit_remaining": 4.12, "usage": 15.88, "is_free_tier": False}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    a = pager.openrouter_info(CFG, client)
    b = pager.openrouter_info(CFG, client)
    assert a == b == {"limit_remaining": 4.12, "usage": 15.88, "is_free_tier": False}
    assert calls == ["/api/v1/key"]
    monkeypatch.setattr(pager, "_OR_CACHE", {})
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert pager.openrouter_info(CFG, bad) == {"error": "HTTP 401"}
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(pager, "_OR_CACHE", {})
    assert pager.openrouter_info(CFG, client) is None


def test_modal_cli_absent_returns_none(monkeypatch):
    from pipeline import pager

    monkeypatch.setattr(pager.shutil, "which", lambda _: None)
    assert pager.modal_warm() is None
    monkeypatch.setattr(pager.shutil, "which", lambda _: "/usr/bin/modal")

    class R:
        returncode, stdout = 0, json.dumps([
            {"App Name": "mailroom-vllm", "Function": "serve_extract"},
            {"App Name": "mailroom-vllm", "Function": "serve_extract"},
            {"App Name": "other", "Function": "serve_fast"}])
    assert pager.modal_warm(run=lambda *a, **k: R()) == {"extract": 2}


def test_snapshot_survives_missing_catalog(temp_base_dir, monkeypatch):
    from pipeline import pager

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(pager, "openrouter_info", lambda *a, **k: None)
    monkeypatch.setattr(pager, "modal_warm", lambda *a, **k: None)
    s = pager.collect_snapshot(CFG, window_minutes=10)
    assert s.window == {} and s.today is None and s.failures == []
    assert s.queues["inbox"] == 0 and s.mode == "production" and s.repo == "llm-mailroom"


def test_snapshot_watcher_dead(temp_base_dir, monkeypatch):
    from pipeline import pager

    monkeypatch.setattr(pager, "openrouter_info", lambda *a, **k: None)
    monkeypatch.setattr(pager, "modal_warm", lambda *a, **k: None)
    (temp_base_dir / "watcher_heartbeat").write_text(json.dumps({"ts": time.time() - 900, "pid": 999999}))
    s = pager.collect_snapshot(CFG, window_minutes=10)
    assert s.watcher_alive is False and s.heartbeat_age_s >= 899


# ── Pager host, CLI, start prompt (Task 5) ───────────────────────────
import hashlib
import hmac
import logging
from zoneinfo import ZoneInfo

from pipeline.pager_core import Alert, ProviderStatus as PS, RunSnapshot
from pipeline.sms_notify import SendResult, set_transport_override

TZ = ZoneInfo("America/Chicago")
MOBILE, GV = "+16085551234", "+14145559876"


def at(hhmm, day=15):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


class Rec:
    name = "rec"

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, to, body):
        self.sent.append((to, body))
        return SendResult(self.ok, "rec", 2, "SM123" if self.ok else "HTTP 500")


def healthy(now, **kw):
    d = dict(taken_at=now, mode="production", watcher_alive=True, heartbeat_age_s=1.0, uptime_s=60.0, sha="abc",
             queues={"inbox": 0, "processing": 0, "review": 0, "failed": 0},
             window={"minutes": 10, "received": 2, "archived": 2, "review": 0, "failed": 0}, stuck=0, paused=None,
             outbox_dead=0, providers=[PS("openrouter", "up")], runs=[], failures=[], openrouter=None,
             today=None, host="mac", repo="llm-mailroom")
    d.update(kw)
    return RunSnapshot(**d)


def broken(now):
    return healthy(now, providers=[PS("modal:extract", "down", "HTTP 502", 5, 5)])


@pytest.fixture
def host(temp_base_dir, monkeypatch):
    from pipeline import pager

    for k in [k for k in os.environ if k.startswith("MAILROOM_PAGER")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("MAILROOM_PAGER_TZ", "America/Chicago")
    feed = {"fn": healthy}
    monkeypatch.setattr(pager, "collect_snapshot", lambda cfg, window_minutes, now=None: feed["fn"](now or time.time()))
    rec = Rec()
    set_transport_override(rec)
    yield pager, rec, feed
    set_transport_override(None)



def make(pager, rec, **block):
    base = {"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago", **block}
    return pager.Pager(PagerConfig.resolve(base, {}, environment="live"), transport=rec)


def ticks(p, start, minutes, step=20):
    return [r for t in range(0, int(minutes * 60), step) for r in p.tick(start + t)]


def test_digest_every_10_minutes_aligned(host):
    pager, rec, _ = host
    ticks(make(pager, rec), at("14:00") + 5, 61)
    assert len(rec.sent) == 6 and all(b.startswith("Mailroom LIVE · ") for _, b in rec.sent)
    assert rec.sent[0][1].splitlines()[0].endswith(" 2:10 PM CDT")


def test_custom_cadence_from_taxonomy(host):
    pager, rec, _ = host
    ticks(make(pager, rec, digest={"production_minutes": 30}), at("14:00") + 5, 61)
    assert len(rec.sent) == 2


def test_healthy_scans_send_nothing_between_digests(host):
    pager, rec, _ = host
    ticks(make(pager, rec), at("14:00") + 5, 9)
    assert rec.sent == []


def test_sends_to_every_recipient(host):
    pager, rec, feed = host
    feed["fn"] = broken
    make(pager, rec, recipients=[MOBILE, GV]).tick(at("14:01"))
    assert sorted(to for to, _ in rec.sent) == sorted([MOBILE, GV])


def test_failed_send_keeps_alert_pending(host):
    pager, _, feed = host
    feed["fn"] = broken
    bad = Rec(ok=False)
    p = make(pager, bad)
    p.tick(at("14:01"))
    assert len(bad.sent) == 1 and "provider_down:modal:extract" in p.state.pending
    bad.ok = True
    p.tick(at("14:02"))
    p.tick(at("14:03"))
    p.tick(at("14:04"))
    alerts = [b for _, b in bad.sent if b.startswith("Mailroom ALERT")]
    assert len(alerts) == 2 and p.state.pending == []


def test_state_survives_restart(host):
    pager, rec, feed = host
    feed["fn"] = broken
    make(pager, rec).tick(at("14:01"))
    p2 = make(pager, rec)
    p2.tick(at("14:03"))
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1
    assert p2.state.next_digest == at("14:10")


def test_mute_cli_writes_state(host):
    pager, rec, _ = host
    assert pager.main(["mute", "2h"]) == 0
    st = json.loads((get_base() / "pager_state.json").read_text())
    assert st["muted_until"] > time.time() + 7000
    assert pager.main(["mute", "off"]) == 0
    assert json.loads((get_base() / "pager_state.json").read_text())["muted_until"] is None


def get_base():
    from pipeline.bins import get_base_dir

    return get_base_dir()


def test_second_pager_is_passive(host):
    pager, rec, _ = host
    a, b = make(pager, rec), make(pager, rec)
    assert a.acquire() is True and b.acquire() is False
    a.release()


def test_watchdog_down_also_pages(host, monkeypatch):
    pager, rec, _ = host
    from pipeline import watchdog

    emails = []
    monkeypatch.setenv("MAILROOM_WATCHDOG", "1")
    monkeypatch.setattr("pipeline.status_notify.send_status_email", lambda *a, **k: emails.append(a) or True)
    monkeypatch.setattr(watchdog, "read_heartbeat", lambda: {"ts": 0, "pid": 999999})
    monkeypatch.setattr(watchdog, "watcher_heartbeat_age", lambda: 9999.0)
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(pager, "load_pager_config", lambda env=None: PagerConfig.resolve(
        {"enabled": True, "recipients": [MOBILE]}, {}, environment="live"))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        watchdog.main()
    assert emails and any("Action needed: the watcher has stopped" in b for _, b in rec.sent)


def test_run_started_and_finished_texts_once(host):
    pager, rec, feed = host
    from pipeline.pager_core import RunProgress

    run = RunProgress("r1", "run_pilot --mock", 3, 0, at("14:00"), at("14:00"), False, {})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[run])
    p = make(pager, rec, mode="research")
    p.tick(at("14:00") + 5)
    p.tick(at("14:01") + 5)
    done = RunProgress("r1", "run_pilot --mock", 3, 3, at("14:00"), at("14:02"), True, {"failed": 0})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[done])
    p.tick(at("14:02") + 5)
    p.tick(at("14:03") + 5)
    kinds = [b.splitlines()[1][:14] for _, b in rec.sent]
    assert kinds == ["▶ Run started:", "✅ Run finished"]


def test_run_pilot_reports_progress(temp_base_dir, monkeypatch):
    from pipeline import pager
    from scripts import run_pilot

    monkeypatch.setattr(run_pilot, "run_sample", lambda m, mock, session_id=None, run_id=None: {
        "id": m["id"], "filename": m["filename"], "stage": "archived", "class_match": True, "wall_time_s": 1.0,
        "llm_cost_usd": 0.01, "llm_calls": 1, "doc_type": m["expected_doc_class"]})
    monkeypatch.setattr(run_pilot, "prepare_samples", lambda: None)
    manifest = temp_base_dir / "manifest.csv"
    manifest.write_text("id,filename,subdir,expected_doc_class,expected_stage\n" + "".join(
        f"s{i},s{i}.txt,x,contract,archived\n" for i in range(5)))
    monkeypatch.setattr(run_pilot, "MANIFEST", manifest)
    monkeypatch.setattr(run_pilot, "_validate_manifest_ground_truth", lambda m: None)
    monkeypatch.setattr(run_pilot, "print_rows", lambda rows: None)
    monkeypatch.setattr(run_pilot, "summarize", lambda rows: {})
    monkeypatch.setattr(run_pilot, "print_summary", lambda s: None)
    monkeypatch.setattr(run_pilot, "misfile_candidates", lambda rows, report=None: [])
    run_pilot.main(["--mock", "--max-docs", "3"])
    (r,) = pager.read_runs()
    assert r.done == r.total == 3 and r.finished and r.metrics["acc"] == 1.0 and r.metrics["failed"] == 0


def test_cli_preview_and_status_never_send(host, capsys):
    pager, rec, _ = host
    assert pager.main(["preview", "--mode", "production"]) == 0
    out = capsys.readouterr().out
    assert "Mailroom LIVE · " in out and "All clear" in out
    assert pager.main(["preview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "mailroom.pager/v1"
    assert pager.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "OFF" in out and rec.sent == []
    assert not (get_base() / "pager_log.jsonl").exists()


def test_log_jsonl_written_with_delivery_and_rotates(host, monkeypatch):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec, recipients=[MOBILE, GV])
    p.tick(at("14:01"))
    lines = (get_base() / "pager_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert [d["to"] for d in entry["delivery"]] == ["+1•••••1234", "+1•••••9876"]
    assert entry["delivery"][0] == {"to": "+1•••••1234", "ok": True, "transport": "rec", "segments": 2, "detail": "SM123"}
    assert MOBILE not in lines[0]
    monkeypatch.setattr(pager, "LOG_MAX_BYTES", 10)
    feed["fn"] = healthy
    p.tick(at("14:10"))
    assert (get_base() / "pager_log.jsonl.1").exists()


def test_webhook_posts_payload_with_signature(host, monkeypatch):
    pager, rec, feed = host
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(204)
    monkeypatch.setenv("MAILROOM_PAGER_WEBHOOK_SECRET", "s3cret")
    feed["fn"] = broken
    p = make(pager, rec, machine={"webhook_url": "https://hooks.example/pager"})
    p.http = httpx.Client(transport=httpx.MockTransport(handler))
    p.tick(at("14:01"))
    (req,) = seen
    assert not p.http.is_closed
    body = req.content
    assert json.loads(body)["kind"] == "alert"
    assert req.headers["X-Mailroom-Signature"] == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    p.http = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    p.tick(at("14:20"))                                       # soft failure
    assert not p.http.is_closed


@pytest.mark.parametrize("fail", [False, True])
def test_webhook_closes_owned_client(host, monkeypatch, fail):
    pager, rec, _ = host
    client_type, clients, seen = httpx.Client, [], []

    def handler(request):
        seen.append(request)
        if fail:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(204)

    def factory(**kwargs):
        client = client_type(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(pager.httpx, "Client", factory)
    p = make(pager, rec, machine={"webhook_url": "https://hooks.example/pager"})
    p._webhook({"kind": "test"})
    assert len(seen) == len(clients) == 1 and clients[0].is_closed


def test_cli_log_human_and_json(host, capsys):
    pager, rec, feed = host
    feed["fn"] = broken
    make(pager, rec).tick(at("14:01"))
    assert pager.main(["log"]) == 0
    out = capsys.readouterr().out
    assert " · alert · action · P-1401-" in out and "Action needed" in out
    assert pager.main(["log", "--json", "-n", "1"]) == 0
    ref = json.loads(capsys.readouterr().out)["ref"]
    assert pager.main(["log", "--ref", ref]) == 0 and ref in capsys.readouterr().out


def test_cli_on_off_persists_and_running_pager_picks_up(host, monkeypatch):
    pager, rec, feed = host
    feed["fn"] = broken
    assert pager.main(["on", "--to", MOBILE]) == 0
    p = pager.Pager(transport=rec)
    assert pager.main(["off"]) == 0
    p.tick(at("14:01"))
    assert rec.sent == []
    assert pager.read_settings()["recipients"] == [MOBILE]
    assert pager.main(["on"]) == 0
    p.tick(at("14:02"))
    assert len(rec.sent) == 1


def test_cli_number_set_add_remove_list_masked(host, capsys):
    pager, *_ = host
    assert pager.main(["number", "set", "608-555-1234"]) == 0
    assert "+16085551234" in capsys.readouterr().out
    assert pager.read_settings()["recipients"] == [MOBILE]
    pager.main(["number", "add", GV])
    pager.main(["number", "list"])
    out = capsys.readouterr().out
    assert "+1•••••1234" in out and "+1•••••9876" in out and MOBILE not in out.split("saved")[-1]
    pager.main(["number", "list", "--show"])
    assert GV in capsys.readouterr().out
    pager.main(["number", "remove", MOBILE])
    assert pager.read_settings()["recipients"] == [GV]


def test_cli_bad_number_exits_2(host, capsys):
    pager, *_ = host
    assert pager.main(["number", "set", "12345"]) == 2
    assert "not a valid number — use E.164, e.g. +16085551234" in capsys.readouterr().err


def test_cli_on_without_number_non_tty_exits_2(host, capsys, monkeypatch):
    pager, *_ = host
    monkeypatch.setattr(pager, "_tty", lambda: False)
    assert pager.main(["on"]) == 2
    assert "no number saved — use: pager on --to +1…" in capsys.readouterr().err


def test_cli_test_to_adhoc_number_does_not_save(host, capsys):
    pager, rec, _ = host
    assert pager.main(["test", "--to", "(414) 555-9876"]) == 0
    assert rec.sent[0][0] == GV and rec.sent[0][1].startswith("✅ MAILROOM PAGER TEST · ")
    assert pager.read_settings().get("recipients") in (None, [])
    set_transport_override(Rec(ok=False))
    assert pager.main(["test", "--to", GV]) == 1


def _answers(*xs):
    it = iter(xs)

    def fn(prompt):
        v = next(it)
        if isinstance(v, BaseException):
            raise v
        return v
    return fn


def live_cfg(pager):
    return pager.load_pager_config({"OBSERVABILITY_ENVIRONMENT": "live"})


def test_prompt_enable_with_number_and_test(host, capsys):
    pager, rec, _ = host
    cfg = pager.prompt_on_start(live_cfg(pager), flag=None, input_fn=_answers("y", "608 555 1234", ""), isatty=True)
    assert cfg.enabled and cfg.recipients == (MOBILE,)
    s = pager.read_settings()
    assert s["updated_by"] == "prompt" and s["enabled"] is True
    assert len(rec.sent) == 1 and "PAGER TEST" in rec.sent[0][1]


def test_prompt_default_keeps_off_on_timeout(host):
    pager, rec, _ = host
    cfg = pager.prompt_on_start(live_cfg(pager), flag=None, input_fn=_answers(TimeoutError()), isatty=True)
    assert not cfg.enabled and rec.sent == []


def test_prompt_skipped_non_tty_logs_hint(host, capsys):
    pager, *_ = host
    cfg = pager.prompt_on_start(live_cfg(pager), flag=None, input_fn=_answers(), isatty=False)
    assert not cfg.enabled
    assert "pager_prompt_skipped" in capsys.readouterr().out


def test_prompt_skipped_in_research_mode_and_with_flags(host):
    pager, *_ = host
    research = pager.load_pager_config({"OBSERVABILITY_ENVIRONMENT": "pilot"})
    assert pager.prompt_on_start(research, flag=None, input_fn=_answers(), isatty=True) is research
    cfg = pager.prompt_on_start(live_cfg(pager), flag=False, input_fn=_answers(), isatty=True)
    assert not cfg.enabled and pager.read_settings()["updated_by"] == "flag"
    with pytest.raises(SystemExit) as e:
        pager.prompt_on_start(live_cfg(pager), flag=True, input_fn=_answers(), isatty=True)
    assert e.value.code == 2


def test_prompt_already_on_asks_keep(host):
    pager, *_ = host
    pager.write_settings(enabled=True, recipients=[MOBILE], by="cli")
    asked = []

    def fn(q):
        asked.append(q)
        return "n"
    cfg = pager.prompt_on_start(live_cfg(pager), flag=None, input_fn=fn, isatty=True)
    assert asked == ["📱 Text alerts are ON → +1•••••1234. Keep them on? [Y/n]: "] and not cfg.enabled


def test_run_experiment_local_reports_progress(temp_base_dir):
    from pipeline import pager
    from scripts.run_experiment import run_local

    items = [{"input": {"n": i}, "expected_output": {"doc_type": "contract"}, "metadata": {}} for i in range(2)]
    seen = []
    run_local(items, lambda item: {"doc_type": "contract", "stage": "archived"}, evaluators=[],
              on_progress=lambda rows, errors, finished: seen.append((len(rows), errors, finished)))
    assert seen == [(1, 0, False), (2, 0, False), (2, 0, True)]
    from scripts.run_experiment import _report_progress

    _report_progress("exp-1", "run_experiment --mock", 2, [{"expected": "contract", "got": "contract"}], 0, False)
    (r,) = pager.read_runs()
    assert r.done == 1 and r.total == 2 and r.metrics["acc"] == 1.0


def test_gateway_api_tier_and_unconfigured_openrouter_are_not_listed(monkeypatch):
    from pipeline import pager

    monkeypatch.setattr(pager, "_breaker_open", lambda: False)
    monkeypatch.setattr(pager, "_modal_tiers", lambda: ["fast", "extract", "vision"])
    names = [p.name for p in pager.provider_statuses(CFG, {}, None, {})]
    assert names == ["modal:fast", "modal:extract", "modal:vision"]


def test_modal_tiers_skip_gateway_api_tier():
    from pipeline.pager import _modal_tiers

    assert "api" not in _modal_tiers()


# ── final-review fixes ───────────────────────────────────────────────


def test_explicit_config_pager_honors_cli_off_and_number_changes(host):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)  # hosts (watcher, API, --page) pass an explicit cfg
    p.tick(at("14:01"))
    assert len(rec.sent) == 1
    assert pager.main(["off"]) == 0
    for m in range(2, 50):
        p.tick(at("14:00") + m * 60)
    assert len(rec.sent) == 1
    assert pager.main(["number", "set", GV]) == 0 and pager.main(["on"]) == 0
    p.tick(at("14:51"))
    assert rec.sent[-1][0] == GV


def test_state_save_failure_does_not_resend_alerts(host, monkeypatch):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)

    def full(_):
        raise OSError("No space left on device")
    monkeypatch.setattr(pager, "save_state", full)
    for m in range(1, 7):
        p.tick(at("14:00") + m * 60)
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1


def test_cli_mute_reaches_running_pager_without_clobbering_its_state(host):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)
    p.tick(at("14:01"))
    sent_at = p.state.open["provider_down:modal:extract"]["last_sent"]
    (get_base() / "pager_state.json").write_text("{}")  # a CLI read-modify-write from a stale copy
    assert pager.main(["mute", "2h"]) == 0
    p.tick(at("14:02"))
    assert p.state.muted_until and p.state.open["provider_down:modal:extract"]["last_sent"] == sent_at
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1


def test_flush_sends_run_finished_before_exit(host):
    pager, rec, feed = host
    from pipeline.pager_core import RunProgress

    run = RunProgress("r1", "run_pilot --mock", 3, 0, at("14:00"), at("14:00"), False, {})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[run])
    p = make(pager, rec, mode="research")
    p.tick(at("14:00"))
    done = RunProgress("r1", "run_pilot --mock", 3, 3, at("14:00"), at("14:00") + 20, True, {"failed": 0})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[done])
    p.flush(at("14:00") + 21)  # well inside the 60 s scan interval
    assert [b.splitlines()[1][:14] for _, b in rec.sent] == ["▶ Run started:", "✅ Run finished"]


def test_research_background_pager_flushes_at_exit(host, monkeypatch):
    pager, rec, _ = host
    registered = []
    monkeypatch.setattr("atexit.register", lambda fn, *a, **k: registered.append(fn))
    p = make(pager, rec, mode="research")
    thread = None
    try:
        thread = p.start_background()
        assert thread is not None
        assert p.flush in registered
    finally:
        p.stop()
        if thread is not None:
            thread.join(timeout=5)


def test_digest_snapshot_only_taken_when_due(host, monkeypatch):
    pager, rec, feed = host
    calls = []
    feed["fn"] = lambda now: calls.append(now) or healthy(now)
    p = make(pager, rec, digest={"production_minutes": 0})
    ticks(p, at("14:00") + 5, 3, step=15)  # 12 host ticks, 3 scans
    assert len(calls) == 3
    calls.clear()
    p.release()
    q = make(pager, rec, quiet_hours={"window": "22:00-07:00"})
    ticks(q, at("23:00") + 5, 3, step=15)
    assert len(calls) == 3


def test_watchdog_runs_pager_off_its_poll_loop(host, monkeypatch):
    pager, rec, _ = host
    from pipeline import watchdog

    started = []
    monkeypatch.setenv("MAILROOM_WATCHDOG", "1")
    monkeypatch.setattr(watchdog, "read_heartbeat", lambda: {"ts": time.time(), "pid": os.getpid()})
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(pager.Pager, "start_background", lambda self, **k: started.append(k) or None)
    monkeypatch.setattr(pager.Pager, "tick", lambda self, now=None: pytest.fail("pager ticked inline"))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        watchdog.main()
    assert started == [{"idle_ok": True}]


def test_cli_accepts_gateway_address_with_email_gateway_transport(host, monkeypatch):
    pager, *_ = host
    monkeypatch.setenv("MAILROOM_PAGER_TRANSPORT", "email_gateway")
    assert pager.main(["on", "--to", "6085551234@vtext.com"]) == 0
    assert pager.load_pager_config().recipients == ("6085551234@vtext.com",)


def test_rate_limits_and_bad_requests_are_not_outages(monkeypatch, tmp_path):
    from llm import provider_health as ph

    monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "1")
    monkeypatch.setattr(ph, "_path", lambda: tmp_path / "provider_health.json")
    monkeypatch.setattr(ph, "_BUF", ph._Buffer())
    url = "https://openrouter.ai/api/v1"
    for status in (429, 429, 400, 429):
        ph.record_llm_outcome(url, "m", ok=False, status=status, latency_s=0.1, now=1000.0)
    h = ph.read_provider_health(now=1001.0)["openrouter"]
    assert h["errors"] == 0 and h["consecutive_errors"] == 0
    for status in (502, None, 500):
        ph.record_llm_outcome(url, "m", ok=False, status=status, latency_s=0.1, now=1002.0)
    h = ph.read_provider_health(now=1003.0)["openrouter"]
    assert h["errors"] == 3 and h["consecutive_errors"] == 3


def test_openrouter_info_refuses_plain_http_for_remote_hosts(host, monkeypatch):
    pager, *_ = host
    import langchain_agents.openrouter_utils as oru

    cfg = pager.load_pager_config()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real")
    pager._OR_CACHE.clear()
    sent = []

    class C:
        def get(self, url, **kw):
            sent.append(url)
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": {}}})()

    monkeypatch.setattr(oru, "openrouter_base_url", lambda: "http://proxy.example.com/v1")
    assert pager.openrouter_info(cfg, client=C()) == {"error": "insecure base URL"}
    assert sent == []
    pager._OR_CACHE.clear()
    monkeypatch.setattr(oru, "openrouter_base_url", lambda: "http://localhost:4000/v1")
    assert "error" not in pager.openrouter_info(cfg, client=C())
    assert sent == ["http://localhost:4000/v1/key"]
    pager._OR_CACHE.clear()


def test_preview_mode_research_collects_research_stats(host, monkeypatch):
    pager, _, feed = host
    seen = []
    monkeypatch.setattr(pager, "collect_snapshot",
                        lambda cfg, window_minutes, now=None: seen.append(cfg.run_mode) or feed["fn"](now or time.time()))
    assert pager.main(["preview", "--mode", "research"]) == 0
    assert seen == ["research"]


def test_preview_digest_reflects_active_failures(host, capsys):
    pager, _, feed = host
    feed["fn"] = broken
    assert pager.main(["preview"]) == 0
    assert "All clear" not in capsys.readouterr().out


def test_partial_delivery_retries_only_failed_recipients(host):
    pager, _, feed = host

    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    feed["fn"] = broken
    t = Flaky()
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                              {}, environment="live")
    p = pager.Pager(cfg, transport=t)
    p.tick(at("14:01"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]
    assert all(not e.get("last_sent") for e in p.state.open.values())  # not acked: GV still owed
    entry = json.loads((get_base() / "pager_log.jsonl").read_text().splitlines()[0])
    assert entry["delivery_partial"] is True
    t.down, t.sent = set(), []
    p.tick(at("14:01") + 90)
    assert [to for to, _ in t.sent] == [GV]  # MOBILE is not texted twice
    assert all(e.get("last_sent") for e in p.state.open.values())
    assert p.state.delivered == {}
    p.tick(at("14:01") + 180)
    assert [to for to, _ in t.sent] == [GV]  # acked: no further retry


def test_idle_ok_pager_keeps_polling_when_another_sender_holds_the_lock(host):
    pager, rec, _ = host
    first, second = make(pager, rec), make(pager, rec)
    assert first.acquire()
    thread = None
    try:
        thread = second.start_background(idle_ok=True)
        assert thread is not None and second._lock is None  # passive, but still polling
        assert make(pager, rec).start_background() is None  # batch hosts still stand down
        first.release()
        second.tick(at("14:00") + 5)
        assert second._lock is not None  # took over once the owner left
    finally:
        second.stop()
        if thread is not None:
            thread.join(timeout=5)
        second.release()
        first.release()



def test_credentialless_pager_does_not_take_the_send_lock(host, monkeypatch):
    pager, rec, feed = host
    set_transport_override(None)  # real credential checks, not the test override
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_MESSAGING_SERVICE_SID"):
        monkeypatch.delenv(k, raising=False)
    feed["fn"] = broken
    cfg = {"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago", "transport": "twilio"}
    blind = pager.Pager(PagerConfig.resolve(cfg, {}, environment="live"))  # embedded API pager: no creds
    sender = pager.Pager(PagerConfig.resolve(cfg, {}, environment="live"), transport=rec)
    thread = blind.start_background(idle_ok=True)  # starts first
    try:
        assert thread is not None and blind._lock is None  # idle, polling, not holding the lock
        assert blind.tick(at("14:01")) == [] and blind._lock is None
        assert sender.tick(at("14:01")) and rec.sent  # the pager that can deliver wins the lock
        assert sender._lock is not None
        sender.release()
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")  # creds appear later: it competes normally
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_FROM_NUMBER", "+16085550000")
        blind.tick(at("14:02"))
        assert blind._lock is not None
    finally:
        blind.stop()
        if thread is not None:
            thread.join(timeout=5)
        blind.release()
        sender.release()

def test_new_digest_is_not_limited_by_an_earlier_partial_digest(host):
    pager, _, _ = host

    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    t = Flaky()
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                                        {}, environment="live"), transport=t)
    p.tick(at("14:10") + 5)  # first digest: GV fails
    t.down, t.sent = set(), []
    p.tick(at("14:20") + 5)  # next digest is new text: every recipient gets it
    assert [to for to, _ in t.sent] == [MOBILE, GV]


def test_cap_limits_recipients_texted_and_the_rest_stay_pending(host):
    pager, rec, feed = host
    feed["fn"] = lambda now: healthy(now, queues={"inbox": 0, "processing": 0, "review": 50, "failed": 0})
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago",
                                         "alerts": {"max_texts_per_hour": 1}}, {}, environment="live"), transport=rec)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    from pipeline.pager_core import Message
    p._deliver(Message("alert", "r1", "x", {}, ("review_backlog",)), at("14:01"))
    assert [to for to, _ in rec.sent] == [MOBILE]  # cap of 1: only one recipient this hour
    assert len(p.state.sent_log) == 1 and not p.state.open["review_backlog"]["last_sent"]
    assert "review_backlog" in p.state.pending


def _warn_pager(pager, rec, cap):
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago",
                                         "alerts": {"max_texts_per_hour": cap}}, {}, environment="live"), transport=rec)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    return p


def test_delivery_record_outlives_an_hour_while_the_alert_is_unacked(host):
    pager, rec, _ = host
    from pipeline.pager_core import Message
    p = _warn_pager(pager, rec, 1)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))
    p._deliver(msg, at("15:30"))  # allowance is back: the other recipient, not the first again
    assert [to for to, _ in rec.sent] == [MOBILE, GV]


def test_recurring_alert_does_not_inherit_the_old_incident_delivery(host):
    pager, rec, _ = host
    from pipeline.pager_core import Message
    p = _warn_pager(pager, rec, 1)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # MOBILE only
    p.state.open["review_backlog"]["since"] = at("16:00")  # cleared quietly, then fired again
    p._deliver(msg, at("16:01"))
    assert [to for to, _ in rec.sent] == [MOBILE, MOBILE]


def _flaky_pager(pager):
    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    t = Flaky()
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                              {}, environment="live")
    p = pager.Pager(cfg, transport=t)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    return p, t


def test_alert_that_recurs_within_the_ttl_goes_to_everyone_again(host):
    pager, _, _ = host
    from pipeline.pager_core import Message
    p, t = _flaky_pager(pager)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # MOBILE ok, GV fails: partial
    assert [to for to, _ in t.sent] == [MOBILE, GV]
    p.state.open["review_backlog"]["since"] = at("14:20")  # cleared, then a new incident
    t.down, t.sent = set(), []
    p._deliver(msg, at("14:21"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]


def test_recovery_that_recurs_does_not_reuse_a_partial_recovery_record(host):
    pager, _, _ = host
    from pipeline.pager_core import Message
    p, t = _flaky_pager(pager)
    e = p.state.open.pop("review_backlog")
    p.state.recovered["review_backlog"] = {**e, "open_s": 60.0}
    msg = Message("resolved", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # partial
    p.state.recovered["review_backlog"]["since"] = at("14:20")  # the alert fired and recovered again
    t.down, t.sent = set(), []
    p._deliver(msg, at("14:21"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]
