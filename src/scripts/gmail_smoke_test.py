"""Gmail + watcher connectivity smoke test (HUB-037).

End-to-end proof that the agent mailbox intake channel works and that the
pipeline is AWARE of it — using an example insurance claim (the committed
FNOL fixtures under ``src/tests/fixtures/insurance_claim/``).

What it proves, leg by leg:

    [connectivity] an email carrying an insurance-claim attachment is fetched
                   off the mailbox by the Gmail intake poller (IMAP sweep)
    [route]        the attachment lands in the SAME inbox bin the watcher
                   drains, with the ``<file>.meta`` sidecar
    [watcher]      the watcher claims it and runs the full 13-node pipeline
                   to a terminal stage (archived for a clean FNOL fixture)
    [awareness]    the manifest records ``intake.source == "gmail"`` and the
                   matter routed from the subject ``[M:<id>]`` tag; a real
                   run also tags the trace ``source-gmail``
    [classify]     the document classifies as ``insurance_claim``

Modes:

    # Default: fully network-free + zero-LLM. Fake IMAP client, mock LLM,
    # scratch MAILROOM_BASE_DIR. Safe anywhere; proves the machinery.
    PYTHONPATH=src python src/scripts/gmail_smoke_test.py

    # Real connectivity: SENDS the smoke email to the agent mailbox via
    # SMTP (same app password), then polls the REAL mailbox over IMAP. All
    # currently-unseen messages are drained (marked seen; their attachments
    # queue into the scratch inbox) — run it on a quiet mailbox.
    PYTHONPATH=src python src/scripts/gmail_smoke_test.py --real

    # Real LLM too (costs OpenRouter tokens; needs OPENROUTER_API_KEY):
    PYTHONPATH=src python src/scripts/gmail_smoke_test.py --real --llm real
    PYTHONPATH=src python src/scripts/gmail_smoke_test.py --llm real      # mock IMAP only

Exit code 0 = every leg passed. Credentials come from ``.env``
(GMAIL_ADDRESS / GMAIL_APP_PASSWORD); they are never printed.
"""

from __future__ import annotations

import argparse
import email.message
import email.utils
import json
import os
import smtplib
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.env import load_env

load_env()

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = PACKAGE_ROOT / "tests" / "fixtures" / "insurance_claim" / "sample_claim.txt"

MOCK_CLASSIFICATION = {
    "doc_type": "insurance_claim",
    "contract_subtype": None,
    # The classification guard requires the subclass token when the class has
    # a catalog (insurance does) — "other" is the sanctioned fallback.
    "doc_subclass": "other",
    "confidence": 0.99,
    "reasoning": "smoke mock",
}

