"""#200 lean cleanup: dead names stay gone, and the intake fallback never ingests a sidecar."""

from __future__ import annotations

import pytest


def test_removed_dead_names_stay_removed():
    import pipeline.bins as bins
    import pipeline.config as config

    assert not hasattr(bins, "move_to_classified")
    assert not hasattr(bins, "SUPPORTED_EXTENSIONS")
    assert not hasattr(config, "load_taxonomy")


def test_intake_fallback_skips_meta_sidecar(temp_base_dir):
    """With no ``file_path``, intake picks from the inbox through ``list_inbox_files``.

    A leftover ``<file>.pdf.meta`` sidecar is not a document, so an inbox holding only
    a sidecar has nothing to ingest.
    """
    from graph.build_graph import intake_node
    from pipeline.bins import inbox_dir

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "contract.pdf.meta").write_text('{"matter_id": "M-1"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="No files in inbox"):
        intake_node({})
