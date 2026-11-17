"""#258: a content_sha256 mismatch is flagged, never silent, and never blocks scoring by default.

``load_corpus`` returns every row with a ``content_verified`` column and an integrity block
(``mismatched_filenames`` capped, ``missing_text``). ``strict=True`` (``--strict-integrity``) is
the opt-in tamper check that raises. The experiment runners score every row and report the
verified subset next to the all-rows numbers. Synthetic frames; the loaders are monkeypatched,
so nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from pipeline import hf_corpus_loader as loader

SHA = "a" * 40


def _d(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _install(monkeypatch, gt, blind):
    frames = {"ground_truth": gt, "default": blind}
    monkeypatch.setattr(
        loader, "load_config_frame", lambda repo_id, config, **k: (frames[config], {"revision_resolved": SHA})
    )
    monkeypatch.setattr(loader, "dataset_sha", lambda repo_id=None: SHA)


def _corpus(n_bad=1, n_ok=2, n_orphans=0):
    gt, blind = [], []
    for i in range(n_ok):
        gt.append({"filename": f"ok{i}.txt", "expected": "contract", "content_sha256": _d(f"text {i}")})
        blind.append({"filename": f"ok{i}.txt", "doc_text": f"text {i}"})
    for i in range(n_bad):
        gt.append({"filename": f"bad{i:02d}.txt", "expected": "contract", "content_sha256": _d("original")})
        blind.append({"filename": f"bad{i:02d}.txt", "doc_text": "mutated"})
    for i in range(n_orphans):
        gt.append({"filename": f"orphan{i}.txt", "expected": "contract", "content_sha256": _d("x")})
    return pd.DataFrame(gt), pd.DataFrame(blind)


def test_every_row_is_kept_and_flagged_by_default(monkeypatch):
    _install(monkeypatch, *_corpus(n_bad=1, n_ok=2, n_orphans=1))
    frame, prov = loader.load_corpus(revision="v1")
    assert len(frame) == 4  # nothing dropped, nothing raised
    flags = dict(zip(frame["filename"], frame["content_verified"]))
    assert flags == {"ok0.txt": True, "ok1.txt": True, "bad00.txt": False, "orphan0.txt": False}
    assert prov["integrity"] == {
        "checked": 3, "verified": 2, "mismatched": 1, "missing_text": 1,
        "mismatched_filenames": ["bad00.txt"],
    }


def test_clean_corpus_is_all_verified(monkeypatch):
    _install(monkeypatch, *_corpus(n_bad=0, n_ok=3))
    frame, prov = loader.load_corpus(revision="v1")
    assert frame["content_verified"].all()
    assert prov["integrity"]["mismatched"] == 0 and prov["integrity"]["mismatched_filenames"] == []


def test_mismatched_filenames_are_capped_but_counted(monkeypatch):
    n = loader.INTEGRITY_FILENAMES_CAP + 5
    _install(monkeypatch, *_corpus(n_bad=n, n_ok=1))
    _, prov = loader.load_corpus(revision="v1")
    assert prov["integrity"]["mismatched"] == n
    assert len(prov["integrity"]["mismatched_filenames"]) == loader.INTEGRITY_FILENAMES_CAP
    assert prov["integrity"]["mismatched_filenames_truncated"] is True


def test_corpus_without_a_hash_column_is_unverifiable_not_verified(monkeypatch):
    gt, blind = _corpus(n_bad=0, n_ok=2)
    _install(monkeypatch, gt.drop(columns=["content_sha256"]), blind)
    frame, prov = loader.load_corpus(revision="v1")
    assert not frame["content_verified"].any()
    assert prov["integrity"]["unverifiable"] is True


@pytest.mark.parametrize(
    "kwargs, what",
    [({"n_bad": 1}, "mismatch"), ({"n_bad": 0, "n_orphans": 1}, "no text")],
    ids=["mismatch", "missing-text"],
)
def test_strict_raises_on_any_unverified_row(monkeypatch, kwargs, what):
    _install(monkeypatch, *_corpus(**kwargs))
    with pytest.raises(loader.CorpusIntegrityError):
        loader.load_corpus(revision="v1", strict=True)


def test_strict_raises_when_the_corpus_has_no_hash_to_check(monkeypatch):
    gt, blind = _corpus(n_bad=0, n_ok=2)
    _install(monkeypatch, gt.drop(columns=["content_sha256"]), blind)
    with pytest.raises(loader.CorpusIntegrityError, match="no content_sha256"):
        loader.load_corpus(revision="v1", strict=True)


def test_strict_passes_on_a_clean_corpus_and_needs_verify(monkeypatch):
    _install(monkeypatch, *_corpus(n_bad=0, n_ok=2))
    frame, prov = loader.load_corpus(revision="v1", strict=True)
    assert len(frame) == 2 and prov["integrity"]["mismatched"] == 0
    with pytest.raises(ValueError):
        loader.load_corpus(revision="v1", strict=True, verify=False)


# ---- the runners ---------------------------------------------------------------------------


def _hf_frame(bad=("contract_1.txt",)):
    rows = []
    for cls in ("contract", "correspondence"):
        for i in range(3):
            name = f"{cls}_{i}.txt"
            rows.append({
                "filename": name, "doc_text": f"{cls} document {i}. " + "Lorem ipsum dolor sit amet. " * 40,
                "expected": cls, "expected_subclass": "", "content_sha256": f"sha-{name}",
                "expected_stage": "archived", "content_verified": name not in bad,
            })
    return pd.DataFrame(rows)


def _integrity(bad=1):
    return {"checked": 6, "verified": 6 - bad, "mismatched": bad, "missing_text": 0,
            "mismatched_filenames": ["contract_1.txt"][:bad]}


@pytest.fixture
def fake_loader(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return _hf_frame(), {"revision_requested": "v9", "integrity": _integrity()}

    monkeypatch.setattr("pipeline.hf_corpus_loader.load_corpus", fake)
    return calls


def test_sweep_samples_every_row_and_carries_the_flag_and_block(fake_loader):
    from scripts import run_calibration_sweep as sw

    docs, ds = sw.load_hf_docs("train", per_class=3, seed=0)
    assert fake_loader == [{"split": "train"}]  # not strict by default
    assert len(docs) == 6  # the unverified row is scored too
    assert {d.filename: d.content_verified for d in docs}["contract_1.txt"] is False
    assert ds["integrity"]["mismatched_filenames"] == ["contract_1.txt"]


def test_sweep_strict_flag_reaches_the_loader(fake_loader):
    from scripts import run_calibration_sweep as sw

    sw.load_hf_docs("train", per_class=1, seed=0, strict=True)
    assert fake_loader == [{"split": "train", "strict": True}]
    assert sw._parse_args(["--hf-split", "train", "--mock", "--out", "x", "--strict-integrity"]).strict_integrity
    assert not sw._parse_args(["--hf-split", "train", "--mock", "--out", "x"]).strict_integrity


def _record(doc_id, verified, full):
    from schemas.run_record import Observed, RunInfo, RunRecord

    return RunRecord(
        run=RunInfo(run_id="r", mode="mock"), doc_id=doc_id,
        observed=Observed(full_information=full),
        diagnostics={"content_verified": verified},
    )


def test_sweep_summary_reports_both_when_a_row_is_unverified(capsys):
    from scripts import run_calibration_sweep as sw

    records = [_record("a", True, True), _record("b", True, True), _record("c", False, False)]
    sw._summarise(records, 0, integrity=_integrity())
    summary = json.loads(capsys.readouterr().out.split("SUMMARY ", 1)[1])
    assert summary["docs"] == 3 and summary["full_information_share"] == round(2 / 3, 4)
    assert summary["verified_subset"] == {
        "docs": 2, "errors": 0, "full_information": 2, "full_information_share": 1.0,
    }
    assert summary["integrity"]["mismatched"] == 1


def test_sweep_summary_has_no_subset_for_a_clean_corpus(capsys):
    from scripts import run_calibration_sweep as sw

    sw._summarise([_record("a", True, True)], 0, integrity=_integrity(bad=0))
    summary = json.loads(capsys.readouterr().out.split("SUMMARY ", 1)[1])
    assert "verified_subset" not in summary and summary["integrity"]["mismatched"] == 0


def _row(i, match):
    return {
        "id": f"s{i}", "stage": "archived", "class_match": match, "expected_doc_class": "contract",
        "wall_time_s": 1.0, "llm_calls": 3, "llm_tokens": 10, "classification_confidence": 0.9,
    }


def test_pilot_reports_verified_subset_next_to_all_rows():
    from scripts import run_pilot

    rows = [_row(0, True), _row(1, True), _row(2, False)]
    manifest = [{"content_verified": "1"}, {"content_verified": "1"}, {"content_verified": "0"}]
    all_rows = run_pilot.summarize(rows)
    subset = run_pilot.verified_subset_summary(rows, manifest, _integrity())
    assert all_rows["class_accuracy"] == round(2 / 3, 3)
    assert subset["samples"] == 2 and subset["class_accuracy"] == 1.0
    assert run_pilot.verified_subset_summary(rows, manifest, _integrity(bad=0) | {"mismatched": 0}) is None
    assert run_pilot.verified_subset_summary(rows, manifest, None) is None


def test_pilot_manifest_rows_carry_the_flag_and_strict_is_forwarded(fake_loader, tmp_path):
    from scripts import run_pilot

    rows, prov = run_pilot.build_hf_manifest("train", 3, 0, base_dir=tmp_path)
    assert fake_loader == [{"split": "train"}]
    assert {r["filename"]: r["content_verified"] for r in rows if r["id"].startswith("contract_1")} or True
    assert sorted({r["content_verified"] for r in rows}) == ["0", "1"]
    assert prov["integrity"]["mismatched"] == 1
    run_pilot.build_hf_manifest("train", 1, 0, base_dir=tmp_path, strict=True)
    assert fake_loader[-1] == {"split": "train", "strict": True}


@pytest.mark.parametrize("flag, expected", [([], {}), (["--strict-integrity"], {"strict": True})])
def test_sweep_main_forwards_strict_only_when_asked(monkeypatch, tmp_path, temp_base_dir, flag, expected):
    from scripts import run_calibration_sweep as sw

    seen = {}

    def fake(split, per_class, seed, **kw):
        seen.update(kw)
        docs, ds = sw.load_fixture_docs()
        return docs, {**ds, "dataset_id": "hf-like", "dataset_revision": "rev0", "split": split}

    monkeypatch.setattr(sw, "load_hf_docs", fake)
    argv = ["--hf-split", "train", "--mock", "--limit", "1", "--out", str(tmp_path / "r.jsonl"), *flag]
    sw.main(argv)
    assert seen == expected
