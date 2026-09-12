"""§45 evaluation-trace identity (HUB-022): dataset_name + dataset_revision +
taxonomy_version ride on every evaluation trace's metadata."""
from __future__ import annotations

from graph.build_graph import dataset_trace_metadata


def test_dataset_trace_metadata_full_identity():
    meta = dataset_trace_metadata(
        {
            "name": "Lucius-Morningstar/mailroom-dataset",
            "revision": "a706784419c37e57930fe17fc7ca0d7ee6672f0f",
            "taxonomy_version": "v9",
        }
    )
    assert meta == {
        "dataset_name": "Lucius-Morningstar/mailroom-dataset",
        "dataset_revision": "a706784419c37e57930fe17fc7ca0d7ee6672f0f",
        "taxonomy_version": "v9",
    }


def test_dataset_trace_metadata_empty_when_unset_or_partial():
    assert dataset_trace_metadata(None) == {}
    assert dataset_trace_metadata({}) == {}
    # partial identity: only the provided keys ride; nothing fabricated
    assert dataset_trace_metadata({"name": "some/dataset"}) == {
        "dataset_name": "some/dataset"
    }
    assert dataset_trace_metadata({"revision": ""}) == {}


def test_dataset_trace_metadata_rejects_non_dict():
    assert dataset_trace_metadata(None) == {}
    assert dataset_trace_metadata("not-a-dict") == {}
    assert dataset_trace_metadata(7) == {}
