"""HUB-043 — the watcher's already-processed dedup must be intake-provenance
aware: a re-sent email (new Message-ID) with an already-processed FILENAME is
a NEW document and must be claimed, never silently dropped."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.watcher import _is_already_processed
from schemas.manifest import DocumentManifest, PipelineStage
from pipeline.bins import inbox_dir, save_manifest, write_inbox_meta


@pytest.fixture
def inbox_file(temp_base_dir) -> Path:
    path = inbox_dir() / "same_name.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return path


def _terminal_manifest(filename: str, *, intake: dict | None = None) -> None:
    manifest = DocumentManifest(
        matter_id="M1",
        original_filename=filename,
        stage=PipelineStage.ARCHIVED,
        doc_type="insurance_claim",
        intake=intake,
    )
    save_manifest(manifest)


def test_no_sidecar_filename_match_is_skipped(inbox_file):
    """Legacy behavior preserved: plain inbox drops (no sidecar) dedup on
    filename."""
    _terminal_manifest("same_name.pdf", intake={"source": "upload", "upload_id": "old"})
    assert _is_already_processed(inbox_file) is True


def test_same_delivery_is_skipped(inbox_file):
    """The original dedup purpose: a crashed re-claim of the SAME delivery."""
    write_inbox_meta(
        inbox_file,
        matter_id="M1",
        source="gmail",
        message_id="<same-message@mail.gmail.com>",
        sender="exios4@gmail.com",
        route="triage",
    )
    _terminal_manifest(
        "same_name.pdf",
        intake={"source": "gmail", "message_id": "<same-message@mail.gmail.com>"},
    )
    assert _is_already_processed(inbox_file) is True


def test_resent_email_new_message_id_must_process(inbox_file):
    """THE BUG: a new email (new Message-ID) with an already-processed
    filename was silently dropped forever. It is a NEW document."""
    write_inbox_meta(
        inbox_file,
        matter_id="M1",
        source="gmail",
        message_id="<brand-new@mail.gmail.com>",
        sender="axios337@gmail.com",
        route="triage",
    )
    _terminal_manifest(
        "same_name.pdf",
        intake={"source": "gmail", "message_id": "<older-message@mail.gmail.com>"},
    )
    assert _is_already_processed(inbox_file) is False


def test_new_upload_id_must_process(inbox_file):
    """Same law for inbox/CLI uploads: a fresh upload_id is a new document."""
    write_inbox_meta(
        inbox_file,
        matter_id="M1",
        source="upload",
        upload_id="fresh-upload-id",
        route="pipeline",
    )
    _terminal_manifest(
        "same_name.pdf",
        intake={"source": "upload", "upload_id": "older-upload-id"},
    )
    assert _is_already_processed(inbox_file) is False


def test_sidecar_with_no_matching_manifest_processes(inbox_file):
    """First delivery of this identity (older manifests pre-date intake
    awareness or belong to other deliveries) ⇒ claim."""
    _terminal_manifest("same_name.pdf")  # legacy manifest, no intake
    write_inbox_meta(
        inbox_file,
        matter_id="M1",
        source="gmail",
        message_id="<first-delivery@mail.gmail.com>",
        sender="jjburleson@wisc.edu",
        route="triage",
    )
    assert _is_already_processed(inbox_file) is False


def test_nonterminal_manifests_never_block(inbox_file):
    """Only terminal stages dedup — an in-flight old run never blocks a new
    delivery (the reconcile path owns crashed claims)."""
    manifest = DocumentManifest(
        matter_id="M1",
        original_filename="same_name.pdf",
        stage=PipelineStage.PROCESSING,
        intake={"source": "gmail", "message_id": "<same@mail.gmail.com>"},
    )
    save_manifest(manifest)
    write_inbox_meta(
        inbox_file,
        matter_id="M1",
        source="gmail",
        message_id="<same@mail.gmail.com>",
        sender="exios4@gmail.com",
        route="triage",
    )
    assert _is_already_processed(inbox_file) is False
