"""Gmail intake channel (HUB-037) — network-free tests.

Every test drives ``poll_once`` through a fake IMAP client; no socket is
ever opened. The channel is opt-in (``MAILROOM_GMAIL_ENABLED=1``) and
conftest keeps it disabled for the whole suite, so a production ``.env``
with real credentials can never leak network polls into tests.
"""

import email.message

import pytest

from pipeline import gmail_intake


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n% fake attachment for tests\n"


def _message(
    subject: str,
    sender: str = "sender@example.com",
    attachments=(),
    message_id: str = "<msg-1@example.com>",
) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "llmmailroom@gmail.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 01 Sep 2026 12:00:00 +0000"
    msg.set_content("please process the attached documents")
    for filename, payload in dict(attachments).items():
        subtype = "pdf" if filename.endswith(".pdf") else "octet-stream"
        msg.add_attachment(
            payload, maintype="application", subtype=subtype, filename=filename
        )
    return msg.as_bytes()


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in (uid commands only, as used by the poller)."""

    def __init__(self, messages: dict[str, bytes], ignore_store: bool = False):
        self._messages = messages
        self._ignore_store = ignore_store
        self.seen: set[str] = set()
        self.labels: dict[str, list[str]] = {}
        self.store_calls = 0
        self.logged_in = False
        self.selected = None

    def login(self, user, password):
        self.logged_in = True

    def enable(self, capability):
        self.enabled = capability
        return ("OK", [b"ENABLED"])

    def select(self, folder, readonly=False):
        self.selected = folder
        return ("OK", [b"1"])

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
            data = self._messages.get(uid)
            if data is None:
                return ("OK", [None])
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
    try:
        ok = gmail_intake.send_intake_echo(_echo_manifest())
        assert ok is True
        assert len(fake.sent) == 1
        frm, to, raw = fake.sent[0]
        assert to == ["client@firm.example"]
        msg = _email.message_from_bytes(raw)
        assert msg["In-Reply-To"] == "<echo-1@example.com>"
        assert msg["Subject"] == "Re: FNOL [M:M-1]"
        body = msg.get_payload()
        assert "STATUS: ARCHIVED" in body
        assert "d-echo-1" in body
        assert "insurance_claim" in body
        # Dedup: the same (doc, stage) echo is sent exactly once.
        assert gmail_intake.send_intake_echo(_echo_manifest()) is True
        assert len(fake.sent) == 1
    finally:
        gmail_intake.set_smtp_factory(None)


def test_echo_skips_non_gmail_and_disabled_channel(temp_base_dir, monkeypatch):
    monkeypatch.setenv("MAILROOM_GMAIL_ENABLED", "1")
    monkeypatch.setenv("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "apppassword1234")
    fake = FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: fake)
    gmail_intake._ECHO_DONE.clear()
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