MOCK_EXTRACTION = {
    "claim_number": "2026-CLM-041701",
    "policy_number": "HO-44-88391-A",
    "insurer": "Acme Insurance Company",
    "insured_party": "Jack B, Morningstar Collective LLC",
    "claim_type": "property",
    "date_of_loss": "2026-03-14",
    "date_filed": "2026-03-21",
    "claimed_amount": 18500.0,
    "adjuster": "J. Featherstone",
    "damages_description": "Hail damage to roof and detached garage (smoke test)",
    "coverage_determination": "approved",
    "confidence": 0.99,
}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_smoke_email(matter_id: str, fixture: Path) -> tuple[bytes, str, str]:
    """Build the smoke email: FNOL fixture attached, ``[M:<matter>]`` subject tag.

    Returns ``(raw_bytes, message_id, attachment_filename)``.
    """
    stamp = _now_stamp()
    message_id = f"<gmail-smoke-{uuid.uuid4().hex[:12]}@mailroom.local>"
    attachment_name = f"insurance_fnol_smoke_{stamp}.txt"
    msg = email.message.EmailMessage()
    address = os.environ.get("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    msg["From"] = address
    msg["To"] = address
    msg["Subject"] = f"FNOL smoke {stamp} [M:{matter_id}]"
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg.set_content(
        "Mailroom connectivity smoke test — the attachment is an example "
        "insurance claim (FNOL) for the watcher to process."
    )
    payload = fixture.read_bytes()
    msg.add_attachment(
        payload,
        maintype="application",
        subtype="octet-stream",
        filename=attachment_name,
    )
    return msg.as_bytes(), message_id, attachment_name


# ---------------------------------------------------------------------------
# Mock LLM plumbing — mirrors scripts/run_pilot.py --mock (both LLM paths:
# the vendored LangChain sorter bypasses get_llm; the BaseAgent specialists
# import get_llm at module import, so BOTH bindings are patched).
# ---------------------------------------------------------------------------


def _mock_get_llm(agent_name: str):
    def create(**kwargs):
        last_msg = (kwargs.get("messages") or [{}])[-1]
        user_content = last_msg.get("content", "") if isinstance(last_msg, dict) else ""
        if "Classify this legal document" in user_content or "RE-EVALUATION REQUESTED" in user_content:
            content = json.dumps(MOCK_CLASSIFICATION)
        elif "ADJUDICATION REQUEST" in user_content:
            content = json.dumps(
                {"decision": "approved", "reasoning": "smoke mock", "resolution_notes": ""}
            )
        else:
            content = json.dumps(MOCK_EXTRACTION)
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client, "mock/mock-smoke"


class _FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in serving one pre-loaded message."""

    def __init__(self, messages: dict[str, bytes]):
        self._messages = messages
        self.seen: set[str] = set()

    def login(self, user, password):
        pass

    def select(self, folder, readonly=False):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "search":
            unseen = " ".join(u for u in self._messages if u not in self.seen)
            return ("OK", [unseen.encode()])
        if command == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            data = self._messages[uid]
            header = b"1 (UID " + uid.encode() + b" RFC822 {" + str(len(data)).encode() + b"}"
            return ("OK", [(header, data), b")"])
        if command == "store":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            self.seen.add(uid)
            return ("OK", [b"OK"])
        raise AssertionError(f"unexpected imap command {command!r}")

    def logout(self):
        pass


def _prepare_base_dir() -> Path:
    """Scratch MAILROOM_BASE_DIR so smoke runs never pollute real bins."""
    scratch = Path(tempfile.mkdtemp(prefix="gmail-smoke-"))
    os.environ["MAILROOM_BASE_DIR"] = str(scratch)
    from pipeline.bins import ensure_dirs, inbox_dir, processing_dir

    ensure_dirs(inbox_dir(), processing_dir())
    return scratch


def _mock_llm_patches():
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent
    from langchain_agents.mock import FakeLangChainLLM

    def _langchain_llm(self):
        # Same mechanism as scripts/run_pilot.py --mock: the vendored LangChain
        # sorter builds its own ChatOpenAI and bypasses get_llm, so the patch
        # targets BaseAgent.llm with a deterministic fake keyed to the FNOL.
        return FakeLangChainLLM(classification=MOCK_CLASSIFICATION, extraction=MOCK_EXTRACTION)

    return [
        patch("llm.client.get_llm", side_effect=_mock_get_llm),
        patch("agents.base.get_llm", side_effect=_mock_get_llm),
        patch.object(_LangChainBaseAgent, "llm", new=_langchain_llm),
    ]


def _run_watcher_route(inbox_file: Path, llm_mode: str) -> tuple[dict | None, list[str]]:
    """Run the watcher's exact claim path on the delivered inbox file.

    Returns (terminal_manifest_or_None, checks_so_far_appended).
    """
    from pipeline.bins import manifests_dir

    patches = _mock_llm_patches() if llm_mode == "mock" else []
    with _ctx(patches):
        from pipeline.watcher import Watcher

        watcher = Watcher()
        watcher._process_existing(inbox_file)

    # Find the terminal manifest for this filename.
    import json as _json

    for mf in manifests_dir().glob("*.json"):
        try:
            data = _json.loads(mf.read_text())
        except Exception:
            continue
        if data.get("original_filename") == inbox_file.name and data.get("stage") in (
            "archived",
            "failed",
            "review",
        ):
            return data, []
    return None, []


class _ctx:
    def __init__(self, patches):
        self._patches = patches
        self._entered = []

    def __enter__(self):
        for p in self._patches:
            self._entered.append(p.__enter__())
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


def _send_via_smtp(raw: bytes) -> None:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        raise SystemExit(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing — set them in packages/llm-mailroom/.env"
        )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(address, password)
        server.sendmail(address, [address], raw)


def run_mock(matter_id: str, fixture: Path, llm_mode: str) -> list[tuple[str, bool, str]]:
    """Network-free smoke: fake IMAP serves the smoke email; scratch bins."""
    checks: list[tuple[str, bool, str]] = []

    # Hermetic: fake mailbox credentials when the real .env is absent.
    os.environ.setdefault("GMAIL_ADDRESS", "smoke@example.com")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "smoke-smoke-smoke-sm")

    scratch = _prepare_base_dir()
    raw, message_id, attachment_name = build_smoke_email(matter_id, fixture)

    # [connectivity] poll_once over a fake IMAP client.
    from pipeline import gmail_intake

    report = gmail_intake.poll_once(
        config=gmail_intake.load_config(),
        imap_factory=lambda: _FakeIMAP({"1": raw}),
    )
    checks.append(
        (
            "connectivity: IMAP sweep fetched the message",
            report["connected"] and report["messages_seen"] == 1,
            f"messages_seen={report['messages_seen']} errors={report['errors']}",
        )
    )

    # [route] attachment + sidecar in the inbox.
    from pipeline.bins import inbox_dir, read_inbox_meta

    delivered = inbox_dir() / attachment_name
    meta = read_inbox_meta(delivered) if delivered.exists() else None
    checks.append(
        (
            "route: attachment + meta sidecar landed in the inbox",
            delivered.exists() and meta is not None and meta.get("message_id") == message_id,
            str(delivered if delivered.exists() else "MISSING"),
        )
    )

    # [watcher] + [awareness] + [classify]: the watcher's exact claim path.
    manifest, _ = _run_watcher_route(delivered, llm_mode)
    checks.append(
        (
            "watcher: claimed + full pipeline to a terminal stage",
            manifest is not None and manifest.get("stage") == "archived",
            f"stage={manifest.get('stage') if manifest else 'NO MANIFEST'}",
        )
    )
    intake = (manifest or {}).get("intake") or {}
    checks.append(
        (
            "awareness: manifest records intake.source=gmail + routed matter",
            intake.get("source") == "gmail"
            and (manifest or {}).get("matter_id") == matter_id,
            f"intake.source={intake.get('source')} matter_id={(manifest or {}).get('matter_id')}",
        )
    )
    checks.append(
        (
            "classify: doc_type == insurance_claim",
            (manifest or {}).get("doc_type") == "insurance_claim",
            f"doc_type={(manifest or {}).get('doc_type')}",
        )
    )
    print(f"scratch base dir: {scratch}")
    return checks


def run_real(matter_id: str, fixture: Path, llm_mode: str) -> list[tuple[str, bool, str]]:
    """Real connectivity: SMTP send → real IMAP poll → watcher route."""
    checks: list[tuple[str, bool, str]] = []

    scratch = _prepare_base_dir()
    raw, message_id, attachment_name = build_smoke_email(matter_id, fixture)

    try:
        _send_via_smtp(raw)
        sent = True
        detail = "delivered to the mailbox via SMTP SSL"
    except Exception as exc:
        sent = False
        detail = f"{type(exc).__name__}: {exc}"
    checks.append(("connectivity: SMTP send to the agent mailbox", sent, detail))
    if not sent:
        return checks

    # Give Gmail a moment to deliver, then sweep the real mailbox.
    import time

    time.sleep(5)
    from pipeline import gmail_intake

    report = gmail_intake.poll_once()
    checks.append(
        (
            "connectivity: real IMAP sweep (UNSEEN) ran",
            report["connected"],
            f"messages_seen={report['messages_seen']} queued={report['attachments_queued']} errors={report['errors']}",
        )
    )

    from pipeline.bins import inbox_dir, read_inbox_meta

    delivered = None
    for candidate in inbox_dir().glob("*.txt"):
        meta = read_inbox_meta(candidate) or {}
        if meta.get("message_id") == message_id:
            delivered = candidate
            break
    checks.append(
        (
            "route: OUR attachment (+sidecar) landed in the inbox",
            delivered is not None,
            str(delivered) if delivered else f"{attachment_name} not found in {inbox_dir()}",
        )
    )
    if delivered is None:
        return checks

    manifest, _ = _run_watcher_route(delivered, llm_mode)
    checks.append(
        (
            "watcher: claimed + full pipeline to a terminal stage",
            manifest is not None and manifest.get("stage") in ("archived", "review", "failed"),
            f"stage={manifest.get('stage') if manifest else 'NO MANIFEST'}",
        )
    )
    intake = (manifest or {}).get("intake") or {}
    checks.append(
        (
            "awareness: manifest records intake.source=gmail + routed matter",
            intake.get("source") == "gmail" and (manifest or {}).get("matter_id") == matter_id,
            f"intake.source={intake.get('source')} matter_id={(manifest or {}).get('matter_id')}",
        )
    )
    if llm_mode == "real":
        checks.append(
            (
                "classify: doc_type == insurance_claim (real LLM)",
                (manifest or {}).get("doc_type") == "insurance_claim",
                f"doc_type={(manifest or {}).get('doc_type')}",
            )
        )
    print(f"scratch base dir: {scratch}")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real", action="store_true", help="send + poll the real mailbox (SMTP + IMAP)")
    parser.add_argument("--llm", choices=("mock", "real"), default="mock", help="pipeline LLM mode (default mock)")
    parser.add_argument("--matter", default="SMOKE-GMAIL", help="matter id for the [M:] subject tag")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="insurance-claim fixture to attach")
    args = parser.parse_args()

    from pipeline.env import default_environment
    from pipeline.logging import setup_logging

    os.environ.setdefault("OBSERVABILITY_PROVIDER", "none")  # smoke stays hermetic unless overridden
    load_env()
    default_environment("misc")
    setup_logging()

    if not args.fixture.exists():
        raise SystemExit(f"fixture not found: {args.fixture}")
    if args.llm == "real" and not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("--llm real requires OPENROUTER_API_KEY")

    print(
        f"gmail smoke: mode={'real' if args.real else 'mock'} llm={args.llm} "
        f"matter={args.matter} fixture={args.fixture.name}"
    )
    checks = (
        run_real(args.matter, args.fixture, args.llm)
        if args.real
        else run_mock(args.matter, args.fixture, args.llm)
    )

    print()
    ok = True
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} — {detail}")
        ok = ok and passed
    print()
    print("GMAIL SMOKE: PASS" if ok else "GMAIL SMOKE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
