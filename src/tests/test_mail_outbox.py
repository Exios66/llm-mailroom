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


def test_live_lease_blocks_second_drainer_and_expired_lease_is_retried():
    _q()
    assert mail_outbox._claim("k", 0) is True  # "other process" claimed first
    smtp = FakeSMTP()
    assert mail_outbox.drain_once(lambda: smtp, now=10)["sent"] == 0
    assert smtp.sent == []
    # lease expired (crash between claim/send and mark): retried exactly once
    assert mail_outbox.drain_once(lambda: smtp, now=mail_outbox.LEASE_SECONDS + 10)["sent"] == 1
    assert mail_outbox.drain_once(lambda: smtp, now=10**6)["sent"] == 0
    assert len(smtp.sent) == 1


def test_interleaved_claims_send_once():
    _q()
    smtp = FakeSMTP()
    orig = mail_outbox._claim
    other = []

    def claim_then_race(key, now):
        ok = orig(key, now)
        other.append(orig(key, now))  # second drainer loses the claim
        return ok

    import pytest as _p

    mp = _p.MonkeyPatch()
    mp.setattr(mail_outbox, "_claim", claim_then_race)
    try:
        assert mail_outbox.drain_once(lambda: smtp, now=5)["sent"] == 1
    finally:
        mp.undo()
    assert other == [False]
    assert len(smtp.sent) == 1


def test_existing_db_without_lease_column_is_migrated():
    import sqlite3

    path = mail_outbox.db_path()
    c = sqlite3.connect(str(path))
    c.execute(
        "CREATE TABLE outbox (dedup_key TEXT PRIMARY KEY, to_addr TEXT NOT NULL, subject TEXT NOT NULL,"
        " headers_json TEXT NOT NULL DEFAULT '{}', text TEXT NOT NULL, html TEXT, state TEXT NOT NULL"
        " DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,"
        " last_error TEXT, created_at REAL NOT NULL, sent_at REAL)"
    )
    c.execute("INSERT INTO outbox (dedup_key,to_addr,subject,text,created_at) VALUES ('old','a@b.c','s','t',1)")
    c.commit()
    c.close()
    smtp = FakeSMTP()
    assert mail_outbox.drain_once(lambda: smtp)["sent"] == 1
    assert mail_outbox.row_state("old") == "sent"


def test_mark_sent_db_error_does_not_abort_batch_or_resend(monkeypatch):
    _q("a")
    _q("b")
    smtp = FakeSMTP()

    def bad(*a, **k):
        raise sqlite3_error()

    import sqlite3

    def sqlite3_error():
        return sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mail_outbox, "_mark_sent", bad)
    r = mail_outbox.drain_once(lambda: smtp, now=1)
    assert r["sent"] == 2 and len(smtp.sent) == 2
    # same-drain-window re-drain must not resend (rows hold a live lease)
    assert mail_outbox.drain_once(lambda: smtp, now=2)["sent"] == 0
    assert len(smtp.sent) == 2


def test_dead_row_logged_and_counted(monkeypatch):
    import structlog

    _q()
    bad = FakeSMTP(fail=True)
    now = 0.0
    with structlog.testing.capture_logs() as logs:
        # A fresh proxy binds under capture_logs even when an earlier test ran
        # setup_logging() (cache_logger_on_first_use pins the module logger).
        monkeypatch.setattr(mail_outbox, "logger", structlog.get_logger(mail_outbox.__name__))
        for _ in range(mail_outbox.MAX_ATTEMPTS):
            mail_outbox.drain_once(lambda: bad, now=now)
            now += 4000
    assert any(l.get("event") == "gmail_echo_dead" for l in logs)


def test_connect_failure_not_retried_per_row():
    for i in range(3):
        _q(f"k{i}")
    n = {"c": 0}

    def factory():
        n["c"] += 1
        raise OSError("down")

    assert mail_outbox.drain_once(factory)["failed"] == 3
    assert n["c"] == 1


def test_worker_works_off_backlog_larger_than_limit(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    smtp = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: smtp)
    for i in range(25):
        _q(f"k{i}")
    w = mail_outbox.OutboxWorker(poll_seconds=30)
    w.start()
    try:
        w.wake()
        import time

        for _ in range(200):
            if len(smtp.sent) == 25:
                break
            time.sleep(0.05)
        assert len(smtp.sent) == 25
    finally:
        w.stop()
        gmail_intake.set_smtp_factory(None)


def test_start_worker_is_idempotent(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    w1 = mail_outbox.start_outbox_worker()
    try:
        assert w1 is not None
        assert mail_outbox.start_outbox_worker() is w1
    finally:
        mail_outbox.stop_outbox_worker(w1)


def test_message_id_domain_fallback_without_at(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setattr(gmail_intake, "load_config", lambda: {"address": "noatsign", "password": "x"})
    monkeypatch.setattr(gmail_intake, "_load_audit_rows", lambda d: ([], None))
    m = {"doc_id": "d1", "stage": "archived", "intake": {"source": "gmail", "message_id": "<m@x>", "sender": "a@b.c"}}
    gmail_intake._enqueue_echo(m)
    import sqlite3

    c = sqlite3.connect(str(mail_outbox.db_path()))
    h = c.execute("SELECT headers_json FROM outbox").fetchone()[0]
    c.close()
    assert "@mailroom.local>" in h
