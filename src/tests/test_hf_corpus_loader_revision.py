"""Revision enforcement, shard merging and join diagnostics of the HF corpus
loader (PR #202 review threads: pinned loads must be revision-aware, every
shard must load, unmatched ground-truth filenames must be reported).

Hermetic: every Hub / Viewer call is monkeypatched; nothing touches the network.
"""

from __future__ import annotations

import hashlib
import io
import sys

import pandas as pd
import pytest

from pipeline import hf_corpus_loader as loader

REPO = "org/ds"
SHA = "a" * 40


def _parquet(rows: list[dict]) -> bytes:
    return pd.DataFrame(rows).to_parquet()


def _card(configs: list[dict], siblings: list[str], sha: str = SHA) -> dict:
    return {
        "sha": sha,
        "siblings": [{"rfilename": f} for f in siblings],
        "cardData": {"configs": configs},
    }


def _install_hub(monkeypatch, *, info: dict, blobs: dict[str, bytes], tag: str = "v1"):
    """Fake Hub: the revision endpoint plus ``resolve/<sha>/<path>`` downloads.
    The Viewer entry points explode so any use of them fails the test."""
    fetched: list[str] = []

    def fake_json(url, **_):
        if url == f"{loader.HUB_BASE}/api/datasets/{REPO}/revision/{tag}":
            return info
        raise RuntimeError(f"unexpected JSON GET {url}")

    def fake_cached(url, label):
        fetched.append(url)
        return blobs[url]

    def viewer_forbidden(*a, **k):
        raise AssertionError("the Viewer must not be used for a pinned load")

    monkeypatch.setattr(loader, "_http_get_json", fake_json)
    monkeypatch.setattr(loader, "_cached_get_bytes", fake_cached)
    monkeypatch.setattr(loader, "parquet_urls", viewer_forbidden)
    monkeypatch.setattr(loader, "_rows_ladder", viewer_forbidden)
    return fetched


def _url(path: str, sha: str = SHA) -> str:
    return f"{loader.HUB_BASE}/datasets/{REPO}/resolve/{sha}/{path}"


# ---------------------------------------------------------------------------
# XKK6: a pinned load is served from the pinned revision, never the Viewer


def test_pinned_load_reads_resolved_commit_not_viewer(temp_base_dir, monkeypatch):
    info = _card(
        [{"config_name": "ground_truth", "data_files": [{"split": "train", "path": "p/gt/train/*"}]}],
        ["README.md", "p/gt/train/train-00000-of-00001.parquet"],
    )
    path = "p/gt/train/train-00000-of-00001.parquet"
    fetched = _install_hub(monkeypatch, info=info, blobs={_url(path): _parquet([{"filename": "a"}])})
    frame, prov = loader.load_config_frame(REPO, "ground_truth", revision="v1")
    assert list(frame["filename"]) == ["a"]
    assert fetched == [_url(path)]  # immutable commit URL, not the tag
    assert prov["revision"] == "v1"
    assert prov["revision_resolved"] == SHA
    assert prov["strategy"] == "hub_resolve"


def test_pinned_load_fails_loudly_when_revision_unresolvable(temp_base_dir, monkeypatch):
    def dead(url, **_):
        raise RuntimeError("hub down")

    def viewer_forbidden(*a, **k):
        raise AssertionError("pinned load fell back to the Viewer")

    monkeypatch.setattr(loader, "_http_get_json", dead)
    monkeypatch.setattr(loader, "parquet_urls", viewer_forbidden)
    monkeypatch.setattr(loader, "_rows_ladder", viewer_forbidden)
    with pytest.raises(RuntimeError, match="cannot resolve revision 'v1'"):
        loader.load_config_frame(REPO, "ground_truth", revision="v1")


