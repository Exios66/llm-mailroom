"""Gmail intake channel (HUB-037) — network-free tests.

Every test drives ``poll_once`` through a fake IMAP client; no socket is
ever opened. The channel is opt-in (``MAILROOM_GMAIL_ENABLED=1``) and
conftest keeps it disabled for the whole suite, so a production ``.env``
with real credentials can never leak network polls into tests.
"""

import email.message
import errno
import imaplib
import os
import stat
import time

import pytest

from pipeline import gmail_intake


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n% fake attachment for tests\n"


def _message(
    subject: str,
    sender: str = "sender@example.com",
    attachments=(),
    message_id: str = "<msg-1@example.com>",
    extra_headers=None,
    auth_results: str | None = "mx.google.com; dkim=pass; spf=pass; dmarc=pass",
) -> bytes:
    msg = email.message.EmailMessage()
    if auth_results is not None:
        msg["Authentication-Results"] = auth_results
    msg["From"] = sender
    msg["To"] = "llmmailroom@gmail.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 01 Sep 2026 12:00:00 +0000"
    for name, value in dict(extra_headers or {}).items():
        msg[name] = value
    msg.set_content("please process the attached documents")
    for filename, payload in dict(attachments).items():
        subtype = "pdf" if filename.endswith(".pdf") else "octet-stream"
        msg.add_attachment(
            payload, maintype="application", subtype=subtype, filename=filename
        )
    return msg.as_bytes()


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in (uid commands only, as used by the poller)."""

    def __init__(
        self,
        messages: dict[str, bytes],
        ignore_store: bool = False,
        structures: dict[str, bytes] | None = None,
    ):
        self._messages = messages
        self._structures = structures or {}
        self.fetch_specs: list[str] = []
        self._ignore_store = ignore_store
        self.seen: set[str] = set()
        self.labels: dict[str, list[str]] = {}
        self.store_calls = 0
        self.logged_in = False
        self.selected = None
        self.uidvalidity = 1
        self.label_store_status = "OK"

    def login(self, user, password):
        self.logged_in = True

    def enable(self, capability):
        self.enabled = capability
        return ("OK", [b"ENABLED"])

    def select(self, folder, readonly=False):
        self.selected = folder
        return ("OK", [b"1"])

    def response(self, code):
        if code == "UIDVALIDITY":
            return (code, [str(self.uidvalidity).encode()])
        return (code, [None])

    def uid(self, command, *args):
        command = command.upper()  # real imaplib does the same
        if command == "SEARCH":
            criteria = " ".join(
                a.decode("utf-8", "ignore") if isinstance(a, bytes) else str(a)
                for a in args
                if a is not None
            )
            if "Message-ID" in criteria:
                mid = criteria.split("Message-ID", 1)[1].strip().strip('"')
                matches = [
                    u
                    for u, raw in self._messages.items()
                    if mid in raw.decode("utf-8", "ignore")
                ]
                return ("OK", [" ".join(matches).encode()])
            unseen = " ".join(u for u in self._messages if u not in self.seen)
            return ("OK", [unseen.encode()])
        if command == "FETCH":
            uid = self._arg(args[0])
            spec = self._arg(args[1]).upper() if len(args) > 1 else ""
            self.fetch_specs.append(spec)
            if spec == "(BODYSTRUCTURE)":
                if uid in self._structures:
                    return ("OK", [self._structures[uid]])
                return ("OK", [None])  # unsupported → poller falls back to a full fetch
            data = self._messages.get(uid)
            if data is None:
                return ("OK", [None])
            if spec == "(BODY.PEEK[HEADER])":
                data = data.split(b"\n\n", 1)[0] + b"\n\n"
            header = b"1 (UID " + uid.encode() + b" RFC822 {" + str(len(data)).encode() + b"}"
            return ("OK", [(header, data), b")"])
        if command == "STORE":
            uid = self._arg(args[0])
            self.store_calls += 1
            flags = " ".join(
                a.decode("utf-8", "ignore") if isinstance(a, bytes) else str(a)
                for a in args[1:]
            )
            if "X-GM-LABELS" in flags:
                # Gmail reaction: emoji label arrives as pre-encoded UTF-8 bytes.
                if self.label_store_status != "OK":
                    return (self.label_store_status, [b"denied"])
                self.labels.setdefault(uid, []).append(
                    flags.split("X-GM-LABELS", 1)[1].strip()
                )
            elif not self._ignore_store:
                self.seen.add(uid)
            return ("OK", [b"OK"])
        raise AssertionError(f"unexpected imap command {command!r}")

    @staticmethod
    def _arg(value) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    def logout(self):
        pass


def _cfg(**overrides) -> dict:
    base = {
        "address": "llmmailroom@gmail.com",
        "password": "apppassword1234",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "folder": "INBOX",
        "poll_seconds": 60.0,
        "default_matter_id": "DEFAULT",
        "allowed_senders": set(),
        "max_attachment_bytes": 50 * 1024 * 1024,
    }
    base.update(overrides)
    return base


# ── gating ────────────────────────────────────────────────────────────────


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAILROOM_GMAIL_ENABLED", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert gmail_intake.gmail_intake_enabled() is False
    assert gmail_intake.start_embedded_poller() is None


def test_enabled_requires_credentials(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert gmail_intake.gmail_intake_enabled() is False


def test_enabled_with_credentials(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "axfo qgzf osej wrqd")
    assert gmail_intake.gmail_intake_enabled() is True
    assert gmail_intake.gmail_app_password() == "axfoqgzfosejwrqd"


# ── matter routing ────────────────────────────────────────────────────────


def test_parse_matter_id_tag():
    assert gmail_intake.parse_matter_id("Invoice scan [M:Smith-001] urgent") == "Smith-001"
    assert gmail_intake.parse_matter_id("no tag here") is None
    assert gmail_intake.parse_matter_id(None) is None


def test_attachment_delivered_with_meta_sidecar(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "Contract bundle [M:MATTER-7]",
                attachments=({"contract.pdf": _pdf_bytes()}),
            )
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["connected"] is True
    assert report["attachments_queued"] == 1
    assert report["messages_seen"] == 1
    assert report["marked_seen"] == 1
    assert report["errors"] == []

    from pipeline.bins import inbox_dir, read_inbox_meta

    delivered = list(inbox_dir().glob("*.pdf"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == _pdf_bytes()
    meta = read_inbox_meta(delivered[0])
    assert meta["matter_id"] == "MATTER-7"
    assert meta["source"] == "gmail"
    assert meta["message_id"] == "<msg-1@example.com>"
    assert meta["sender"] == "sender@example.com"
    assert meta["original_filename"] == "contract.pdf"


def test_default_matter_id_without_subject_tag(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("no tag", attachments=({"doc.txt": b"hello"}), message_id="<msg-2@example.com>")}
    )
    report = gmail_intake.poll_once(config=_cfg(default_matter_id="LOBBY"), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1

    from pipeline.bins import inbox_dir, read_inbox_meta

    meta = read_inbox_meta(next(inbox_dir().glob("*.txt")))
    assert meta["matter_id"] == "LOBBY"


# ── guards ────────────────────────────────────────────────────────────────


def test_unaccepted_extension_skipped_but_seen(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("payload", attachments=({"malware.exe": b"MZ..."}), message_id="<msg-3@example.com>")}
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["skipped_extension"] == 1
    assert report["attachments_queued"] == 0
    assert report["marked_seen"] == 1  # never re-polled forever

    from pipeline.bins import list_inbox_files

    assert list_inbox_files() == []


def test_attachment_over_size_cap_skipped(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("big", attachments=({"big.pdf": b"x" * 4096}), message_id="<msg-4@example.com>")}
    )
    report = gmail_intake.poll_once(
        config=_cfg(max_attachment_bytes=1024), imap_factory=lambda: client
    )
    assert report["skipped_size"] == 1
    assert report["attachments_queued"] == 0


def test_sender_allowlist_rejects_others(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message("from stranger", sender="stranger@evil.example", attachments=({"c.pdf": _pdf_bytes()}), message_id="<msg-5@example.com>"),
            "2": _message("from client", sender="client@firm.example", attachments=({"c2.pdf": _pdf_bytes()}), message_id="<msg-6@example.com>"),
        }
    )
    report = gmail_intake.poll_once(
        config=_cfg(allowed_senders={"client@firm.example"}), imap_factory=lambda: client
    )
    assert report["skipped_sender"] == 1
    assert report["attachments_queued"] == 1


# ── dedup + idempotency ───────────────────────────────────────────────────


def test_state_dedup_prevents_double_queue_when_seen_mark_fails(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("docs", attachments=({"d.pdf": _pdf_bytes()}), message_id="<msg-7@example.com>")},
        ignore_store=True,  # \Seen marking silently lost
    )
    first = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert first["attachments_queued"] == 1

    second = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert second["already_processed"] == 1
    assert second["attachments_queued"] == 0

    from pipeline.bins import inbox_dir

    assert len(list(inbox_dir().glob("*.pdf"))) == 1


def test_same_name_attachments_uniquified(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message("first", attachments=({"scan.pdf": b"one"}), message_id="<msg-8@example.com>"),
            "2": _message("second", attachments=({"scan.pdf": b"two"}), message_id="<msg-9@example.com>"),
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 2

    from pipeline.bins import inbox_dir

    names = sorted(p.name for p in inbox_dir().glob("*.pdf"))
    assert names == ["scan-1.pdf", "scan.pdf"]


# ── single vs bundle routing (HUB-037) ───────────────────────────────────


def test_single_attachment_message_routed_to_triage(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "single doc",
                attachments=({"single.pdf": _pdf_bytes()}),
                message_id="<route-1@example.com>",
            )
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1

    from pipeline.bins import inbox_dir, read_inbox_meta

    meta = read_inbox_meta(next(inbox_dir().glob("*.pdf")))
    assert meta["route"] == "triage"  # one accepted attachment → free-triage lane


def test_multi_attachment_message_routed_to_pipeline(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "bundle",
                attachments=(
                    {"bundle_a.pdf": _pdf_bytes(), "bundle_b.pdf": _pdf_bytes()}
                ),
                message_id="<route-2@example.com>",
            )
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 2

    from pipeline.bins import inbox_dir, read_inbox_meta

    metas = [read_inbox_meta(p) for p in inbox_dir().glob("*.pdf")]
    assert len(metas) == 2
    assert all(m["route"] == "pipeline" for m in metas)  # 2+ → full paid pipeline


def test_rejected_attachment_does_not_count_for_routing(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "one accepted, one rejected",
                attachments=({"good.pdf": _pdf_bytes(), "bad.exe": b"MZ..."}),
                message_id="<route-3@example.com>",
            )
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1
    assert report["skipped_extension"] == 1

    from pipeline.bins import inbox_dir, read_inbox_meta

    meta = read_inbox_meta(next(inbox_dir().glob("*.pdf")))
    assert meta["route"] == "triage"  # only accepted attachments count


# ── status surface (what /health reports) ─────────────────────────────────


def test_status_snapshot_accumulates(temp_base_dir):
    before = gmail_intake.status()
    client = FakeIMAP(
        {"1": _message("docs", attachments=({"d.pdf": _pdf_bytes()}), message_id="<msg-10@example.com>")}
    )
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    after = gmail_intake.status()
    assert after["messages_seen"] == before["messages_seen"] + 1
    assert after["attachments_queued"] == before["attachments_queued"] + 1
    assert after["last_error"] is None
    assert "password" not in after  # never leak credentials via status


def test_poll_once_without_credentials_never_connects(temp_base_dir):
    report = gmail_intake.poll_once(config=_cfg(address="", password=""))
    assert report["connected"] is False
    assert report["errors"] == ["missing_credentials"]


# ── watcher → pipeline intake awareness (HUB-037) ─────────────────────────


def test_watcher_passes_intake_meta_and_source_to_pipeline(temp_base_dir, mocker):
    from pipeline.bins import inbox_dir, write_inbox_meta
    from pipeline.watcher import Watcher

    inbox_file = inbox_dir() / "fnol_smoke.txt"
    inbox_file.write_text("ACME INSURANCE COMPANY — FNOL")
    write_inbox_meta(
        inbox_file,
        source="gmail",
        matter_id="MATTER-9",
        message_id="<msg-watch@example.com>",
        sender="client@firm.example",
        subject="FNOL [M:MATTER-9]",
    )

    spy = mocker.patch("pipeline.watcher.run_pipeline", return_value={"doc_id": "d1"})
    Watcher()._process_existing(inbox_file)

    assert spy.call_count == 1
    args, kwargs = spy.call_args
    assert args[1] == "MATTER-9"  # matter routed from the sidecar
    assert kwargs["source"] == "gmail"
    assert kwargs["intake_meta"]["message_id"] == "<msg-watch@example.com>"
    assert kwargs["intake_meta"]["sender"] == "client@firm.example"


def test_intake_meta_defaults_upload_source_for_bare_sidecar(temp_base_dir):
    from pipeline.watcher import _intake_meta_from_sidecar

    # /upload sidecars carry no `source` key — the manifest still records the route.
    assert _intake_meta_from_sidecar({"upload_id": "abc"}) == {
        "upload_id": "abc",
        "source": "upload",
    }
    # Gmail sidecars pass their keys through; unknown sidecar keys never leak.
    assert _intake_meta_from_sidecar(
        {"source": "gmail", "message_id": "<x>", "secret_field": "nope"}
    ) == {"source": "gmail", "message_id": "<x>"}
    assert _intake_meta_from_sidecar(None) == {}


def test_finalize_aborted_carries_intake_meta(temp_base_dir):
    from pathlib import Path

    from graph.build_graph import _finalize_aborted
    from pipeline.bins import processing_dir, get_worker_id, load_manifest

    proc = processing_dir(get_worker_id())
    proc.mkdir(parents=True, exist_ok=True)
    stranded = proc / "aborted_fnol.txt"
    stranded.write_text("FNOL that will crash")

    _finalize_aborted(
        {
            "file_path": str(stranded),
            "original_filename": "aborted_fnol.txt",
            "matter_id": "MATTER-9",
            "intake_meta": {"source": "gmail", "message_id": "<msg-abort@example.com>"},
        },
        "watcher pipeline exception",
    )

    from pipeline.bins import manifests_dir
    import json

    manifests = [json.loads(m.read_text()) for m in manifests_dir().glob("*.json")]
    mine = [m for m in manifests if m.get("original_filename") == "aborted_fnol.txt"]
    assert mine and mine[0]["stage"] == "failed"
    assert mine[0]["intake"]["source"] == "gmail"
    assert mine[0]["intake"]["message_id"] == "<msg-abort@example.com>"


# ── check-emoji reaction at watcher claim time (HUB-037) ──────────────────


def test_react_to_message_applies_check_label(temp_base_dir):
    raw = _message(
        "docs",
        attachments=({"d.pdf": _pdf_bytes()}),
        message_id="<react-1@example.com>",
    )
    client = FakeIMAP({"7": raw})
    ok = gmail_intake.react_to_message(
        "<react-1@example.com>", config=_cfg(), imap_factory=lambda: client
    )
    assert ok is True
    assert client.labels["7"] == ['("&JwU-")']  # ✅ rides as RFC 3501 mUTF-7
    assert gmail_intake.status()["reactions_sent"] == 1


def test_react_dedups_per_message_within_and_across_claims(temp_base_dir):
    raw = _message(
        "docs",
        attachments=({"d.pdf": _pdf_bytes()}),
        message_id="<react-2@example.com>",
    )
    # One email, several attachments → several claims → ONE reaction.
    client = FakeIMAP({"1": raw})
    assert gmail_intake.react_to_message("<react-2@example.com>", config=_cfg(), imap_factory=lambda: client)
    assert gmail_intake.react_to_message("<react-2@example.com>", config=_cfg(), imap_factory=lambda: client)
    assert client.store_calls == 1  # second call short-circuited


def test_mutf7_encoding():
    assert gmail_intake._to_mutf7("✅") == b"&JwU-"
    assert gmail_intake._to_mutf7("✔ Done") == b"&JxQ- Done"
    assert gmail_intake._to_mutf7("ascii") == b"ascii"


def test_react_missing_message_returns_false_and_allows_retry(temp_base_dir):
    client = FakeIMAP({})
    ok = gmail_intake.react_to_message(
        "<react-3@example.com>", config=_cfg(), imap_factory=lambda: client
    )
    assert ok is False
    # A failed reaction is not marked attempted — a later claim retries.
    assert gmail_intake.react_to_message(
        "<react-3@example.com>", config=_cfg(), imap_factory=lambda: client
    ) is False
    assert client.store_calls == 0


def test_reactions_gate_and_label_config(monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "0")
    assert gmail_intake.reactions_enabled() is False
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "1")
    monkeypatch.setenv("MAILROOM_GMAIL_REACTION_LABEL", "✔ Done")
    assert gmail_intake.reactions_enabled() is True
    assert gmail_intake.reaction_label() == "✔ Done"
    monkeypatch.delenv("MAILROOM_GMAIL_REACTION_LABEL")
    assert gmail_intake.reaction_label() == "✅"


def test_watcher_claim_dispatches_reaction(temp_base_dir, mocker):
    from pipeline.watcher import _notify_intake_reaction

    spy = mocker.patch("pipeline.gmail_intake.react_to_message", return_value=True)
    _notify_intake_reaction(
        {"source": "gmail", "message_id": "<msg-react@example.com>"}, async_mode=False
    )
    spy.assert_called_once_with("<msg-react@example.com>")

    # Non-gmail sources and sidecars without a message id never react.
    spy.reset_mock()
    _notify_intake_reaction({"source": "upload", "upload_id": "x"}, async_mode=False)
    _notify_intake_reaction({"source": "gmail"}, async_mode=False)
    assert spy.call_count == 0


# ── completion echo on the source thread (HUB-037) ────────────────────────


class FakeSMTP:
    def __init__(self):
        self.sent = []
        self.logged_in = False

    def login(self, user, password):
        self.logged_in = True

    def sendmail(self, frm, to, raw):
        self.sent.append((frm, to, raw))

    def quit(self):
        pass


def _echo_manifest() -> dict:
    return {
        "doc_id": "d-echo-1",
        "matter_id": "M-1",
        "original_filename": "fnol.pdf",
        "stage": "archived",
        "doc_type": "insurance_claim",
        "doc_subclass": "other",
        "classification_confidence": 0.98,
        "extracted_data": {"insurer": "Acme", "confidence": 0.97, "_report": "Report text"},
        "intake": {
            "source": "gmail",
            "message_id": "<echo-1@example.com>",
            "sender": "client@firm.example",
            "subject": "FNOL [M:M-1]",
            "received_at": "2026-09-02T13:00:00-05:00",
        },
    }


def test_send_intake_echo_replies_on_source_thread(temp_base_dir, monkeypatch):
    import email as _email

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    # The terminal-stage reaction retry opens IMAP: inject a fake so no socket is touched.
    gmail_intake.set_imap_factory(lambda: FakeIMAP({}))
    try:
        ok = gmail_intake.send_intake_echo(_echo_manifest())
        assert ok is True
        assert len(fake.sent) == 1
        frm, to, raw = fake.sent[0]
        assert to == ["client@firm.example"]
        msg = _email.message_from_bytes(raw)
        assert msg["In-Reply-To"] == "<echo-1@example.com>"
        assert msg["Subject"] == "Re: FNOL [M:M-1]"
        # Multipart alternative: clean HTML + plain-text fallback.
        assert msg.is_multipart()
        parts = {p.get_content_type(): p for p in msg.walk()}
        assert "text/plain" in parts and "text/html" in parts
        body = parts["text/plain"].get_payload(decode=True).decode("utf-8")
        html = parts["text/html"].get_payload(decode=True).decode("utf-8")
        assert "STATUS: ARCHIVED" in body
        assert "d-echo-1" in body
        assert "insurance_claim" in body
        # Human directive: the closing message links back to mailroom-dev.
        assert "https://github.com/Exios66/mailroom-dev" in body
        assert "https://github.com/Exios66/mailroom-dev" in html
        assert "ARCHIVED" in html
        # Dedup: the same (doc, stage) echo is sent exactly once.
        assert gmail_intake.send_intake_echo(_echo_manifest()) is True
        assert len(fake.sent) == 1
    finally:
        gmail_intake.set_smtp_factory(None)
        gmail_intake.set_imap_factory(None)


def test_echo_not_resent_after_restart(temp_base_dir, monkeypatch, mocker):
    """The outbox row is durable: a fresh module state + new connections never re-send."""
    import importlib

    from pipeline import mail_outbox

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    mocker.patch("pipeline.gmail_intake.react_to_message", return_value=True)
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    try:
        assert gmail_intake.send_intake_echo(_echo_manifest()) is True
        importlib.reload(mail_outbox)  # restart: all module state (locks, worker) is fresh
        assert mail_outbox.row_state("echo:d-echo-1:archived") == "sent"
        assert gmail_intake.send_intake_echo(_echo_manifest()) is True
        assert len(fake.sent) == 1
    finally:
        gmail_intake.set_smtp_factory(None)


def test_send_intake_echo_false_for_dead_row(temp_base_dir, monkeypatch, mocker):
    import sqlite3

    from pipeline import mail_outbox

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    mocker.patch("pipeline.gmail_intake.react_to_message", return_value=True)
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    try:
        mail_outbox.enqueue("echo:d-echo-1:archived", to_addr="x@y.z", subject="s", text="t", html=None)
        c = sqlite3.connect(str(mail_outbox.db_path()))
        c.execute("UPDATE outbox SET state='dead'")
        c.commit()
        c.close()
        assert gmail_intake.send_intake_echo(_echo_manifest()) is False
        assert fake.sent == []
    finally:
        gmail_intake.set_smtp_factory(None)


def test_dispatch_returns_immediately_when_build_blocks(temp_base_dir, monkeypatch):
    import threading

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "0")
    gate, entered = threading.Event(), threading.Event()

    def slow(manifest):
        entered.set()
        gate.wait(10)

    monkeypatch.setattr(gmail_intake, "_enqueue_echo", slow)
    t0 = time.monotonic()
    gmail_intake.dispatch_intake_echo(_echo_manifest())
    assert time.monotonic() - t0 < 1.0
    assert entered.wait(5)
    gate.set()


def test_dispatch_build_failure_is_logged(temp_base_dir, monkeypatch):
    import threading

    import structlog

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "0")
    done = threading.Event()

    def boom(manifest):
        try:
            raise RuntimeError("build blew up")
        finally:
            done.set()

    monkeypatch.setattr(gmail_intake, "_enqueue_echo", boom)
    with structlog.testing.capture_logs() as logs:
        gmail_intake.dispatch_intake_echo(_echo_manifest())
        assert done.wait(5)
        for _ in range(100):
            if any(l.get("event") == "gmail_echo_build_failed" for l in logs):
                break
            time.sleep(0.05)
    assert any(l.get("event") == "gmail_echo_build_failed" for l in logs)


def test_dispatch_enqueues_and_wakes_worker(temp_base_dir, monkeypatch, mocker):
    import threading

    from pipeline import mail_outbox

    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "0")
    woke = threading.Event()

    class W:
        def wake(self):
            woke.set()

    monkeypatch.setattr(mail_outbox, "get_worker", lambda: W())
    gmail_intake.dispatch_intake_echo(_echo_manifest())
    assert woke.wait(10)
    assert mail_outbox.row_state("echo:d-echo-1:archived") == "pending"


def test_echo_skips_non_gmail_and_disabled_channel(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    try:
        # /upload document — no gmail provenance → no echo.
        m = _echo_manifest()
        m["intake"] = {"source": "upload", "upload_id": "x"}
        assert gmail_intake.send_intake_echo(m) is False
        # Channel master switch off → no echo even for gmail docs.
        monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "0")
        assert gmail_intake.send_intake_echo(_echo_manifest()) is False
        assert fake.sent == []
    finally:
        gmail_intake.set_smtp_factory(None)


def test_build_echo_body_renders_archive_entry_and_audit():
    import json as _json

    manifest = _echo_manifest()
    rows = [
        {
            "event": "ingested",
            "actor": "pipeline",
            "timestamp": "2026-09-02T18:00:00Z",
            "detail": _json.dumps({"file_sha256": "aa11", "size_bytes": 88345}),
        },
        {
            "event": "archived",
            "actor": "archivist",
            "timestamp": "2026-09-02T18:01:00Z",
            "detail": _json.dumps(
                {
                    "archive_path": "archive/M-1/insurance_claim/fnol.pdf",
                    "file_sha256": "bb22",
                    "size_bytes": 88345,
                }
            ),
        },
    ]
    body = gmail_intake.build_echo_body(manifest, rows, True)
    assert "STATUS: ARCHIVED" in body
    assert "archive/M-1/insurance_claim/fnol.pdf" in body
    assert "bb22" in body
    assert "hash chain intact" in body
    assert "ingested" in body
    assert "archived" in body

    # Failed terminal: shows the reason, no archive block.
    m2 = _echo_manifest()
    m2["stage"] = "failed"
    m2["escalation_reason"] = "llm_auth"
    body2 = gmail_intake.build_echo_body(m2, [], None)
    assert "STATUS: FAILED" in body2
    assert "llm_auth" in body2
    assert "audit chain unavailable" in body2


def test_friendly_reason_translates_guardrail_block():
    reason = (
        "sorter reviewer failed (RuntimeError: MAILROOM_LLM_FREE_ONLY is on: "
        "model 'qwen/qwen3.7-flash' is not free (cost_models prices non-zero, "
        "or unregistered without a ':free' suffix) — refusing to resolve an "
        "LLM client for it) — routing to human review"
    )
    plain, steps = gmail_intake.friendly_reason(reason)
    assert "free-only pilot guardrail" in plain
    assert "MAILROOM_LLM_FREE_ONLY" in steps
    assert gmail_intake.friendly_reason("some unknown reason") is None
    assert gmail_intake.friendly_reason("") is None


def test_build_echo_body_conveys_what_happened_on_review():
    """Human directive 2026-09-04: the closing message must explain the route,
    the WHY, and the next steps — never an opaque '(RuntimeError)'."""
    import json as _json

    m = _echo_manifest()
    m["stage"] = "review"
    m["escalation_reason"] = (
        "sorter reviewer failed (RuntimeError: MAILROOM_LLM_FREE_ONLY is on: "
        "model 'qwen/qwen3.7-flash' is not free) — routing to human review"
    )
    m["intake"]["triage_handoff"] = "exceeds_free_budget:69376>12000"
    rows = [
        {"event": "ingested", "actor": "pipeline", "timestamp": "2026-09-04T03:31:03+00:00", "detail": "{}"},
        {"event": "classified", "actor": "sorter", "timestamp": "2026-09-04T03:31:34+00:00", "detail": "{}"},
        {"event": "routed_to_review", "actor": "pipeline", "timestamp": "2026-09-04T03:32:01+00:00", "detail": "{}"},
    ]
    body = gmail_intake.build_echo_body(m, rows, True)
    assert "-- PROCESSING TIMELINE" in body
    assert "(+31s)" in body and "(+27s)" in body  # per-step durations
    assert "-- WHAT HAPPENED" in body
    assert "exceeds_free_budget:69376>12000" in body  # the route narrative
    assert "free-only pilot guardrail" in body  # the WHY
    assert "next steps:" in body and "REVIEW" in body  # the what-to-do
    # Raw cause stays visible for operators.
    assert "MAILROOM_LLM_FREE_ONLY is on" in body


def test_build_echo_html_renders_banner_and_link():
    m = _echo_manifest()
    html = gmail_intake.build_echo_html(m, [], True)
    assert "ARCHIVED" in html
    assert "https://github.com/Exios66/mailroom-dev" in html
    m2 = _echo_manifest()
    m2["stage"] = "review"
    m2["escalation_reason"] = "sorter reviewer failed (RuntimeError: MAILROOM_LLM_FREE_ONLY is on)"
    html2 = gmail_intake.build_echo_html(m2, [], True)
    assert "What happened" in html2
    assert "free-only pilot guardrail" in html2


# ── reaction guarantee at terminal stage (HUB-037) ───────────────────────


def test_echo_retries_failed_reaction_at_terminal(temp_base_dir, monkeypatch, mocker):
    """A single-document triage-lane claim has ONE reaction shot — if the
    claim-time attempt failed, the terminal echo retries it (deduped per
    Message-ID, so a successful reaction is never re-sent)."""
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    react_spy = mocker.patch("pipeline.gmail_intake.react_to_message", return_value=True)
    try:
        ok = gmail_intake.send_intake_echo(_echo_manifest())
        assert ok is True
        react_spy.assert_called_once_with("<echo-1@example.com>")
    finally:
        gmail_intake.set_smtp_factory(None)


def test_echo_skips_reaction_retry_when_reactions_disabled(temp_base_dir, monkeypatch, mocker):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    monkeypatch.setenv("MAILROOM_GMAIL_REACTIONS", "0")
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    react_spy = mocker.patch("pipeline.gmail_intake.react_to_message", return_value=True)
    try:
        ok = gmail_intake.send_intake_echo(_echo_manifest())
        assert ok is True
        react_spy.assert_not_called()
    finally:
        gmail_intake.set_smtp_factory(None)


def test_reaction_failure_tracks_reactions_failed_counter(temp_base_dir):
    client = FakeIMAP({})
    gmail_intake.react_to_message("<react-fail@example.com>", config=_cfg(), imap_factory=lambda: client)
    assert gmail_intake.status()["reactions_failed"] >= 1


# ── poison-message cap, same-fs staging, per-message state save ───────────


def _one_pdf_client(uid="1", message_id="<poison@example.com>"):
    return FakeIMAP(
        {uid: _message("docs", attachments=({"d.pdf": _pdf_bytes()}), message_id=message_id)}
    )


def _staging_dir():
    from pipeline.bins import inbox_dir

    d = inbox_dir()
    return d.with_name(d.name + ".staging")


class _Fatal(BaseException):
    """Escapes the per-message ``except Exception`` (simulates a hard crash)."""


def test_exdev_rename_falls_back_to_copy(temp_base_dir, monkeypatch):
    from pipeline.bins import inbox_dir

    inbox_prefix = str(inbox_dir())
    real_link, real_replace = os.link, os.replace
    raised = {"link": 0, "replace": 0}

    def _flaky(name, real):
        def wrapper(src, dst, *a, **kw):
            # Fail the first placement into the inbox only (the state-file
            # save also uses os.replace and must not be disturbed).
            if str(dst).startswith(inbox_prefix) and raised[name] == 0:
                raised[name] += 1
                raise OSError(errno.EXDEV, "x")
            return real(src, dst, *a, **kw)

        return wrapper

    monkeypatch.setattr(gmail_intake.os, "link", _flaky("link", real_link))
    monkeypatch.setattr(gmail_intake.os, "replace", _flaky("replace", real_replace))

    client = _one_pdf_client()
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)

    assert report["errors"] == []
    assert report["attachments_queued"] == 1
    assert [p.read_bytes() for p in inbox_dir().glob("*.pdf")] == [_pdf_bytes()]
    assert raised["link"] == 1
    assert list(inbox_dir().glob("*.part")) == []
    assert list(inbox_dir().glob(".*.part")) == []
    staging = _staging_dir()
    assert not staging.exists() or list(staging.glob("*")) == []


def test_message_that_raises_is_quarantined_after_cap(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "3")

    def boom(*a, **kw):
        raise RuntimeError("poison")

    monkeypatch.setattr(gmail_intake, "deliver_attachment", boom)
    client = _one_pdf_client()

    for _ in range(2):
        report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
        assert report["errors"]
        assert report["quarantined"] == 0
        assert "1" not in client.seen

    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["quarantined"] == 1
    assert "1" in client.seen
    assert any("mailroom/failed" in label for label in client.labels["1"])

    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["messages_seen"] == 0


def test_temp_file_removed_when_deliver_raises(temp_base_dir, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("poison")

    created = []
    real_stage = gmail_intake._stage_path

    def spy(ext):
        path = real_stage(ext)
        assert path.exists()
        created.append(path)
        return path

    monkeypatch.setattr(gmail_intake, "deliver_attachment", boom)
    monkeypatch.setattr(gmail_intake, "_stage_path", spy)
    client = _one_pdf_client()
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)

    assert len(created) == 1
    assert created[0].parent == _staging_dir()
    assert not created[0].exists()


def test_state_saved_per_message_survives_mid_sweep_crash(temp_base_dir, monkeypatch):
    client = FakeIMAP(
        {
            "1": _message("a", attachments=({"a.pdf": _pdf_bytes()}), message_id="<first@example.com>"),
            "2": _message("b", attachments=({"b.pdf": _pdf_bytes()}), message_id="<second@example.com>"),
        }
    )
    real_fetch = gmail_intake._fetch_message

    def fetch(c, uid):
        if uid == "2":
            raise _Fatal()
        return real_fetch(c, uid)

    monkeypatch.setattr(gmail_intake, "_fetch_message", fetch)
    with pytest.raises(_Fatal):
        gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)

    assert "<first@example.com>" in gmail_intake._load_state()["processed_message_ids"]


def test_safe_filename_neutralizes_traversal():
    name = gmail_intake._safe_filename("..\\..\\x" * 40)
    assert name is not None
    assert "\\" not in name
    assert ".." not in name
    assert len(name) <= 120


def test_exdev_copy_failure_leaves_no_partial_in_inbox(temp_base_dir, monkeypatch):
    from pipeline.bins import inbox_dir

    def exdev(*a, **kw):
        raise OSError(errno.EXDEV, "x")

    def broken_copy(fin, fout, *a, **kw):
        fout.write(b"partial")
        raise OSError(errno.EIO, "disk died mid-copy")

    monkeypatch.setattr(gmail_intake.os, "link", exdev)
    monkeypatch.setattr(gmail_intake.os, "replace", exdev)
    monkeypatch.setattr(gmail_intake.shutil, "copyfileobj", broken_copy)

    client = _one_pdf_client()
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)

    assert report["errors"]
    assert report["attachments_queued"] == 0
    leftovers = [p.name for p in inbox_dir().iterdir() if not p.name.endswith(".meta")]
    assert leftovers == []
    staging = _staging_dir()
    assert not staging.exists() or list(staging.glob("*")) == []


def test_missing_message_id_key_scoped_by_folder_and_uidvalidity(temp_base_dir):
    raw = _message("no id", attachments=({"d.pdf": _pdf_bytes()}))
    raw = b"\n".join(
        line for line in raw.split(b"\n") if not line.lower().startswith(b"message-id:")
    )
    assert b"Message-ID" not in raw
    client = FakeIMAP({"7": raw})
    client.uidvalidity = 4242
    report = gmail_intake.poll_once(config=_cfg(folder="Work"), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1
    ids = gmail_intake._load_state()["processed_message_ids"]
    assert ids == ["uid:Work:4242:7"]


# ── review fixes (round 1) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        imaplib.IMAP4.abort("socket error: EOF"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_transport_errors_abort_sweep_without_counting(temp_base_dir, monkeypatch, exc):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "2")
    fetched = []

    def fetch(c, uid):
        fetched.append(uid)
        raise exc

    monkeypatch.setattr(gmail_intake, "_fetch_message", fetch)
    client = FakeIMAP(
        {
            "1": _message("a", attachments=({"a.pdf": _pdf_bytes()}), message_id="<a@example.com>"),
            "2": _message("b", attachments=({"b.pdf": _pdf_bytes()}), message_id="<b@example.com>"),
        }
    )
    for _ in range(4):
        fetched.clear()
        report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
        assert report["quarantined"] == 0
        assert report["errors"]
        assert fetched == ["1"]  # sweep stopped at the first connection failure
    assert client.seen == set()
    assert gmail_intake._load_state()["failed_attempts"] == {}


def test_fetch_failed_counts_toward_cap(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(gmail_intake, "_fetch_message", lambda c, uid: None)
    client = _one_pdf_client()
    reports = [
        gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client) for _ in range(3)
    ]
    assert [r["quarantined"] for r in reports] == [0, 0, 1]
    assert "1" in client.seen
    assert any("quarantined:1" in e for e in reports[2]["errors"])


def test_placed_attachment_has_umask_governed_mode(temp_base_dir):
    from pipeline.bins import inbox_dir

    old = os.umask(0o022)
    try:
        client = _one_pdf_client()
        gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    finally:
        os.umask(old)
    (pdf,) = inbox_dir().glob("*.pdf")
    assert stat.S_IMODE(pdf.stat().st_mode) == 0o644


@pytest.mark.parametrize("err", [errno.EPERM, errno.ENOTSUP, errno.EMLINK, errno.ENOSYS])
def test_place_falls_back_to_copy_when_hardlinks_unsupported(temp_base_dir, monkeypatch, err):
    from pipeline.bins import inbox_dir

    def nolink(*a, **kw):
        raise OSError(err, "no hard links here")

    real_link = os.link
    calls = {"n": 0}

    def link(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            nolink()
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(gmail_intake.os, "link", link)
    client = _one_pdf_client()
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["errors"] == []
    assert [p.read_bytes() for p in inbox_dir().glob("*.pdf")] == [_pdf_bytes()]
    assert list(inbox_dir().glob(".*.part")) == []


def test_safe_filename_caps_utf8_bytes_and_keeps_extension():
    for stem in ("文" * 118, "😀" * 100, "é" * 150):
        name = gmail_intake._safe_filename(stem + ".pdf")
        assert name.endswith(".pdf")
        assert len(name.encode("utf-8")) <= 200
        name.encode("utf-8").decode("utf-8")  # whole code points only
    assert gmail_intake._safe_filename("a" * 300 + ".pdf").endswith(".pdf")


def test_safe_filename_never_returns_whitespace_only():
    assert gmail_intake._safe_filename(". .") is None
    assert gmail_intake._safe_filename(" .. a") == "a"
    assert gmail_intake._safe_filename("  a b .pdf ") == "a b .pdf"


def test_state_save_is_unique_tmp_and_fsynced(temp_base_dir, monkeypatch):
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(gmail_intake.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    gmail_intake._save_state({"processed_message_ids": ["<x>"], "failed_attempts": {}})
    assert synced
    path = gmail_intake._state_path()
    assert list(path.parent.glob("gmail_intake_state.json*")) == [path]
    assert gmail_intake._load_state()["processed_message_ids"] == ["<x>"]


def test_unreadable_state_logs_loudly(temp_base_dir):
    from structlog.testing import capture_logs

    path = gmail_intake._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{truncated")
    with capture_logs() as logs:
        state = gmail_intake._load_state()
    assert state["processed_message_ids"] == []
    assert any(e["event"] == "gmail_intake_state_unreadable" for e in logs)


def test_quarantine_label_failure_is_reported(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "1")
    monkeypatch.setattr(
        gmail_intake, "deliver_attachment", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("poison"))
    )
    client = _one_pdf_client()
    client.label_store_status = "NO"
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["quarantined"] == 1
    assert "quarantine_label_failed:1" in report["errors"]
    assert any(e.startswith("quarantined:1:RuntimeError") for e in report["errors"])


def test_stale_part_files_are_swept_at_poll_start(temp_base_dir):
    from pipeline.bins import inbox_dir

    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    inbox_dir().mkdir(parents=True, exist_ok=True)
    old_a, new_a = staging / "gmail-old.pdf.part", staging / "gmail-new.pdf.part"
    old_b, new_b = inbox_dir() / ".old.part", inbox_dir() / ".new.part"
    for f in (old_a, new_a, old_b, new_b):
        f.write_bytes(b"x")
    stale = time.time() - 2 * 3600
    for f in (old_a, old_b):
        os.utime(f, (stale, stale))
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: FakeIMAP({}))
    assert not old_a.exists() and not old_b.exists()
    assert new_a.exists() and new_b.exists()


def test_fallback_attempts_carry_over_to_message_id(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "3")
    real_fetch = gmail_intake._fetch_message
    state_calls = {"n": 0}

    def flaky_fetch(c, uid):
        state_calls["n"] += 1
        return None if state_calls["n"] == 1 else real_fetch(c, uid)

    monkeypatch.setattr(gmail_intake, "_fetch_message", flaky_fetch)
    monkeypatch.setattr(
        gmail_intake, "deliver_attachment", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("p"))
    )
    client = _one_pdf_client()
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)  # fetch_failed: fallback=1
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)  # carry 1 + 1 = 2 under Message-ID
    failed = gmail_intake._load_state()["failed_attempts"]
    assert failed == {"<poison@example.com>": 2}
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["quarantined"] == 1
    assert "<poison@example.com>" in gmail_intake._load_state()["processed_message_ids"]


def test_imap4_error_on_fetch_is_per_message_and_quarantines(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_MAX_ATTEMPTS", "3")

    def fetch(c, uid):
        raise imaplib.IMAP4.error("BAD")

    monkeypatch.setattr(gmail_intake, "_fetch_message", fetch)
    client = _one_pdf_client()
    reports = [
        gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client) for _ in range(3)
    ]
    assert [r["quarantined"] for r in reports] == [0, 0, 1]
    assert "1" in client.seen


def test_no_hardlink_filesystem_promotes_part_by_rename(temp_base_dir, monkeypatch):
    from pipeline.bins import inbox_dir

    def nolink(*a, **kw):
        raise OSError(errno.EPERM, "no hard links")

    monkeypatch.setattr(gmail_intake.os, "link", nolink)
    client = FakeIMAP(
        {
            "1": _message("a", attachments=({"d.pdf": _pdf_bytes()}), message_id="<a@example.com>"),
            "2": _message("b", attachments=({"d.pdf": _pdf_bytes() + b"2"}), message_id="<b@example.com>"),
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["errors"] == []
    assert report["attachments_queued"] == 2
    assert sorted(p.name for p in inbox_dir().glob("*.pdf")) == ["d-1.pdf", "d.pdf"]
    assert list(inbox_dir().glob(".*.part")) == []
    staging = _staging_dir()
    assert not staging.exists() or list(staging.glob("*")) == []


def test_already_processed_message_drops_carried_attempts(temp_base_dir):
    gmail_intake._save_state(
        {
            "processed_message_ids": ["<poison@example.com>"],
            "failed_attempts": {
                "<poison@example.com>": 1,
                "uid:INBOX:1:1": 1,
            },
        }
    )
    client = _one_pdf_client()
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["already_processed"] == 1
    assert gmail_intake._load_state()["failed_attempts"] == {}


# ── reject replies + two-phase delivery (hardening T4) ────────────────────


def _outbox_rows():
    import sqlite3

    from pipeline import mail_outbox

    if not mail_outbox.db_path().exists():
        return []
    conn = sqlite3.connect(str(mail_outbox.db_path()))
    try:
        return conn.execute(
            "SELECT dedup_key, to_addr, subject, text, html FROM outbox ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def test_unaccepted_extension_gets_reject_reply(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("Scan", attachments={"malware.exe": b"MZ"}, message_id="<rej-1@x>")}
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["reject_replies"] == 1
    assert "1" in client.seen
    rows = _outbox_rows()
    assert len(rows) == 1
    key, to, subject, text, html = rows[0]
    assert key == "reject:<rej-1@x>"
    assert to == "sender@example.com"
    assert subject == "Re: Scan"
    assert "malware.exe" in text and ".pdf" in text


def test_no_attachments_gets_reject_reply(temp_base_dir):
    client = FakeIMAP({"1": _message("hi", message_id="<rej-2@x>")})
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["reject_replies"] == 1
    (row,) = _outbox_rows()
    assert "no attachment" in row[3]


def test_non_allowlisted_sender_gets_no_reply(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("x", sender="stranger@evil.example", attachments={"a.exe": b"MZ"}, message_id="<rej-3@x>")}
    )
    report = gmail_intake.poll_once(
        config=_cfg(allowed_senders={"client@firm.example"}), imap_factory=lambda: client
    )
    assert report["skipped_sender"] == 1
    assert _outbox_rows() == []


def test_reject_reply_respects_hourly_budget(temp_base_dir):
    client = FakeIMAP({"1": _message("x", attachments={"a.exe": b"MZ"}, message_id="<rej-4@x>")})
    report = gmail_intake.poll_once(
        config=_cfg(max_replies_per_hour=0), imap_factory=lambda: client
    )
    assert report["reject_replies"] == 0
    assert _outbox_rows() == []
    assert "1" in client.seen


def test_reject_reply_html_escapes_filename():
    text, html = gmail_intake.build_reject_reply(
        "s", [("<script>x</script>.exe", "extension")], [".pdf"], 50
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_io_error_during_staging_publishes_nothing_and_retries(temp_base_dir, monkeypatch):
    from pathlib import Path

    from pipeline.bins import inbox_dir

    client = FakeIMAP(
        {"1": _message("two", attachments={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"}, message_id="<io-1@x>")}
    )
    real_write = Path.write_bytes
    calls = {"n": 0}

    def flaky_write(self, data):
        if self.name.endswith(".part"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write)
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 0
    assert list(inbox_dir().glob("*.pdf")) == [] if inbox_dir().exists() else True
    assert list(_staging_dir().glob("*")) == []
    assert "1" not in client.seen
    assert gmail_intake._load_state()["failed_attempts"]["<io-1@x>"] == 1

    monkeypatch.setattr(Path, "write_bytes", real_write)
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 2
    assert sorted(p.name for p in inbox_dir().glob("*.pdf")) == ["a.pdf", "b.pdf"]
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 0


def test_promote_failure_never_unlinks_published_file(temp_base_dir, monkeypatch):
    from pipeline.bins import inbox_dir

    client = FakeIMAP(
        {"1": _message("two", attachments={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"}, message_id="<pf-1@x>")}
    )
    real_place = gmail_intake._place
    calls = {"n": 0}

    def flaky_place(src, dest_dir, name, meta=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("promote boom")
        return real_place(src, dest_dir, name, meta)

    monkeypatch.setattr(gmail_intake, "_place", flaky_place)
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert [p.name for p in inbox_dir().glob("*.pdf")] == ["a.pdf"]
    assert report["attachments_queued"] == 1
    assert any(e.startswith("promote_failed:1:") and "b.pdf" in e for e in report["errors"])
    assert "1" in client.seen
    assert list(_staging_dir().glob("*")) == []

    monkeypatch.setattr(gmail_intake, "_place", real_place)
    client.seen.clear()
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["already_processed"] == 1
    assert len(list(inbox_dir().glob("*.pdf"))) == 1


def test_sidecar_written_before_file_is_visible(temp_base_dir, monkeypatch):
    from pipeline.bins import inbox_meta_path

    seen_meta = []
    real_link = gmail_intake.os.link

    def spy_link(src, dest):
        if not str(dest).endswith(".meta"):  # the sidecar itself is linked too
            seen_meta.append(inbox_meta_path(gmail_intake.Path(dest)).exists())
        return real_link(src, dest)

    monkeypatch.setattr(gmail_intake.os, "link", spy_link)
    client = FakeIMAP({"1": _message("one", attachments={"a.pdf": _pdf_bytes()}, message_id="<sc-1@x>")})
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1
    assert seen_meta == [True]


def test_existing_sidecar_is_never_overwritten(temp_base_dir):
    """An existing `<file>.meta` may belong to a claim in flight: take a new name."""
    from pipeline.bins import inbox_dir, read_inbox_meta

    inbox_dir().mkdir(parents=True, exist_ok=True)
    (inbox_dir() / "scan.pdf.meta").write_text('{"matter_id": "OLD"}')
    client = FakeIMAP({"1": _message("[M:NEW]", attachments={"scan.pdf": _pdf_bytes()}, message_id="<st-1@x>")})
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert read_inbox_meta(inbox_dir() / "scan.pdf")["matter_id"] == "OLD"
    assert not (inbox_dir() / "scan.pdf").exists()
    assert read_inbox_meta(inbox_dir() / "scan-1.pdf")["matter_id"] == "NEW"


def test_deliver_attachment_returns_io_on_oserror(temp_base_dir, monkeypatch):
    def boom(*a, **kw):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(gmail_intake, "_place", boom)
    delivered, reason = gmail_intake.deliver_attachment(
        "a.pdf", b"%PDF", {"_max_attachment_bytes": 1024}
    )
    assert (delivered, reason) == (None, "io")


# ── BODYSTRUCTURE pre-filter + PEEK fetch (hardening T6) ──────────────────


def _fixture_structure() -> bytes:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "gmail" / "bodystructure_two_attachments.txt").read_bytes()


def test_parse_bodystructure_gmail_format_sample():
    parts = gmail_intake.parse_bodystructure(_fixture_structure())
    named = [(p.name, p.size, p.maintype) for p in parts if p.name]
    assert named == [
        ("Master Services Agreement.pdf", 2873450, "application"),
        ("étiquette.png", 70140, "image"),
    ]
    assert [p.maintype for p in parts if not p.name] == ["text", "text"]
    pdf = parts[2]
    assert pdf.decoded_size == 2873450 * 57 // 78


def test_parse_bodystructure_literal_and_rfc2231_and_garbage():
    raw = (
        b'(UID 9 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("application" "pdf" NIL NIL NIL "base64" 400 NIL ("attachment" ("filename*" '
        b"\"utf-8''r%C3%A9sum%C3%A9.pdf\")) NIL NIL) \"mixed\" NIL NIL NIL))"
    )
    parts = gmail_intake.parse_bodystructure(raw)
    assert parts[1].name == "résumé.pdf"
    lit = b'(UID 9 BODYSTRUCTURE ("application" "pdf" ("name" {7}\r\nx y.pdf) NIL NIL "base64" 8 NIL NIL NIL NIL))'
    assert gmail_intake.parse_bodystructure(lit)[0].name == "x y.pdf"
    assert gmail_intake.parse_bodystructure(b"garbage (((") == []
    assert gmail_intake.parse_bodystructure(b"") == []


def test_oversize_only_message_never_downloads_body(temp_base_dir):
    structure = (
        b'1 (UID 1 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("application" "pdf" ("name" "big.pdf") NIL NIL "base64" 99999999 NIL '
        b'("attachment" ("filename" "big.pdf")) NIL NIL) "mixed" NIL NIL NIL))'
    )
    client = FakeIMAP(
        {"1": _message("Big", attachments={"big.pdf": b"%PDF"}, message_id="<big@x>")},
        structures={"1": structure},
    )
    report = gmail_intake.poll_once(config=_cfg(max_attachment_bytes=1024), imap_factory=lambda: client)
    assert "(BODY.PEEK[])" not in client.fetch_specs
    assert "(BODY.PEEK[HEADER])" in client.fetch_specs
    assert report["prefiltered"] == 1
    assert report["skipped_size"] == 1
    assert report["reject_replies"] == 1
    assert "1" in client.seen
    (row,) = _outbox_rows()
    assert row[0] == "reject:<big@x>" and "big.pdf" in row[3]


def test_acceptable_structure_downloads_body(temp_base_dir):
    structure = (
        b'1 (UID 1 BODYSTRUCTURE ("application" "pdf" ("name" "c.pdf") NIL NIL "base64" 40 NIL '
        b'("attachment" ("filename" "c.pdf")) NIL NIL))'
    )
    client = FakeIMAP(
        {"1": _message("ok", attachments={"c.pdf": _pdf_bytes()}, message_id="<ok@x>")},
        structures={"1": structure},
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert "(BODY.PEEK[])" in client.fetch_specs
    assert report["attachments_queued"] == 1


def test_fetch_uses_peek_and_does_not_mark_seen_until_handled(temp_base_dir, monkeypatch):
    client = FakeIMAP({"1": _message("x", attachments={"c.pdf": _pdf_bytes()}, message_id="<pk@x>")})

    def boom(*a, **kw):
        raise RuntimeError("deliver failed")

    monkeypatch.setattr(gmail_intake, "deliver_attachment", boom)
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert "(RFC822)" not in client.fetch_specs
    assert "(BODY.PEEK[])" in client.fetch_specs
    assert "1" not in client.seen


def test_unparseable_structure_falls_back_to_full_fetch(temp_base_dir):
    client = FakeIMAP(
        {"1": _message("x", attachments={"c.pdf": _pdf_bytes()}, message_id="<up@x>")},
        structures={"1": b"1 (UID 1 BODYSTRUCTURE (garbage"},
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert "(BODY.PEEK[])" in client.fetch_specs
    assert report["attachments_queued"] == 1
    assert report["prefiltered"] == 0


class _FakeSock:
    def __init__(self):
        self.timeout = 30.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class _IdleClient:
    def __init__(self, lines):
        self.lines = list(lines)
        self.sent = []
        self.sock = _FakeSock()

    def _new_tag(self):
        return b"A001"

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def socket(self):
        return self.sock


def test_idle_wait_returns_true_on_exists_push(monkeypatch):
    selects = []

    def fake_select(r, w, x, timeout):
        selects.append(timeout)
        return (r, [], [])

    monkeypatch.setattr(gmail_intake.select, "select", fake_select)
    client = _IdleClient([b"+ idling\r\n", b"* 5 EXISTS\r\n", b"A001 OK IDLE terminated\r\n"])
    assert gmail_intake.idle_wait(client, 60) is True
    assert client.sent == [b"A001 IDLE\r\n", b"DONE\r\n"]
    assert len(selects) == 1 and 0 < selects[0] <= 60
    assert client.sock.timeout == 30.0  # the socket timeout is never touched


def test_idle_wait_timeout_uses_select_and_still_reads_done(monkeypatch):
    monkeypatch.setattr(gmail_intake.select, "select", lambda r, w, x, t: ([], [], []))
    client = _IdleClient([b"+ idling\r\n", b"A001 OK done\r\n"])
    assert gmail_intake.idle_wait(client, 1) is False
    assert client.sent == [b"A001 IDLE\r\n", b"DONE\r\n"]
    assert client.lines == []  # tagged OK consumed through the same reader


def test_idle_wait_raises_when_refused():
    client = _IdleClient([b"A001 BAD unknown command\r\n"])
    with pytest.raises(gmail_intake.GmailIntakeError):
        gmail_intake.idle_wait(client, 5)


def test_poller_uses_idle_when_enabled_and_falls_back_on_error(monkeypatch):
    events = []
    poller = gmail_intake.GmailIntakePoller(poll_seconds=0.01, use_idle=True)
    monkeypatch.setattr(poller, "_open_idle_client", lambda: object())

    calls = {"n": 0}

    def fake_idle(client, timeout_s):
        calls["n"] += 1
        events.append("idle")
        if calls["n"] == 2:
            raise OSError("connection dropped")
        return True

    def fake_poll(**kw):
        events.append("poll")
        if events.count("poll") >= 4:
            poller.stop()
        return {}

    real_wait = poller._stop_event.wait

    def spy_wait(timeout=None):
        events.append("sleep")
        return real_wait(0)

    monkeypatch.setattr(gmail_intake, "idle_wait", fake_idle)
    monkeypatch.setattr(gmail_intake, "poll_once", fake_poll)
    monkeypatch.setattr(poller._stop_event, "wait", spy_wait)
    poller.run()
    assert events[:6] == ["poll", "idle", "poll", "idle", "sleep", "poll"]
    assert poller._idle_client is None  # closed on stop


def test_poller_without_idle_sleeps(monkeypatch):
    poller = gmail_intake.GmailIntakePoller(poll_seconds=0.01, use_idle=False)
    monkeypatch.setattr(gmail_intake, "idle_wait", lambda *a: pytest.fail("idle used while disabled"))
    n = {"polls": 0}

    def fake_poll(**kw):
        n["polls"] += 1
        if n["polls"] >= 2:
            poller.stop()
        return {}

    monkeypatch.setattr(gmail_intake, "poll_once", fake_poll)
    poller.run()
    assert n["polls"] == 2


# ── acknowledgment + bundle digest (hardening T7) ─────────────────────────


@pytest.fixture
def echo_env(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    gmail_intake.set_imap_factory(lambda: FakeIMAP({}))
    yield
    gmail_intake.set_imap_factory(None)


def _bundle_manifest(doc_id, message_id="<bundle@x>", group_size=3, stage="archived", **extra):
    m = {
        "doc_id": doc_id,
        "matter_id": "M-1",
        "original_filename": f"{doc_id}.pdf",
        "stage": stage,
        "doc_type": "contract",
        "classification_confidence": 0.9,
        "intake": {
            "source": "gmail",
            "message_id": message_id,
            "sender": "sender@example.com",
            "subject": "Bundle",
            "group_size": group_size,
        },
    }
    m.update(extra)
    return m


def _keys():
    return [r[0] for r in _outbox_rows()]


def test_single_doc_message_gets_ack_then_echo(echo_env):
    client = FakeIMAP({"1": _message("One", attachments={"a.pdf": _pdf_bytes()}, message_id="<one@x>")})
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["ack_replies"] == 1
    manifest = _bundle_manifest("d1", message_id="<one@x>", group_size=1)
    assert gmail_intake._enqueue_echo(manifest) == "echo:d1:archived"
    assert _keys() == ["ack:<one@x>", "echo:d1:archived"]
    ack_text = _outbox_rows()[0][3]
    assert "a.pdf" in ack_text and "triage" in ack_text


def test_three_doc_bundle_gets_one_ack_and_one_digest(echo_env):
    from pipeline import mail_outbox

    client = FakeIMAP(
        {"1": _message("Bundle", attachments={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b", "c.pdf": b"%PDF c"},
                       message_id="<bundle@x>")}
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 3 and report["ack_replies"] == 1
    assert mail_outbox.group_info("<bundle@x>")["expected"] == 3

    from pipeline.bins import inbox_dir, read_inbox_meta

    assert {read_inbox_meta(p)["group_size"] for p in inbox_dir().glob("*.pdf")} == {3}
    for doc in ("d1", "d2"):
        assert gmail_intake._enqueue_echo(_bundle_manifest(doc)) is None
    assert _keys() == ["ack:<bundle@x>"]
    assert gmail_intake._enqueue_echo(_bundle_manifest("d3")) == "digest:<bundle@x>"
    assert _keys() == ["ack:<bundle@x>", "digest:<bundle@x>"]
    digest_text = _outbox_rows()[1][3]
    assert all(f"d{i}.pdf" in digest_text for i in (1, 2, 3))
    assert "INCOMPLETE" not in digest_text


def test_stale_group_flushes_partial_digest(echo_env):
    import time as _time

    from pipeline import mail_outbox

    mail_outbox.register_group("<bundle@x>", expected=3, sender="sender@example.com", subject="Bundle")
    gmail_intake._enqueue_echo(_bundle_manifest("d1"))
    gmail_intake._enqueue_echo(_bundle_manifest("d2"))
    assert gmail_intake.flush_stale_digests(now=_time.time() + 60) == []
    assert gmail_intake.flush_stale_digests(now=_time.time() + 21601) == ["digest:<bundle@x>"]
    (row,) = [r for r in _outbox_rows() if r[0].startswith("digest:")]
    assert "INCOMPLETE: 2 of 3" in row[3]


def test_late_result_after_partial_digest_gets_own_echo(echo_env):
    import time as _time

    from pipeline import mail_outbox

    mail_outbox.register_group("<bundle@x>", expected=3, sender="sender@example.com", subject="Bundle")
    gmail_intake._enqueue_echo(_bundle_manifest("d1"))
    gmail_intake.flush_stale_digests(now=_time.time() + 21601)
    assert gmail_intake._enqueue_echo(_bundle_manifest("d3")) == "echo:d3:archived"
    # d1 was already in the digest: no extra echo for the same stage...
    assert gmail_intake._enqueue_echo(_bundle_manifest("d1")) is None
    # ...but a later stage for d1 is new information.
    assert gmail_intake._enqueue_echo(_bundle_manifest("d1", stage="review")) == "echo:d1:review"
    assert _keys().count("digest:<bundle@x>") == 1


def test_duplicate_terminal_manifest_is_idempotent(echo_env):
    from pipeline import mail_outbox

    mail_outbox.register_group("<bundle@x>", expected=2, sender="sender@example.com", subject="Bundle")
    gmail_intake._enqueue_echo(_bundle_manifest("d1", group_size=2))
    gmail_intake._enqueue_echo(_bundle_manifest("d1", group_size=2))
    assert mail_outbox.record_group_result("<bundle@x>", "d1", "archived", {}) == (1, 2)
    gmail_intake._enqueue_echo(_bundle_manifest("d2", group_size=2))
    gmail_intake._enqueue_echo(_bundle_manifest("d2", group_size=2))
    assert _keys().count("digest:<bundle@x>") == 1
    assert [k for k in _keys() if k.startswith("echo:")] == []


def test_lowered_group_expected_closes_when_results_already_in(echo_env):
    """A promote failure lowers `expected` after the queued files finished."""
    from pipeline import mail_outbox

    mail_outbox.register_group("<bundle@x>", expected=3, sender="sender@example.com", subject="Bundle")
    assert gmail_intake._enqueue_echo(_bundle_manifest("d1")) is None
    assert gmail_intake._enqueue_echo(_bundle_manifest("d2")) is None
    gmail_intake._register_group("<bundle@x>", 2, "sender@example.com", "Bundle")
    assert gmail_intake._close_group_if_complete("<bundle@x>") == "digest:<bundle@x>"
    assert mail_outbox.group_info("<bundle@x>")["closed_at"] is not None
    assert gmail_intake._close_group_if_complete("<bundle@x>") is None  # once only
    assert _keys().count("digest:<bundle@x>") == 1
    assert "INCOMPLETE" not in [r for r in _outbox_rows() if r[0].startswith("digest:")][0][3]


def test_unknown_group_falls_back_to_per_document_echo(echo_env):
    assert gmail_intake._enqueue_echo(_bundle_manifest("d9", message_id="<nogroup@x>")) == "echo:d9:archived"


def test_digest_escapes_hostile_extracted_values():
    results = [
        {
            "doc_id": "d1",
            "stage": "review",
            "filename": '<script>alert("x")</script>.pdf',
            "doc_type": "a & b",
            "reason": '"quoted" <b>bold</b>',
        }
    ]
    html = gmail_intake.build_digest_html("s", results, 1, False)
    assert "<script>" not in html and "<b>bold</b>" not in html
    assert "&lt;script&gt;" in html and "a &amp; b" in html and "&quot;quoted&quot;" in html
    _, ack_html = gmail_intake.build_ack_email(
        "s", [{"filename": "<script>x</script>.pdf", "upload_id": "u1"}], "triage",
        rejected=[("<img src=x>.exe", "extension")],
    )
    assert "<script>" not in ack_html and "<img" not in ack_html


def test_digest_and_ack_are_multipart_with_text_part(echo_env):
    import email as _email

    from pipeline import mail_outbox

    client = FakeIMAP(
        {"1": _message("Bundle", attachments={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"}, message_id="<bundle@x>")}
    )
    gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    gmail_intake._enqueue_echo(_bundle_manifest("d1", group_size=2))
    gmail_intake._enqueue_echo(_bundle_manifest("d2", group_size=2))
    rows = {r[0]: r for r in _outbox_rows()}
    for key in ("ack:<bundle@x>", "digest:<bundle@x>"):
        _k, to, subject, text, html = rows[key]
        row = mail_outbox.OutboxRow(key, to, subject, {"In-Reply-To": "<bundle@x>"}, text, html, 0)
        msg = _email.message_from_bytes(mail_outbox._build_message(row, "llmmailroom@gmail.com").as_bytes())
        types = {p.get_content_type() for p in msg.walk()}
        assert {"text/plain", "text/html"} <= types
        assert msg["Subject"] == "Re: Bundle"


def test_group_size_survives_watcher_intake_meta_whitelist():
    from pipeline.watcher import _intake_meta_from_sidecar

    meta = _intake_meta_from_sidecar({"source": "gmail", "message_id": "<m@x>", "group_size": 3, "junk": 1})
    assert meta["group_size"] == 3
    assert "junk" not in meta


def test_ack_disabled_by_env(echo_env, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ACKS", "0")
    client = FakeIMAP({"1": _message("One", attachments={"a.pdf": _pdf_bytes()}, message_id="<one@x>")})
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["attachments_queued"] == 1 and report["ack_replies"] == 0
    assert _keys() == []
