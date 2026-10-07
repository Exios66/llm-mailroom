"""Durable mail outbox — network-free tests (FakeSMTP seam, temp base dir)."""

import threading

import pytest

from pipeline import gmail_intake, mail_outbox


class FakeSMTP:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def login(self, user, password):
        pass

    def sendmail(self, frm, to, raw):
        if self.fail:
            raise OSError("boom")
        self.sent.append((frm, to, raw))

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _cfg(temp_base_dir, monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")


def _q(key="k", to="a@example.com"):
    return mail_outbox.enqueue(key, to_addr=to, subject="s", text="t", html="<p>t</p>", headers={"In-Reply-To": "<x@y>"})


def test_enqueue_is_idempotent_across_restart():
    assert _q() is True
    assert _q() is False
    smtp = FakeSMTP()
    r = mail_outbox.drain_once(lambda: smtp)
    assert r == {"sent": 1, "failed": 0, "dead": 0}
    assert len(smtp.sent) == 1
    assert _q() is False  # still False once sent
    assert mail_outbox.drain_once(lambda: smtp)["sent"] == 0
    assert len(smtp.sent) == 1


def test_failed_send_retried_after_backoff():
    _q()
    calls = {"n": 0}
    good = FakeSMTP()

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connect refused")
        return good

    assert mail_outbox.drain_once(factory, now=0)["failed"] == 1
    assert mail_outbox.drain_once(factory, now=10)["sent"] == 0
    assert mail_outbox.drain_once(factory, now=61)["sent"] == 1
    assert mail_outbox.row_state("k") == "sent"


def test_row_goes_dead_after_max_attempts_and_is_never_revived():
    _q()
    bad = FakeSMTP(fail=True)
    now, last = 0.0, None
    for _ in range(mail_outbox.MAX_ATTEMPTS):
        last = mail_outbox.drain_once(lambda: bad, now=now)
        now += 4000
    assert last["dead"] == 1
    assert mail_outbox.row_state("k") == "dead"
    good = FakeSMTP()
    assert mail_outbox.drain_once(lambda: good, now=now + 10**6)["sent"] == 0
    assert good.sent == []
    assert _q() is False  # dead row is not revived by enqueue


def test_one_smtp_connection_per_drain():
    for i in range(3):
        _q(f"k{i}")
    n = {"c": 0}
    smtp = FakeSMTP()

    def factory():
        n["c"] += 1
        return smtp

    assert mail_outbox.drain_once(factory)["sent"] == 3
    assert n["c"] == 1


def test_count_recent_windows_by_recipient():
    _q("a", "x@example.com")
    _q("b", "x@example.com")
    _q("c", "y@example.com")
    assert mail_outbox.count_recent("x@example.com", 60) == 2
    assert mail_outbox.count_recent("x@example.com", 60, now=10**10) == 0


def test_drain_never_raises(monkeypatch):
    _q()
    monkeypatch.setattr(mail_outbox, "_drain_locked", lambda *a, **k: 1 / 0)
    assert mail_outbox.drain_once()["failed"] == 1


def test_worker_drains_on_wake_and_stops(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    smtp = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: smtp)
    w = mail_outbox.OutboxWorker(poll_seconds=30)
    w.start()
    try:
        _q()
        done = threading.Event()
        orig = mail_outbox.drain_once

        def spy(*a, **k):
            r = orig(*a, **k)
            done.set()
            return r

        monkeypatch.setattr(mail_outbox, "drain_once", spy)
        w.wake()
        assert done.wait(10)
        assert len(smtp.sent) == 1
    finally:
        w.stop()
        gmail_intake.set_smtp_factory(None)
    assert not w.is_alive()


def test_start_worker_noop_when_channel_disabled(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "0")
    assert mail_outbox.start_outbox_worker() is None