def test_pinned_load_fails_when_config_not_declared_at_revision(temp_base_dir, monkeypatch):
    info = _card(
        [{"config_name": "default", "data_files": [{"split": "train", "path": "d/*"}]}],
        ["d/train-00000-of-00001.parquet"],
    )
    _install_hub(monkeypatch, info=info, blobs={})
    with pytest.raises(RuntimeError, match="no data files"):
        loader.load_config_frame(REPO, "ground_truth", revision="v1")


def test_rows_ladder_refuses_a_pinned_revision():
    with pytest.raises(ValueError, match="cannot enforce"):
        loader._rows_ladder(REPO, "default", split="train", revision="v1")


def test_load_corpus_rejects_mixed_revisions(temp_base_dir, monkeypatch):
    shas = iter(["1" * 40, "2" * 40])

    def fake_frame(repo_id, config, *, split="train", revision=None, max_rows=None):
        return pd.DataFrame([{"filename": "a", "doc_text": "t"}]), {"revision_resolved": next(shas)}

    monkeypatch.setattr(loader, "load_config_frame", fake_frame)
    monkeypatch.setattr(loader, "dataset_sha", lambda repo_id=None: "1" * 40)
    with pytest.raises(RuntimeError, match="different commits"):
        loader.load_corpus(revision="v1")


def test_hub_tip_drift_compares_resolved_sha_not_the_tag(temp_base_dir, monkeypatch, caplog):
    """A tag pin never equals a sha; drift is tip vs the pin's resolved commit."""

    def fake_frame(repo_id, config, *, split="train", revision=None, max_rows=None):
        return pd.DataFrame([{"filename": "a", "doc_text": "t"}]), {"revision_resolved": SHA}

    monkeypatch.setattr(loader, "load_config_frame", fake_frame)
    monkeypatch.setattr(loader, "dataset_sha", lambda repo_id=None: SHA)
    _, prov = loader.load_corpus(revision="v1")
    assert prov["revision_resolved"] == SHA
    assert "hub_tip_drift" not in prov

    monkeypatch.setattr(loader, "dataset_sha", lambda repo_id=None: "b" * 40)
    with caplog.at_level("WARNING", logger="llm_mailroom.hf_corpus_loader"):
        _, prov = loader.load_corpus(revision="v1")
    assert prov["hub_tip_drift"] == {"tip": "b" * 40, "pinned": SHA}


# ---------------------------------------------------------------------------
# XKMs: every shard loads


def test_pinned_load_concatenates_every_shard_in_order(temp_base_dir, monkeypatch):
    paths = [f"p/gt/train/train-0000{i}-of-00003.parquet" for i in range(3)]
    info = _card(
        [{"config_name": "ground_truth", "data_files": [{"split": "train", "path": "p/gt/train/train-*.parquet"}]}],
        ["README.md", *reversed(paths), "p/gt/test/test-00000-of-00001.parquet"],
    )
    blobs = {_url(p): _parquet([{"filename": f"f{i}a"}, {"filename": f"f{i}b"}]) for i, p in enumerate(paths)}
    _install_hub(monkeypatch, info=info, blobs=blobs)
    frame, prov = loader.load_config_frame(REPO, "ground_truth", revision="v1")
    assert list(frame["filename"]) == ["f0a", "f0b", "f1a", "f1b", "f2a", "f2b"]
    assert prov["shards"] == 3 and prov["num_rows"] == 6
    assert prov["source_urls"] == [_url(p) for p in paths]


def test_max_rows_stops_fetching_shards_and_cuts_the_frame(temp_base_dir, monkeypatch):
    paths = [f"d/train-0000{i}-of-00003.parquet" for i in range(3)]
    info = _card(
        [{"config_name": "default", "data_files": [{"split": "train", "path": "d/*"}]}], paths
    )
    blobs = {_url(p): _parquet([{"n": 3 * i}, {"n": 3 * i + 1}, {"n": 3 * i + 2}]) for i, p in enumerate(paths)}
    fetched = _install_hub(monkeypatch, info=info, blobs=blobs)
    frame, prov = loader.load_config_frame(REPO, "default", revision="v1", max_rows=4)
    assert list(frame["n"]) == [0, 1, 2, 3]
    assert len(fetched) == 2 and prov["shards"] == 2 and prov["shards_total"] == 3


