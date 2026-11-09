"""PR #202 review: terminal-bin movers and abort recovery.

- a claim that vanishes mid-scan is skipped, not fatal (XKL7);
- abort recovery is bound to THIS run's own claim (XKMI);
- the selected failed-bin path is persisted and preferred by the locator (XKMW);
- publication works on filesystems without hard links (XKMh).
"""

import errno
import os
import time
from pathlib import Path

import pytest


def _claim_dirs(bins, *workers):
    for worker in workers:
        bins.processing_dir(worker).mkdir(parents=True, exist_ok=True)


class TestClaimScanSkipsVanishedCandidates:
    """The file exists at the ``is_file`` check and is gone at ``stat``."""

    @staticmethod
    def _vanish_after_exists_check(monkeypatch, target_name):
        real_is_file = Path.is_file
        real_stat = Path.stat
        checked = set()

        def is_file(self, *args, **kwargs):
            result = real_is_file(self, *args, **kwargs)
            if self.name == target_name and result:
                checked.add(self)
            return result

        def stat(self, *args, **kwargs):
            if self in checked and self.name == target_name:
                checked.discard(self)
                raise FileNotFoundError(errno.ENOENT, "vanished", str(self))
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", is_file)
        monkeypatch.setattr(Path, "stat", stat)

    def test_stat_race_is_skipped(self, temp_base_dir, monkeypatch):
        from pipeline import bins

        _claim_dirs(bins, "w1")
        (bins.processing_dir("w1") / "claim.pdf").write_bytes(b"x")
        self._vanish_after_exists_check(monkeypatch, "claim.pdf")
        assert bins.find_claimed_file("claim.pdf") is None

    def test_stat_race_keeps_other_candidate(self, temp_base_dir, monkeypatch):
        from pipeline import bins

        _claim_dirs(bins, "w1", "w2")
        (bins.processing_dir("w1") / "claim.pdf").write_bytes(b"a")
        (bins.processing_dir("w2") / "claim.pdf").write_bytes(b"b")
        real_is_file = Path.is_file
        real_stat = Path.stat
        doomed = bins.processing_dir("w1") / "claim.pdf"
        armed = []

        def is_file(self, *args, **kwargs):
            result = real_is_file(self, *args, **kwargs)
            if self == doomed and result:
                armed.append(self)
            return result

        def stat(self, *args, **kwargs):
            if self == doomed and armed:
                armed.clear()
                raise FileNotFoundError(errno.ENOENT, "vanished", str(self))
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", is_file)
        monkeypatch.setattr(Path, "stat", stat)
        assert bins.find_claimed_file("claim.pdf") == bins.processing_dir("w2") / "claim.pdf"


