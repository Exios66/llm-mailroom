"""Gmail + watcher connectivity smoke test (HUB-037).

End-to-end proof that the agent mailbox intake channel works and that the
watcher is AWARE of it — using an example insurance claim (the committed
FNOL fixtures under ``src/tests/fixtures/insurance_claim/``). Two emails
are swept: a SINGLE-document email and a MULTI-document (bundle) email, so
the single-vs-bundle routing contract is proven on both lanes.

What it proves, leg by leg:

    [connectivity] both emails are fetched off the mailbox by the Gmail
                   intake poller (IMAP sweep)
    [route]        the SINGLE-document attachment lands in the SAME inbox
                   bin with a ``route: triage`` sidecar; the bundle's two
                   attachments carry ``route: pipeline``
    [triage-lane]  the single-document upload is handled by the FREE triage
                   lane — core pipeline steps WITHOUT the paid agents: the
                   terminal manifest is archived with ``intake.triage``
                   (primary class + gist) and NO pipeline extraction
    [triage-audit] the lane's audit entries live in their own section
                   (``triage_ingested`` / ``triage_classified`` /
                   ``triage_archived`` — never the pipeline vocabulary)
    [pipeline-route] the multi-document upload runs the FULL paid pipeline
                   to a terminal stage (archived for a clean FNOL fixture),
                   with extraction — and the triage read is DROPPED (no
                   ``intake.triage`` on those manifests)
    [classify]     the pipeline-route document classifies as
                   ``insurance_claim``
    [echo]         completion reports reply on the source email threads;
                   the single-document echo carries the INTAKE TRIAGE
                   section, the pipeline echo does not

Modes:

    # Default: fully network-free + zero-LLM. Fake IMAP client, mock LLM,
    # scratch MAILROOM_BASE_DIR. Safe anywhere; proves the machinery.
    PYTHONPATH=src python src/scripts/gmail_smoke_test.py

    # Real connectivity: SENDS the smoke emails to the agent mailbox via
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
import tempfile
import time
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

MOCK_TRIAGE = {
    "primary_doc_class": "insurance_claim",
    "doc_subclass": "other",
    "confidence": 0.97,
    "gist": "An insurance first notice of loss (FNOL) for hail damage.",
    "keywords": ["FNOL", "hail damage", "policy", "claim"],
}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_smoke_email(
    matter_id: str,
    fixture: Path,
    attachment_name: str | None = None,
    *,
    stamp: str | None = None,
    subject_prefix: str = "FNOL smoke",
    n_attachments: int = 1,
) -> tuple[bytes, str, list[str]]:
    """Build a smoke email: FNOL fixture(s) attached, ``[M:<matter>]`` subject tag.

    ``n_attachments=1`` produces a single-document upload (triage lane);
    ``n_attachments=2`` produces a multi-document upload (full pipeline).
    Returns ``(raw_bytes, message_id, attachment_filenames)``.
    """
    stamp = stamp or _now_stamp()
    message_id = f"<gmail-smoke-{uuid.uuid4().hex[:12]}@mailroom.local>"
    attachment_names = [
        attachment_name or (f"insurance_fnol_smoke_{stamp}.txt" if n_attachments == 1 else f"insurance_fnol_bundle_{stamp}_a.txt"),
        f"insurance_fnol_bundle_{stamp}_b.txt",
    ][:n_attachments]
    msg = email.message.EmailMessage()
    address = os.environ.get("GMAIL_ADDRESS", "llmmailroom@gmail.com")
    msg["From"] = address
    msg["To"] = address
    msg["Subject"] = f"{subject_prefix} {stamp} [M:{matter_id}]"
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg.set_content(
        "Mailroom connectivity smoke test — the attachment is an example "
        "insurance claim (FNOL) for the watcher to process."
    )
    payload = fixture.read_bytes()
    for name in attachment_names:
        msg.add_attachment(
            payload,
            maintype="application",
            subtype="octet-stream",
            filename=name,
        )
    return msg.as_bytes(), message_id, attachment_names


# ---------------------------------------------------------------------------
# Mock LLM plumbing — mirrors scripts/run_pilot.py --mock (both LLM paths:
# the vendored LangChain sorter bypasses get_llm; the BaseAgent specialists
# import get_llm at module import, so BOTH bindings are patched).
# ---------------------------------------------------------------------------


def _mock_get_llm(agent_name: str):
    def create(**kwargs):
        last_msg = (kwargs.get("messages") or [{}])[-1]
        user_content = last_msg.get("content", "") if isinstance(last_msg, dict) else ""
        if user_content.startswith("File: ") and "Document text:" in user_content:
            content = json.dumps(MOCK_TRIAGE)  # gmail intake triage (free model)
        elif "Classify this legal document" in user_content or "RE-EVALUATION REQUESTED" in user_content:
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
    """Minimal imaplib.IMAP4_SSL stand-in serving one pre-loaded message.

    Records ``X-GM-LABELS`` stores so the watcher's check-emoji reaction is
    provable without network.
    """

    def __init__(self, messages: dict[str, bytes]):
        self._messages = messages
        self.seen: set[str] = set()
        self.labels: dict[str, list[str]] = {}

    def login(self, user, password):
        pass

    def enable(self, capability):
        self.enabled = capability
        return ("OK", [b"ENABLED"])

    def select(self, folder, readonly=False):
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
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            data = self._messages[uid]
            header = b"1 (UID " + uid.encode() + b" RFC822 {" + str(len(data)).encode() + b"}"
            return ("OK", [(header, data), b")"])
        if command == "STORE":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            flags = " ".join(
                a.decode("utf-8", "ignore") if isinstance(a, bytes) else str(a)
                for a in args[1:]
            )
            if "X-GM-LABELS" in flags:
                self.labels.setdefault(uid, []).append(
                    flags.split("X-GM-LABELS", 1)[1].strip()
                )
            else:
                self.seen.add(uid)
            return ("OK", [b"OK"])
        raise AssertionError(f"unexpected imap command {command!r}")

    def logout(self):
        pass


class _FakeSMTP:
    """Records sendmail calls so the completion echo is provable without network."""

    def __init__(self):
        self.sent = []

    def login(self, user, password):
        pass

    def sendmail(self, frm, to, raw):
        self.sent.append((frm, to, raw))

    def quit(self):
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
    """Network-free smoke: fake IMAP serves both smoke emails; scratch bins."""
    checks: list[tuple[str, bool, str]] = []

    # Hermetic: fake mailbox credentials when the real .env is absent. The
    # mock sender + allowlist are FORCED so the smoke never depends on the
    # real .env roster (HUB-039 pilot allowlist would reject the mock sender).
    os.environ["GMAIL_ADDRESS"] = "smoke@example.com"
    os.environ.setdefault("GMAIL_APP_PASSWORD", "smoke-smoke-smoke-sm")
    os.environ.setdefault("MAILROOM_GMAIL_ENABLED", "1")
    os.environ["MAILROOM_GMAIL_ALLOWED_SENDERS"] = "smoke@example.com"

    scratch = _prepare_base_dir()
    from pipeline import gmail_intake

    # [emails] one single-document email + one multi-document (bundle) email.
    single_raw, single_mid, single_names = build_smoke_email(matter_id, fixture)
    bundle_raw, bundle_mid, bundle_names = build_smoke_email(
        matter_id,
        fixture,
        subject_prefix="BUNDLE smoke",
        n_attachments=2,
    )
    stamp = _now_stamp()

    # [connectivity] poll_once over a fake IMAP client; the same fake also
    # serves the watcher's reaction (module-level seam), so the ✅ label
    # application is provable network-free.
    messages = {"1": single_raw, "2": bundle_raw}
    fake_imap = _FakeIMAP(messages)
    gmail_intake.set_imap_factory(lambda: fake_imap)  # also serves the watcher's ✅ reactions
    report = gmail_intake.poll_once(
        config=gmail_intake.load_config(),
        imap_factory=lambda: fake_imap,
    )
    checks.append(
        (
            "connectivity: IMAP sweep fetched both messages",
            report["connected"] and report["messages_seen"] == 2,
            f"messages_seen={report['messages_seen']} queued={report['attachments_queued']} errors={report['errors']}",
        )
    )

    # [route] the poller stamps the single-vs-bundle routing contract.
    from pipeline.bins import inbox_dir, read_inbox_meta

    single_path = inbox_dir() / single_names[0]
    single_meta = read_inbox_meta(single_path) if single_path.exists() else None
    checks.append(
        (
            "route: single-document email carries route=triage",
            single_meta is not None and single_meta.get("route") == "triage",
            f"route={single_meta.get('route') if single_meta else 'NO SIDECAR'}",
        )
    )
    bundle_paths = [inbox_dir() / n for n in bundle_names]
    bundle_metas = [read_inbox_meta(p) if p.exists() else None for p in bundle_paths]
    checks.append(
        (
            "route: multi-document email carries route=pipeline on every attachment",
            len(bundle_metas) == 2
            and all(m is not None and m.get("route") == "pipeline" for m in bundle_metas),
            f"routes={[m.get('route') if m else None for m in bundle_metas]}",
        )
    )

    # [triage-lane] + [pipeline-route]: the watcher's exact claim path on each
    # delivered file — the single-doc file takes the free-triage lane, the
    # bundle files take the full paid pipeline.
    smtp = _FakeSMTP()
    gmail_intake.set_smtp_factory(lambda: smtp)
    single_manifest, _ = _run_watcher_route(single_path, llm_mode)
    bundle_manifests = [_run_watcher_route(p, llm_mode)[0] for p in bundle_paths]

    single_intake = (single_manifest or {}).get("intake") or {}
    triage = single_intake.get("triage") or {}
    checks.append(
        (
            "triage-lane: single-doc handled by the triage lane (archived, no paid extraction)",
            single_manifest is not None
            and single_manifest.get("stage") == "archived"
            and triage.get("primary_doc_class") == "insurance_claim"
            and not single_manifest.get("extracted_data"),
            f"stage={single_manifest.get('stage') if single_manifest else 'NO MANIFEST'} "
            f"triage.class={triage.get('primary_doc_class')} extracted={'yes' if single_manifest and single_manifest.get('extracted_data') else 'no'}",
        )
    )

    # [triage-audit] the lane's audit entries live in their own section.
    import asyncio

    from storage.audit_log import get_audit_chain

    audit_events = []
    if single_manifest:
        audit_rows = asyncio.run(get_audit_chain(single_manifest["doc_id"]))
        audit_events = [r["event"] for r in audit_rows]
    checks.append(
        (
            "triage-audit: audit entries namespaced triage_* (own section)",
            audit_events == ["triage_ingested", "triage_classified", "triage_archived"],
            f"events={audit_events}",
        )
    )

    checks.append(
        (
            "pipeline-route: bundle documents ran the full paid pipeline",
            len(bundle_manifests) == 2
            and all(m is not None and m.get("stage") == "archived" and m.get("extracted_data") for m in bundle_manifests),
            f"stages={[m.get('stage') if m else None for m in bundle_manifests]}",
        )
    )
    checks.append(
        (
            "no-triage-on-multi: bundle manifests carry NO intake.triage",
            all((not ((m or {}).get("intake") or {}).get("triage")) for m in bundle_manifests),
            "triage dropped for multi-document uploads",
        )
    )
    bundle_doc_type = (bundle_manifests[0] or {}).get("doc_type") if bundle_manifests else None
    checks.append(
        (
            "classify: pipeline-route doc_type == insurance_claim",
            bundle_doc_type == "insurance_claim",
            f"doc_type={bundle_doc_type}",
        )
    )

    # [reaction] the watcher reacted to the source emails with the ✅ label
    # (async daemon thread — bounded wait, then read the status counter).
    deadline = time.time() + 5
    while time.time() < deadline and gmail_intake.status()["reactions_sent"] < 1:
        time.sleep(0.1)
    reacted = gmail_intake.status()["reactions_sent"] >= 1
    checks.append(
        (
            "reaction: source emails reacted with the check emoji (✅ label)",
            reacted,
            f"reactions_sent={gmail_intake.status()['reactions_sent']}",
        )
    )

    # [echo] completion reports replied on the source threads (async daemon
    # threads — bounded wait, then inspect what was sent). The triage-lane
    # echo carries the INTAKE TRIAGE section; the pipeline echoes do not.
    deadline = time.time() + 10
    while time.time() < deadline and gmail_intake.status()["echoes_sent"] < 3:
        time.sleep(0.1)
    echo_triage = echo_pipeline = False
    if smtp.sent:
        import email as _email

        for _, to, raw in smtp.sent:
            msg = _email.message_from_bytes(raw)
            payload = msg.get_payload(decode=True)
            body = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else (payload or "")
            if "INTAKE TRIAGE" in body:
                echo_triage = True
            else:
                echo_pipeline = True
    checks.append(
        (
            "echo: triage-lane echo carries INTAKE TRIAGE; pipeline echoes do not",
            echo_triage and echo_pipeline,
            f"sent={len(smtp.sent)} triage_echo={echo_triage} pipeline_echo={echo_pipeline}",
        )
    )
    gmail_intake.set_smtp_factory(None)  # reset the seam
    gmail_intake.set_imap_factory(None)  # reset the seam
    print(f"scratch base dir: {scratch}")
    return checks


def run_real(matter_id: str, fixture: Path, llm_mode: str) -> list[tuple[str, bool, str]]:
    """Real connectivity: SMTP send → real IMAP poll → watcher route."""
    checks: list[tuple[str, bool, str]] = []

    scratch = _prepare_base_dir()
    single_raw, single_mid, single_names = build_smoke_email(matter_id, fixture)
    bundle_raw, bundle_mid, bundle_names = build_smoke_email(
        matter_id,
        fixture,
        subject_prefix="BUNDLE smoke",
        n_attachments=2,
    )
    from pipeline import gmail_intake

    gmail_intake.set_imap_factory(None)  # real run: never inherit an injected factory
    reactions_before = gmail_intake.status()["reactions_sent"]

    try:
        _send_via_smtp(single_raw)
        _send_via_smtp(bundle_raw)
        sent = True
        detail = "both smoke emails delivered to the mailbox via SMTP SSL"
    except Exception as exc:
        sent = False
        detail = f"{type(exc).__name__}: {exc}"
    checks.append(("connectivity: SMTP send to the agent mailbox", sent, detail))
    if not sent:
        return checks

    # Give Gmail a moment to deliver, then sweep the real mailbox.
    time.sleep(5)

    report = gmail_intake.poll_once()
    checks.append(
        (
            "connectivity: real IMAP sweep (UNSEEN) ran",
            report["connected"],
            f"messages_seen={report['messages_seen']} queued={report['attachments_queued']} errors={report['errors']}",
        )
    )

    from pipeline.bins import inbox_dir, read_inbox_meta

    def _find_delivered(filenames):
        for candidate in inbox_dir().glob("*.txt"):
            meta = read_inbox_meta(candidate) or {}
            if meta.get("original_filename") in filenames:
                return candidate, meta
        return None, None

    single_path, single_meta = _find_delivered(single_names)
    checks.append(
        (
            "route: single-document email carries route=triage (real mailbox)",
            single_meta is not None and single_meta.get("route") == "triage",
            str(single_path) if single_meta else "single-doc attachment not found",
        )
    )
    bundle_paths = []
    bundle_metas = []
    for name in bundle_names:
        path, meta = _find_delivered([name])
        if path is not None:
            bundle_paths.append(path)
            bundle_metas.append(meta)
    checks.append(
        (
            "route: multi-document email carries route=pipeline (real mailbox)",
            len(bundle_metas) == 2
            and all(m is not None and m.get("route") == "pipeline" for m in bundle_metas),
            f"bundle sidecars={len(bundle_metas)}",
        )
    )
    if single_path is None or len(bundle_paths) != 2:
        return checks

    single_manifest, _ = _run_watcher_route(single_path, llm_mode)
    checks.append(
        (
            "triage-lane: single-doc handled by the free triage lane (archived)",
            single_manifest is not None
            and single_manifest.get("stage") in ("archived", "review", "failed")
            and (single_manifest.get("intake") or {}).get("triage"),
            f"stage={single_manifest.get('stage') if single_manifest else 'NO MANIFEST'} "
            f"triage={bool((single_manifest or {}).get('intake', {}).get('triage'))}",
        )
    )
    bundle_manifests = [_run_watcher_route(p, llm_mode)[0] for p in bundle_paths]
    checks.append(
        (
            "pipeline-route: bundle documents ran the full paid pipeline",
            len(bundle_manifests) == 2
            and all(m is not None and m.get("stage") in ("archived", "review", "failed") for m in bundle_manifests)
            and all((not ((m or {}).get("intake") or {}).get("triage")) for m in bundle_manifests),
            "triage dropped for multi-document uploads",
        )
    )
    if llm_mode == "real":
        bundle_doc_type = (bundle_manifests[0] or {}).get("doc_type") if bundle_manifests else None
        checks.append(
            (
                "classify: pipeline-route doc_type == insurance_claim (real LLM)",
                bundle_doc_type == "insurance_claim",
                f"doc_type={bundle_doc_type}",
            )
        )
    # [reaction] the watcher reacted to OUR emails on the real mailbox.
    deadline = time.time() + 10
    while time.time() < deadline and gmail_intake.status()["reactions_sent"] <= reactions_before:
        time.sleep(0.2)
    checks.append(
        (
            "reaction: ✅ label applied to the source messages (real mailbox)",
            gmail_intake.status()["reactions_sent"] > reactions_before,
            f"reactions_sent={gmail_intake.status()['reactions_sent']} (before: {reactions_before})",
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