def test_unpinned_viewer_ladder_concatenates_every_shard(temp_base_dir, monkeypatch):
    listing = {
        "parquet_files": [
            {"split": "train", "config": "default", "filename": "1-of-2.parquet", "url": "https://v/1"},
            {"split": "train", "config": "default", "filename": "0-of-2.parquet", "url": "https://v/0"},
            {"split": "train", "config": "other", "filename": "0.parquet", "url": "https://v/o"},
            {"split": "test", "config": "default", "filename": "t.parquet", "url": "https://v/t"},
        ]
    }
    monkeypatch.setattr(loader, "_http_get_json", lambda url, **_: listing)
    blobs = {"https://v/0": _parquet([{"n": 0}]), "https://v/1": _parquet([{"n": 1}])}
    monkeypatch.setattr(loader, "_cached_get_bytes", lambda url, label: blobs[url])
    assert loader.parquet_urls(REPO) == {
        "default": ["https://v/0", "https://v/1"],
        "other": ["https://v/o"],
    }
    frame, prov = loader.load_config_frame(REPO, "default")
    assert list(frame["n"]) == [0, 1]
    assert prov["revision"] is None and prov["revision_resolved"] is None
    assert prov["shards"] == 2


def test_revision_data_files_without_a_card(temp_base_dir):
    base = {"sha": SHA, "configs": None}
    assert loader.revision_data_files(
        {**base, "siblings": ["README.md", "manifest.json", "train.jsonl", "test.jsonl"]}, "default", "train"
    ) == ["train.jsonl"]
    assert loader.revision_data_files(
        {**base, "siblings": ["README.md", "all.jsonl"]}, "default", "train"
    ) == ["all.jsonl"]
    assert loader.revision_data_files({**base, "siblings": ["all.jsonl"]}, "ground_truth", "train") == []


