"""HUB-050 status channel: 🟢/🔴/🟠 emails, enriched heartbeat, watchdog
transitions. Hermetic — the SMTP factory seam intercepts every send, and
conftest keeps MAILROOM_WATCHER_STATUS=0 unless a case opts in."""
import json
import os
import time

import pytest

from pipeline.bins import touch_watcher_heartbeat, watcher_heartbeat_age


# ── status email plumbing ────────────────────────────────────────────


def test_status_email_renders_all_kinds():
    from pipeline.status_notify import _render_html, _render_text

    rows = [("PID", "123"), ("Host", "mac")]
    for kind, emoji in (("running", "🟢"), ("down", "🔴"), ("still_down", "🟠")):
        html = _render_html(kind, "title", rows, "note text")
        text = _render_text("title", rows, "note text")
        assert emoji in html and "title" in html
        assert "LLM Mailroom watcher status" in html
        assert "PID" in html and "123" in html
        assert "note text" in html
        assert "PID: 123" in text and "note text" in text


@pytest.fixture
def status_env(monkeypatch):
    monkeypatch.setenv("MAILROOM_WATCHER_STATUS", "1")
    monkeypatch.setenv("MAILROOM_STATUS_EMAIL", "human@example.com")
    monkeypatch.setenv("GMAIL_ADDRESS", "mailroom@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    yield


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self):
        self.logins: list[str] = []
        self.sent: list[tuple[str, list[str], bytes]] = []
        _FakeSMTP.instances.append(self)

    def login(self, user, _pw):
        self.logins.append(user)

    def sendmail(self, from_addr, to_addrs, payload):
        self.sent.append((from_addr, list(to_addrs), payload))

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_smtp_fakes():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def _parsed(payload: bytes):
    import email.parser
    import email.policy

    return email.parser.BytesParser(policy=email.policy.default).parsebytes(payload)


def _decoded_subject(payload: bytes) -> str:
    return str(_parsed(payload)["Subject"])


def test_send_status_email_via_injected_factory(status_env, mocker):
    import email

    from pipeline.status_notify import send_status_email, set_status_smtp_factory

    set_status_smtp_factory(_FakeSMTP)
    try:
        assert send_status_email("running", "watcher is up", [("PID", "42")]) is True
    finally:
        set_status_smtp_factory(None)
    fake = _FakeSMTP.instances[-1]
    assert fake.logins == ["mailroom@example.com"]
    (from_addr, to_addrs, payload) = fake.sent[-1]
    assert from_addr == "mailroom@example.com"
    assert to_addrs == ["human@example.com"]
    msg = _parsed(payload)
    assert "🟢" in _decoded_subject(payload)
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    assert "🟢" in html_part.get_content()


def test_send_status_email_disabled_by_kill_switch(status_env, mocker, monkeypatch):
    from pipeline.status_notify import send_status_email, set_status_smtp_factory

    monkeypatch.setenv("MAILROOM_WATCHER_STATUS", "0")
    monkeyed = mocker.patch("pipeline.status_notify._smtp_config")
    monkeyed.side_effect = AssertionError("config must not load while disabled")
    set_status_smtp_factory(_FakeSMTP)
    try:
        assert send_status_email("down", "down", [("A", "b")]) is False
    finally:
        set_status_smtp_factory(None)
    assert not any(f.sent for f in _FakeSMTP.instances)


def test_send_status_email_skips_without_creds(monkeypatch, mocker):
    from pipeline.status_notify import send_status_email, set_status_smtp_factory

    monkeypatch.setenv("MAILROOM_WATCHER_STATUS", "1")
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    set_status_smtp_factory(_FakeSMTP)
    try:
        assert send_status_email("down", "down", [("A", "b")]) is False
    finally:
        set_status_smtp_factory(None)
    assert not any(f.sent for f in _FakeSMTP.instances)


def test_send_status_email_failure_is_soft(status_env):
    from pipeline.status_notify import send_status_email, set_status_smtp_factory

    def _boom():
        raise RuntimeError("smtp down")

    set_status_smtp_factory(_boom)
    try:
        assert send_status_email("down", "down", [("A", "b")]) is False
    finally:
        set_status_smtp_factory(None)


