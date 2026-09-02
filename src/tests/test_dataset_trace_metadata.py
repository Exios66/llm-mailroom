"""§45 evaluation-trace identity (HUB-022): dataset_name + dataset_revision +
taxonomy_version ride on every evaluation trace's metadata."""
from __future__ import annotations

from graph.build_graph import dataset_trace_metadata


def test_dataset_trace_metadata_full_identity():
    meta = dataset_trace_metadata(
        {
            "name": "Lucius-Morningstar/mailroom-corpus",
            "revision": "bb57c5ad00333d239ea456fe3f2298c3ba5b5108",
            "taxonomy_version": "v7",
        }
    )
    assert meta == {
        "dataset_name": "Lucius-Morningstar/mailroom-corpus",
        "dataset_revision": "bb57c5ad00333d239ea456fe3f2298c3ba5b5108",
        "taxonomy_version": "v7",
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