def test_pinned_load_reads_jsonl_files(temp_base_dir, monkeypatch):
    info = {"sha": SHA, "siblings": [{"rfilename": "train.jsonl"}], "cardData": {}}
    blobs = {_url("train.jsonl"): b'{"filename": "a"}\n{"filename": "b"}\n'}
    _install_hub(monkeypatch, info=info, blobs=blobs)
    frame, _ = loader.load_config_frame(REPO, "default", revision="v1")
    assert list(frame["filename"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# XKMw: ground-truth rows with no matching text are reported


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _patch_frames(monkeypatch, gt: pd.DataFrame, blind: pd.DataFrame):
    frames = {"ground_truth": gt, "default": blind}
    monkeypatch.setattr(
        loader,
        "load_config_frame",
        lambda repo_id, config, **k: (frames[config], {"revision_resolved": SHA}),
    )
    monkeypatch.setattr(loader, "dataset_sha", lambda repo_id=None: SHA)


def test_unmatched_ground_truth_filename_is_reported(temp_base_dir, monkeypatch, caplog):
    gt = pd.DataFrame(
        [
            {"filename": "a.txt", "expected": "contract", "content_sha256": _digest("alpha")},
            {"filename": "orphan.txt", "expected": "contract", "content_sha256": _digest("zzz")},
        ]
    )
    blind = pd.DataFrame([{"filename": "a.txt", "doc_text": "alpha"}])
    _patch_frames(monkeypatch, gt, blind)
    with caplog.at_level("WARNING", logger="llm_mailroom.hf_corpus_loader"):
        merged, prov = loader.load_corpus(revision="v1")
    assert prov["unmatched_text"] == 1
    # The integrity block still counts only rows that have text.
    assert prov["integrity"] == {"checked": 1, "verified": 1, "mismatched": 0}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("1 of 2" in m and "no default row" in m for m in msgs)
    assert not any("content_sha256 mismatch" in m for m in msgs)
    assert merged["doc_text"].isna().sum() == 1


def test_fully_matched_corpus_has_no_unmatched_warning(temp_base_dir, monkeypatch, caplog):
    gt = pd.DataFrame([{"filename": "a.txt", "expected": "contract", "content_sha256": _digest("alpha")}])
    blind = pd.DataFrame([{"filename": "a.txt", "doc_text": "alpha"}])
    _patch_frames(monkeypatch, gt, blind)
    with caplog.at_level("WARNING", logger="llm_mailroom.hf_corpus_loader"):
        _, prov = loader.load_corpus(revision="v1")
    assert prov["unmatched_text"] == 0
    assert not any("no default row" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# XKMn: the pilot's fallback honors the pin


@pytest.fixture
def no_datasets_lib(monkeypatch):
    monkeypatch.setitem(sys.modules, "datasets", None)  # `import datasets` -> ImportError


def test_pilot_fallback_loads_the_pinned_revision(no_datasets_lib, monkeypatch):
    from scripts import run_hf_pilot as pilot

    calls: list[dict] = []

    def fake_frame(repo_id, config, *, split="train", revision=None, max_rows=None):
        calls.append({"repo": repo_id, "config": config, "split": split, "revision": revision, "max_rows": max_rows})
        return (
            pd.DataFrame(
                [{"filename": "a.htm", "expected": "contract", "expected_subclass": "x", "cuad_clause_labels": None}]
            ),
            {"revision_resolved": SHA},
        )

    monkeypatch.setattr("pipeline.hf_corpus_loader.load_config_frame", fake_frame)
    monkeypatch.setattr(
        pilot, "_paginate_viewer", lambda **kw: (_ for _ in ()).throw(AssertionError("Viewer used under a pin"))
    )
    labels = pilot.load_ground_truth_labels(split="train", max_scan=50)
    assert labels["a.htm"]["expected"] == "contract"
    corpus = pilot.active_corpus()
    assert calls == [
        {
            "repo": corpus["id"],
            "config": corpus["gt_config"],
            "split": "train",
            "revision": corpus["revision"],
            "max_rows": 50,
        }
    ]
    assert corpus["revision"]


def test_pilot_fallback_fails_instead_of_reading_viewer_when_pin_unenforceable(no_datasets_lib, monkeypatch):
    from scripts import run_hf_pilot as pilot

    def boom(*a, **k):
        raise RuntimeError("cannot resolve revision")

    monkeypatch.setattr("pipeline.hf_corpus_loader.load_config_frame", boom)
    monkeypatch.setattr(
        pilot, "_paginate_viewer", lambda **kw: (_ for _ in ()).throw(AssertionError("Viewer used under a pin"))
    )
    with pytest.raises(RuntimeError, match="cannot resolve revision"):
        pilot.load_hf_rows(split="train", max_scan=10)


def test_pilot_fallback_normalizes_numpy_cells(no_datasets_lib, monkeypatch):
    from scripts import run_hf_pilot as pilot

    frame = pd.DataFrame([{"filename": "a", "labels": [1, 2], "note": float("nan")}])
    frame["labels"] = frame["labels"].map(lambda v: __import__("numpy").array(v))
    monkeypatch.setattr("pipeline.hf_corpus_loader.load_config_frame", lambda *a, **k: (frame, {}))
    rows = pilot._fallback_rows(split="train", max_scan=0, config="default")
    assert rows == [{"filename": "a", "labels": [1, 2], "note": None}]


def test_pilot_unpinned_corpus_still_uses_viewer(monkeypatch):
    from scripts import run_hf_pilot as pilot

    monkeypatch.setattr(pilot, "active_corpus", lambda: {"id": "x/y", "revision": None})
    monkeypatch.setattr(pilot, "_paginate_viewer", lambda **kw: [{"filename": "v"}])
    assert pilot._fallback_rows(split="train", max_scan=5, config="default") == [{"filename": "v"}]