class TestAbortBoundToOwnClaim:
    def test_recorded_claim_beats_newer_same_name_claim(self, temp_base_dir):
        from pipeline import bins

        inbox = bins.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        mine_src = inbox / "claim.pdf"
        mine_src.write_bytes(b"mine")
        mine = bins.claim_file(mine_src, "worker-mine")

        _claim_dirs(bins, "worker-other")
        other = bins.processing_dir("worker-other") / "claim.pdf"
        other.write_bytes(b"other run bytes")
        time.sleep(0.01)
        other.touch()  # heartbeat: the other run's claim is now the newest
        assert other.stat().st_mtime > mine.stat().st_mtime

        assert bins.find_claimed_file("claim.pdf", source_path=mine_src) == mine
        # Name-only lookup (no source known) still resolves by newest mtime.
        assert bins.find_claimed_file("claim.pdf") == other

    def test_recorded_claim_gone_never_substitutes_another_worker(self, temp_base_dir):
        from pipeline import bins

        inbox = bins.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        src = inbox / "claim.pdf"
        src.write_bytes(b"mine")
        mine = bins.claim_file(src, "worker-mine")
        _claim_dirs(bins, "worker-other")
        (bins.processing_dir("worker-other") / "claim.pdf").write_bytes(b"other")
        mine.unlink()
        assert bins.find_claimed_file("claim.pdf", source_path=src) is None

    def test_unrecorded_source_falls_back_to_name(self, temp_base_dir):
        from pipeline import bins

        _claim_dirs(bins, "w1")
        claimed = bins.processing_dir("w1") / "claim.pdf"
        claimed.write_bytes(b"x")
        assert bins.find_claimed_file("claim.pdf", source_path=bins.inbox_dir() / "claim.pdf") == claimed

    def test_finalize_aborted_moves_own_file_not_other_runs(
        self, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        from graph.build_graph import _finalize_aborted
        from pipeline import bins

        inbox = bins.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        src = inbox / "claim.pdf"
        src.write_bytes(b"my bytes")
        mine = bins.claim_file(src, "worker-mine")

        _claim_dirs(bins, "worker-other")
        other = bins.processing_dir("worker-other") / "claim.pdf"
        other.write_bytes(b"other run bytes")
        time.sleep(0.01)
        other.touch()

        result = _finalize_aborted(
            {
                "doc_id": "own-claim-1",
                "matter_id": "M-OWN",
                "original_filename": "claim.pdf",
                "file_path": str(src),  # stale: claimed away from the inbox
                "trace_id": None,
            },
            "unit abort",
        )
        assert result["stage"] == "failed"
        assert not mine.exists()
        assert other.read_bytes() == b"other run bytes"  # untouched
        stored = list(bins.failed_dir().glob("claim*.pdf"))
        assert [p.read_bytes() for p in stored] == [b"my bytes"]


class TestFailedLocatorPrefersRecordedPath:
    def _occupy(self, bins):
        failed = bins.failed_dir()
        failed.mkdir(parents=True, exist_ok=True)
        (failed / "claim.pdf").write_bytes(b"first bytes")
        (failed / "claim--doc-9.pdf").write_bytes(b"older doc-id bytes")
        return failed

    def test_abort_persists_numbered_path_and_locator_uses_it(
        self, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        from graph.build_graph import _finalize_aborted
        from pipeline import bins
        from pipeline.review_resolve import locate_document_file

        failed = self._occupy(bins)
        live = bins.processing_dir("w1")
        live.mkdir(parents=True, exist_ok=True)
        claimed = live / "claim.pdf"
        claimed.write_bytes(b"new aborted bytes")

        _finalize_aborted(
            {
                "doc_id": "doc-9",
                "matter_id": "M-NUM",
                "original_filename": "claim.pdf",
                "file_path": str(claimed),
                "trace_id": None,
            },
            "unit abort",
        )
        manifest = bins.load_manifest("doc-9")
        assert manifest.file_location == str(failed / "claim--1.pdf")
        assert (failed / "claim--1.pdf").read_bytes() == b"new aborted bytes"
        assert bins.failed_file_path(manifest) == failed / "claim--1.pdf"
        assert locate_document_file(manifest) == failed / "claim--1.pdf"

    def test_locator_ignores_recorded_path_outside_failed_bin(self, temp_base_dir):
        from pipeline import bins
        from schemas.manifest import DocumentManifest, PipelineStage

        failed = bins.failed_dir()
        failed.mkdir(parents=True, exist_ok=True)
        stray = temp_base_dir / "elsewhere.pdf"
        stray.write_bytes(b"stray")
        manifest = DocumentManifest(
            doc_id="doc-1",
            matter_id="M",
            original_filename="a.pdf",
            stage=PipelineStage.FAILED,
            file_location=str(stray),
        )
        assert bins.failed_file_path(manifest) == failed / "a--doc-1.pdf"


class TestNoHardLinkFilesystems:
    @pytest.fixture
    def no_link(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise OSError(errno.EPERM, "hard links not supported")

        monkeypatch.setattr(os, "link", refuse)

    @pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EXDEV])
    def test_move_to_failed_without_links(self, temp_base_dir, monkeypatch, code):
        from pipeline import bins

        def refuse(*args, **kwargs):
            raise OSError(code, "link refused")

        monkeypatch.setattr(os, "link", refuse)
        src = temp_base_dir / "doc.pdf"
        src.write_bytes(b"payload")
        dest = bins.move_to_failed(src, "doc-1")
        assert dest == bins.failed_dir() / "doc.pdf"
        assert dest.read_bytes() == b"payload"
        assert not src.exists()
        assert [p.name for p in bins.failed_dir().iterdir()] == ["doc.pdf"]  # no temp left

    def test_move_to_archive_without_links(self, temp_base_dir, no_link):
        from pipeline import bins

        src = temp_base_dir / "doc.pdf"
        src.write_bytes(b"payload")
        dest = bins.move_to_archive(src, "M1", "contract", "doc-1")
        assert dest.read_bytes() == b"payload"
        assert not src.exists()

    def test_occupied_destination_is_never_overwritten(self, temp_base_dir, no_link):
        from pipeline import bins

        failed = bins.failed_dir()
        failed.mkdir(parents=True, exist_ok=True)
        (failed / "doc.pdf").write_bytes(b"first")
        (failed / "doc--doc-1.pdf").write_bytes(b"second")
        src = temp_base_dir / "doc.pdf"
        src.write_bytes(b"third")
        dest = bins.move_to_failed(src, "doc-1")
        assert dest.name == "doc--1.pdf"
        assert dest.read_bytes() == b"third"
        assert (failed / "doc.pdf").read_bytes() == b"first"
        assert (failed / "doc--doc-1.pdf").read_bytes() == b"second"
        assert not src.exists()
        assert sorted(p.name for p in failed.iterdir()) == ["doc--1.pdf", "doc--doc-1.pdf", "doc.pdf"]

    def test_direct_mover_raises_file_exists_and_keeps_source(self, temp_base_dir, no_link):
        from pipeline import bins

        dest = temp_base_dir / "dest.pdf"
        dest.write_bytes(b"keep")
        src = temp_base_dir / "src.pdf"
        src.write_bytes(b"new")
        with pytest.raises(FileExistsError):
            bins._move_review_file_no_clobber(src, dest)
        assert dest.read_bytes() == b"keep"
        assert src.read_bytes() == b"new"
        assert [p.name for p in temp_base_dir.iterdir() if p.name.startswith(".")] == []

    def test_claim_file_without_links(self, temp_base_dir, no_link):
        from pipeline import bins

        inbox = bins.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        src = inbox / "doc.pdf"
        src.write_bytes(b"payload")
        claimed = bins.claim_file(src, "w1")
        assert claimed.read_bytes() == b"payload"
        assert not src.exists()
