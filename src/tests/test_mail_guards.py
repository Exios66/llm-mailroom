"""Gmail sender authentication + loop guards (pipeline/mail_guards.py)."""

import email
import email.message

import pytest

from pipeline import gmail_intake, mail_guards, mail_outbox
from tests.test_gmail_intake import FakeIMAP, _cfg, _message, _pdf_bytes


def _msg(**headers) -> email.message.Message:
    m = email.message.EmailMessage()
    m["From"] = headers.pop("From", "client@firm.example")
    m["Subject"] = "hello"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content("body")
    return m


def test_forged_auth_results_header_ignored():
    v = mail_guards.parse_authentication_results(
        ["mx.google.com; dmarc=fail", "mx.google.com; dmarc=pass"]
    )
    assert v.dmarc == "fail"


def test_untrusted_topmost_auth_results_fails_closed():
    v = mail_guards.parse_authentication_results(
        ["evil.example; dmarc=pass", "mx.google.com; dmarc=pass"]
    )
    assert v.dmarc is None and v.dkim is None and v.spf is None
    assert mail_guards.sender_permitted({"a@x.example"}, "a@x.example", v, True) == (False, "dmarc")


def test_parse_reads_all_methods_and_folded_header():
    v = mail_guards.parse_authentication_results(
        ["mx.google.com;\r\n       dkim=pass header.i=@firm.example;\r\n       spf=softfail; dmarc=PASS (p=NONE)"]
    )
    assert (v.dkim, v.spf, v.dmarc) == ("pass", "softfail", "pass")
    assert mail_guards.parse_authentication_results([]).dmarc is None


def test_method_only_recognised_at_segment_start():
    # A property value that looks like a method result must not win over the
    # real dmarc result later in the header.
    v = mail_guards.parse_authentication_results(
        ["mx.google.com; spf=pass smtp.mailfrom=dmarc=pass@evil.example; dmarc=fail header.from=evil.example"]
    )
    assert (v.spf, v.dmarc) == ("pass", "fail")
    v = mail_guards.parse_authentication_results(
        ['mx.google.com; spf=pass (comment; dmarc=pass) smtp.mailfrom="x;dmarc=pass"; dmarc=fail']
    )
    assert v.dmarc == "fail"


def test_allowlist_requires_dmarc_pass():
    allowed = {"a@firm.example"}
    fail = mail_guards.AuthVerdict(dmarc="fail")
    ok = mail_guards.AuthVerdict(dmarc="pass")
    assert mail_guards.sender_permitted(allowed, "a@firm.example", fail, True) == (False, "dmarc")
    assert mail_guards.sender_permitted(allowed, "a@firm.example", ok, True)[0] is True
    assert mail_guards.sender_permitted(allowed, '"Name" <A@Firm.Example>', ok, True)[0] is True
    assert mail_guards.sender_permitted(allowed, "a@firm.example", fail, False)[0] is True
    assert mail_guards.sender_permitted(set(), "x@y.example", fail, True) == (True, "open")
    assert mail_guards.sender_permitted(allowed, "b@firm.example", ok, True) == (False, "allowlist")


def test_plus_tag_does_not_match_bare_allowlist_entry():
    ok = mail_guards.AuthVerdict(dmarc="pass")
    assert mail_guards.sender_permitted({"a@x.example"}, "a+tag@x.example", ok, True) == (
        False,
        "allowlist",
    )


@pytest.mark.parametrize(
    "headers,reason",
    [
        ({"Auto-Submitted": "auto-replied"}, "auto-submitted"),
        ({"Precedence": "bulk"}, "precedence"),
        ({"Precedence": "list"}, "precedence"),
        ({"Precedence": "junk"}, "precedence"),
        ({"List-Id": "<news.firm.example>"}, "list"),
        ({"List-Unsubscribe": "<mailto:u@firm.example>"}, "list"),
        ({"X-Auto-Response-Suppress": "All"}, "auto-response-suppress"),
        ({"Return-Path": "<>"}, "null-return-path"),
        ({"From": "MAILER-DAEMON@googlemail.com"}, "daemon-sender"),
        ({"From": "postmaster@firm.example"}, "daemon-sender"),
        ({"From": "no-reply@firm.example"}, "daemon-sender"),
        ({"From": "noreply@firm.example"}, "daemon-sender"),
        ({"From": "Mailroom <LLMMailroom@gmail.com>"}, "own-address"),
    ],
)
def test_automated_reason_cases(headers, reason):
    assert mail_guards.automated_reason(_msg(**headers), "llmmailroom@gmail.com") == reason


def test_automated_reason_dsn_and_human():
    dsn = email.message.EmailMessage()
    dsn["From"] = "client@firm.example"
    dsn.set_content("x")
    dsn.make_mixed()
    dsn.set_type("multipart/report")
    assert mail_guards.automated_reason(dsn, "llmmailroom@gmail.com") == "dsn"
    assert mail_guards.automated_reason(_msg(Auto_Submitted="no"), "llmmailroom@gmail.com") is None
    assert mail_guards.automated_reason(_msg(), "llmmailroom@gmail.com") is None


def test_poll_skips_automated_without_reply(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "Out of office",
                attachments={"c.pdf": _pdf_bytes()},
                extra_headers={"Auto-Submitted": "auto-replied"},
            )
        }
    )
    report = gmail_intake.poll_once(config=_cfg(), imap_factory=lambda: client)
    assert report["skipped_automated"] == 1
    assert report["attachments_queued"] == 0
    assert "1" in client.seen

    from pipeline.bins import list_inbox_files

    assert list_inbox_files() == []
    assert mail_outbox.count_recent("sender@example.com", 3600) == 0


def test_poll_rejects_allowlisted_sender_without_dmarc_pass(temp_base_dir):
    client = FakeIMAP(
        {
            "1": _message(
                "spoof",
                sender="client@firm.example",
                attachments={"c.pdf": _pdf_bytes()},
                auth_results="mx.google.com; dmarc=fail",
                message_id="<spoof@x>",
            ),
            "2": _message(
                "real",
                sender="Client <Client@Firm.Example>",
                attachments={"d.pdf": _pdf_bytes()},
                message_id="<real@x>",
            ),
        }
    )
    report = gmail_intake.poll_once(
        config=_cfg(allowed_senders={"client@firm.example"}, require_dmarc=True),
        imap_factory=lambda: client,
    )
    assert report["skipped_auth"] == 1
    assert report["attachments_queued"] == 1
    assert {"1", "2"} <= client.seen


def test_reply_budget(temp_base_dir):
    for i in range(20):
        mail_outbox.enqueue(f"k{i}", to_addr="a@x.example", subject="s", text="t", html=None)
    assert mail_guards.reply_allowed("a@x.example", 20) is False
    assert mail_guards.reply_allowed("A <a@x.example>", 21) is True
    assert mail_guards.reply_allowed("b@x.example", 20) is True


def test_allow_self_lets_own_address_through_but_not_auto_replies():
    own = _msg(From="llmmailroom@gmail.com")
    assert mail_guards.automated_reason(own, "llmmailroom@gmail.com", allow_self=True) is None
    reply = _msg(From="llmmailroom@gmail.com", Auto_Submitted="auto-replied")
    assert mail_guards.automated_reason(reply, "llmmailroom@gmail.com", allow_self=True) == "auto-submitted"