# ── enriched heartbeat ───────────────────────────────────────────────


def test_heartbeat_enrichment_roundtrip():
    from pipeline.watchdog import read_heartbeat

    touch_watcher_heartbeat(extra={"pid": 424242, "host": "test-host", "sha": "abc1234"})
    hb = read_heartbeat()
    assert hb is not None
    assert hb["pid"] == 424242
    assert hb["host"] == "test-host"
    assert hb["sha"] == "abc1234"
    assert watcher_heartbeat_age() is not None and watcher_heartbeat_age() < 5.0


# ── watchdog transition core ─────────────────────────────────────────


@pytest.fixture
def watchdog_core(mocker):
    """Import the watchdog with the heartbeat age under test control."""
    import pipeline.watchdog as wd

    age_holder = {"age": 0.0}
    mocker.patch.object(wd, "watcher_heartbeat_age", lambda: age_holder["age"])
    return wd, age_holder


def test_watchdog_fresh_is_silent(watchdog_core):
    wd, holder = watchdog_core
    holder["age"] = 1.0
    action, state = wd.evaluate({"pid": 1}, True, {}, 1000.0, stale_s=30.0, remind_min=60.0)
    assert action == "none"
    assert state["down_alerted"] is False


def test_watchdog_pid_gone_alerts_once_then_reminds(watchdog_core):
    wd, holder = watchdog_core
    holder["age"] = 120.0  # stale
    state: dict = {}
    action, state = wd.evaluate({"pid": 7}, False, state, 1000.0, stale_s=30.0, remind_min=60.0)
    assert action == "down"
    # Still down, inside the reminder window → quiet (no spam).
    action, state = wd.evaluate({"pid": 7}, False, state, 1100.0, stale_s=30.0, remind_min=60.0)
    assert action == "none"
    # Past the reminder window → one 🟠 reminder.
    action, state = wd.evaluate({"pid": 7}, False, state, 1000.0 + 61 * 60, stale_s=30.0, remind_min=60.0)
    assert action == "remind"


def test_watchdog_hung_ladder_two_stale_checks(watchdog_core):
    wd, holder = watchdog_core
    holder["age"] = 120.0  # stale, pid alive (sleep/hang guard)
    state: dict = {}
    action, state = wd.evaluate({"pid": 7}, True, state, 1000.0, stale_s=30.0, remind_min=60.0)
    assert action == "none"  # first stale check: give the sleep/wake benefit
    action, state = wd.evaluate({"pid": 7}, True, state, 1020.0, stale_s=30.0, remind_min=60.0)
    assert action == "down"  # second consecutive stale check: hung


def test_watchdog_missing_heartbeat_alerts_immediately(watchdog_core):
    wd, holder = watchdog_core
    holder["age"] = 0.0
    holder["age"] = 9999.0
    action, state = wd.evaluate(None, None, {}, 1000.0, stale_s=30.0, remind_min=60.0)
    assert action == "down"


def test_watchdog_recovery_resets_quietly(watchdog_core):
    wd, holder = watchdog_core
    holder["age"] = 120.0
    action, state = wd.evaluate({"pid": 7}, False, {}, 1000.0, stale_s=30.0, remind_min=60.0)
    assert action == "down"
    holder["age"] = 1.0  # watcher came back (🟢 comes from the watcher itself)
    action, state = wd.evaluate({"pid": 7}, True, state, 1010.0, stale_s=30.0, remind_min=60.0)
    assert action == "none"
    assert state["down_alerted"] is False
    # A NEW outage later alerts again (state genuinely reset).
    holder["age"] = 120.0
    action, state = wd.evaluate({"pid": 7}, False, state, 2000.0, stale_s=30.0, remind_min=60.0)
    assert action == "down"


