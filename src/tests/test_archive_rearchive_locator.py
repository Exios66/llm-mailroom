"""#250: a re-archive that lands at ``<stem>--<n>`` must stay locatable.

``_no_clobber_move_to_dir`` tries ``<name>``, ``<stem>--<doc_id>``, then
``<stem>--<n>``. Re-archiving a document whose bare name is taken by another
document and whose own ``--<doc_id>`` copy already exists stores the new bytes
under ``--<n>``; ``archived_file_path`` used to resolve only the older copy.
The older copy is never replaced (A-20), so the manifest records the new path.
"""

from __future__ import annotations

from pathlib import Path

from schemas.manifest import DocumentManifest, PipelineStage


def _manifest(doc_id: str) -> DocumentManifest:
    return DocumentManifest(
        doc_id=doc_id,
        matter_id="M1",
        original_filename="doc.pdf",
        doc_type="contract",
        stage=PipelineStage.ARCHIVED,
    )


def _archive(bins, base: Path, manifest: DocumentManifest, payload: bytes) -> Path:
    src = base / "doc.pdf"
    src.write_bytes(payload)
    # the same call shape the archivist uses
    return bins.move_to_archive(
        src, manifest.matter_id, manifest.doc_type, doc_id=manifest.doc_id, manifest=manifest
    )


def test_rearchive_numbered_copy_is_the_one_served(temp_base_dir):
    from pipeline import bins

    other = _manifest("other-doc")
    mine = _manifest("my-doc")
    archive = bins.archive_dir("M1", "contract")

    # ``other`` owns the bare name; ``mine`` lands at ``--<doc_id>``, then ``--1``.
    assert _archive(bins, temp_base_dir, other, b"other doc bytes") == archive / "doc.pdf"
    first = _archive(bins, temp_base_dir, mine, b"first archive")
    assert first.name == "doc--my-doc.pdf"
    assert bins.archived_file_path(mine) == first

    second = _archive(bins, temp_base_dir, mine, b"second archive")
    assert second.name == "doc--1.pdf"
    assert bins.archived_file_path(mine) == second
    assert bins.archived_file_path(mine).read_bytes() == b"second archive"

    # Nothing preserved was overwritten (A-20), including the other document's bytes.
    assert first.read_bytes() == b"first archive"
    assert (archive / "doc.pdf").read_bytes() == b"other doc bytes"
    assert bins.archived_file_path(other) == archive / "doc.pdf"


def test_manifest_without_recorded_location_resolves_as_before(temp_base_dir):
    from pipeline import bins

    archive = bins.archive_dir("M1", "contract")
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "doc--my-doc.pdf").write_bytes(b"owned")
    assert bins.archived_file_path(_manifest("my-doc")) == archive / "doc--my-doc.pdf"


def test_recorded_location_of_another_document_is_ignored(temp_base_dir):
    from pipeline import bins

    archive = bins.archive_dir("M1", "contract")
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "doc--other-doc.pdf").write_bytes(b"someone else")
    (archive / "doc--my-doc.pdf").write_bytes(b"mine")
    mine = _manifest("my-doc")
    mine.file_location = str(archive / "doc--other-doc.pdf")
    assert bins.archived_file_path(mine) == archive / "doc--my-doc.pdf"


def test_archive_document_records_and_persists_the_location(temp_base_dir):
    from agents.archivist import archive_document
    from pipeline import bins

    archive = bins.archive_dir("M1", "contract")
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "doc.pdf").write_bytes(b"other doc bytes")
    (archive / "doc--my-doc.pdf").write_bytes(b"first archive")
    mine = _manifest("my-doc")

    src = temp_base_dir / "doc.pdf"
    src.write_bytes(b"second archive")
    path, _entry = archive_document(mine, src)

    assert path.name == "doc--1.pdf"
    stored = bins.load_manifest("my-doc")
    assert stored.file_location == str(path)
    assert bins.archived_file_path(stored).read_bytes() == b"second archive"