def test_watchdog_retries_down_alert_after_send_failure(
    watchdog_core, status_env, mocker, monkeypatch
):
    """A failed 🔴 send must NOT latch the reminder cadence: the next poll
    re-fires the DOWN alert (audit fix 2026-09-04 — an SMTP blip used to
    silence the human for a full remind cycle)."""
    wd, holder = watchdog_core
    holder["age"] = 120.0  # stale from the first poll onward
    monkeypatch.setenv("MAILROOM_WATCHDOG", "1")

    clock = {"now": 1000.0}
    mocker.patch.object(wd.time, "time", lambda: clock["now"])
    kinds: list[str] = []

    def _send(kind, *a, **k):
        kinds.append(kind)
        if len(kinds) == 1:
            return False  # first 🔴 send fails (SMTP blip)
        if len(kinds) == 2:
            clock["now"] += 60 * 60.0  # jump past the reminder cadence
        return True

    sends = mocker.patch("pipeline.status_notify.send_status_email", side_effect=_send)
    mocker.patch.object(wd, "read_heartbeat", lambda: {"pid": 999999})
    sleeps = mocker.patch.object(wd.time, "sleep", side_effect=[None, None, KeyboardInterrupt])

    with pytest.raises(KeyboardInterrupt):
        wd.main()

    # first send fails → latch reverts → the retry is a 🔴 DOWN again;
    # only after a SUCCESSFUL send does the ladder advance to 🟠.
    assert kinds == ["down", "down", "still_down"]
    assert sends.call_count == 3
    assert sleeps.call_count == 3


# ── 🟢 startup/relaunch confirmation ────────────────────────────────


def test_watcher_startup_notice_sends_email(status_env, mocker, monkeypatch):
    import email

    import pipeline.watcher as watcher
    from pipeline.status_notify import set_status_smtp_factory

    monkeypatch.setattr(watcher, "_STATUS_STARTED_AT", "2026-09-04 06:00:00 UTC", raising=False)
    mocker.patch.object(watcher, "_code_sha", lambda: "abc1234")
    mocker.patch("time.sleep", lambda *_: None)
    set_status_smtp_factory(_FakeSMTP)
    try:
        watcher._send_startup_notice()
    finally:
        set_status_smtp_factory(None)
    fake = _FakeSMTP.instances[-1]
    (_from, _to, payload) = fake.sent[-1]
    msg = _parsed(payload)
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    body = html_part.get_content()
    assert "🟢" in _decoded_subject(payload)
    assert "watcher is running" in body
    assert "Heartbeat" in body


def test_watcher_start_spawns_heartbeat_and_notice(status_env, mocker):
    """start() enriches the first heartbeat and fires the notice thread
    (fail-soft); the lock/observer machinery is stubbed out here."""
    import pipeline.watcher as watcher
    from pipeline.bins import get_base_dir

    mocker.patch.object(watcher, "acquire_watcher_lock", lambda: mocker.MagicMock())
    watcher_instance = watcher.Watcher()
    watcher_instance.observer = mocker.MagicMock()
    mocker.patch.object(watcher, "ensure_dirs", lambda *_: None)
    mocker.patch.object(watcher, "inbox_dir", lambda: get_base_dir() / "inbox")
    mocker.patch.object(watcher, "list_inbox_files", lambda: [])
    mocker.patch.object(watcher.Watcher, "_reconcile_stale_claims", lambda self: None)
    mocker.patch("pipeline.watcher.InboxHandler", create=True)
    mocker.patch.object(watcher_instance.observer, "schedule", lambda *a, **k: None)
    mocker.patch("pipeline.gmail_intake.start_embedded_poller", lambda: None)
    mocker.patch("pipeline.relations.start_embedded_relations_scanner", lambda: None)

    watcher_instance.start()
    try:
        hb = json.loads((get_base_dir() / "watcher_heartbeat").read_text())
        assert hb["pid"] == os.getpid()
        assert hb["host"]
        assert "started_at" in hb
    finally:
        watcher_instance.observer.stop = lambda *_: None
        watcher_instance._running = False
        watcher_instance._lock = None
        watcher._watcher_owned = False
