"""run_pilot.py --dataset hf: real-corpus manifest rows, no network.

``pipeline.hf_corpus_loader.load_corpus`` is monkeypatched with a small frame
that carries the real corpus column names. Nothing here touches the Hub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_pilot

CLASSES = ("contract", "correspondence", "corporate_record", "insurance_claim", "merger_agreement")


def _text(cls: str, i: int) -> str:
    # > 200 chars (the sweep sampler drops shorter rows), distinct per doc, and long
    # enough to prove no truncation at the 25k char cap run_hf_pilot uses elsewhere.
    body = f"{cls} document {i}. " + "The parties agree to the terms herein. " * 40
    if i == 0:
        body += "END-MARKER-" + "x" * 30000 + "-TAIL"
    return body


def _frame(per_class_rows: int = 4) -> pd.DataFrame:
    rows = []
    for cls in CLASSES:
        for i in range(per_class_rows):
            text = _text(cls, i)
            rows.append({
                "filename": f"{cls}_{i}.PDF",
                "expected": cls,
                "expected_subclass": "",
                "gt_fields": "{}",
                "expected_stage": "archived",
                "review_expected": "false",
                "content_sha256": f"sha-{cls}-{i}",
                "source_corpus": "sec_edgar",
                "doc_text": text,
            })
    return pd.DataFrame(rows)


@pytest.fixture
def fake_corpus(monkeypatch):
    from pipeline import hf_corpus_loader

    calls = []

    def fake_load_corpus(**kwargs):
        calls.append(kwargs)
        return _frame(), {"revision_requested": "v-test", "hub_sha_tip": "tip"}

    monkeypatch.setattr(hf_corpus_loader, "load_corpus", fake_load_corpus)
    return calls


def test_rows_have_the_keys_the_manifest_consumers_read(fake_corpus, tmp_path):
    rows, prov = run_pilot.build_hf_manifest("train", 1, 0, base_dir=tmp_path)
    assert fake_corpus == [{"split": "train"}]
    assert prov["dataset_id"] == "Lucius-Morningstar/mailroom-dataset"
    assert prov["dataset_revision"] == "v-test" and prov["split"] == "train"
    assert [r["expected_doc_class"] for r in rows] == sorted(CLASSES)
    for r in rows:
        for key in ("id", "subdir", "filename", "source", "dataset", "expected_doc_class",
                    "expected_stage", "size_tier", "expected_fields"):
            assert key in r
        assert r["subdir"] == "hf" and r["filename"].endswith(".txt")
        assert r["expected_stage"] == "archived"
        assert (tmp_path / "samples" / "hf" / r["filename"]).is_file()
        assert isinstance(json.loads(r["expected_fields"]), dict)
    # the manifest validator accepts every row (it rejects an empty expected_fields column)
    run_pilot._validate_manifest_ground_truth(rows)


def test_text_is_written_untruncated(fake_corpus, tmp_path):
    frame = _frame()
    rows, _ = run_pilot.build_hf_manifest("train", 4, 0, base_dir=tmp_path)
    assert len(rows) == 4 * len(CLASSES)
    originals = set(frame["doc_text"])
    long_seen = 0
    for r in rows:
        written = (tmp_path / "samples" / "hf" / r["filename"]).read_bytes().decode("utf-8")
        assert written in originals
        assert len(written) == int(r["chars"])
        long_seen += written.endswith("-TAIL")
    assert long_seen == len(CLASSES)  # the >30k char document of every class survived whole


def test_sampling_is_deterministic_per_class_and_seeded(fake_corpus, tmp_path):
    a, _ = run_pilot.build_hf_manifest("train", 2, 7, base_dir=tmp_path / "a")
    b, _ = run_pilot.build_hf_manifest("train", 2, 7, base_dir=tmp_path / "b")
    assert [r["filename"] for r in a] == [r["filename"] for r in b]
    for cls in CLASSES:
        assert sum(r["expected_doc_class"] == cls for r in a) == 2
    others = {tuple(r["filename"] for r in run_pilot.build_hf_manifest("train", 2, s, base_dir=tmp_path / f"s{s}")[0])
              for s in range(8)}
    assert len(others) > 1  # the seed matters


def test_per_class_larger_than_pool_returns_the_whole_class(fake_corpus, tmp_path):
    rows, _ = run_pilot.build_hf_manifest("train", 50, 0, base_dir=tmp_path)
    assert len(rows) == 4 * len(CLASSES)


def test_per_class_below_one_is_refused(fake_corpus, tmp_path):
    with pytest.raises(SystemExit, match="per-class"):
        run_pilot.build_hf_manifest("train", 0, 0, base_dir=tmp_path)


def test_colliding_inbox_names_stay_unique(monkeypatch, tmp_path):
    from pipeline import hf_corpus_loader

    extra = [dict(_frame().iloc[0].to_dict(), filename=name) for name in ("a.PDF", "a.htm")]
    frame = pd.DataFrame(extra)
    monkeypatch.setattr(hf_corpus_loader, "load_corpus", lambda **kw: (frame, {}))
    rows, _ = run_pilot.build_hf_manifest("train", 5, 0, base_dir=tmp_path)
    assert len({r["filename"] for r in rows}) == len(rows) == 2
    assert len({r["id"] for r in rows}) == 2


def test_hf_rows_count_as_real_and_synthetic_still_do_not(fake_corpus, tmp_path):
    rows, _ = run_pilot.build_hf_manifest("train", 1, 0, base_dir=tmp_path)
    assert run_pilot.filter_real_samples(rows, mock_mode=False) == rows
    assert all(run_pilot.is_real_sample(r) for r in rows)
    assert not run_pilot.is_real_sample({"source": "contract_01.txt"})
    assert run_pilot.is_real_sample({"source": "CUAD_x.pdf"})
    assert run_pilot.is_real_sample({"source": "external/maud/x.txt"})


def test_resolve_dataset(monkeypatch, tmp_path):
    present = tmp_path / "manifest.csv"
    present.write_text("id\n")
    monkeypatch.setattr(run_pilot, "MANIFEST", present)
    assert run_pilot.resolve_dataset(None, True) == ("samples", None)
    assert run_pilot.resolve_dataset(None, False) == ("samples", None)
    assert run_pilot.resolve_dataset("samples", True) == ("samples", None)
    assert run_pilot.resolve_dataset("hf", True) == ("hf", None)

    monkeypatch.setattr(run_pilot, "MANIFEST", tmp_path / "absent.csv")
    dataset, notice = run_pilot.resolve_dataset(None, True)
    assert dataset == "hf" and "absent.csv not found" in notice and "falling back" in notice
    assert run_pilot.resolve_dataset("hf", False) == ("hf", None)
    with pytest.raises(SystemExit, match="only the monorepo has"):
        run_pilot.resolve_dataset("samples", True)
    with pytest.raises(SystemExit, match="--dataset hf explicitly"):
        run_pilot.resolve_dataset(None, False)  # --real never falls back on its own


def test_samples_mode_is_untouched_when_manifest_exists(temp_base_dir, monkeypatch, capsys):
    prepared, built = [], []
    monkeypatch.setattr(run_pilot, "prepare_samples", lambda: prepared.append(1))
    monkeypatch.setattr(run_pilot, "build_hf_manifest", lambda *a, **k: built.append(1))
    manifest = temp_base_dir / "manifest.csv"
    manifest.write_text("id,filename,subdir,expected_doc_class,expected_stage\ns0,s0.txt,x,contract,archived\n")
    monkeypatch.setattr(run_pilot, "MANIFEST", manifest)
    monkeypatch.setattr(run_pilot, "_validate_manifest_ground_truth", lambda m: None)
    seen = []
    monkeypatch.setattr(run_pilot, "run_sample", lambda m, mock, session_id=None, run_id=None: seen.append(m["id"]) or {
        "id": m["id"], "filename": m["filename"], "stage": "archived", "class_match": True,
        "wall_time_s": 1.0, "llm_cost_usd": 0.0, "llm_calls": 1, "doc_type": "contract",
        "expected_doc_class": "contract", "expected_stage": "archived", "stage_expected": True,
        "classification_confidence": 0.95, "llm_tokens": 0, "actual_doc_class": "contract"})
    assert run_pilot.main(["--mock"]) == 0
    assert prepared == [1] and built == [] and seen == ["s0"]
    out = capsys.readouterr().out
    assert "dataset:" not in out and "falling back" not in out


def test_end_to_end_mock_on_hf_documents(temp_base_dir, monkeypatch, fake_corpus, capsys):
    """Two real-shaped documents through the whole mock pipeline, manifest.csv absent."""
    monkeypatch.setattr(run_pilot, "MANIFEST", temp_base_dir / "no-such-manifest.csv")
    monkeypatch.setattr(run_pilot, "prepare_samples", lambda: pytest.fail("prepare_samples must not run for hf"))
    assert run_pilot.main(["--mock", "--per-class", "1", "--include", "contract"]) == 0
    out = capsys.readouterr().out
    assert "falling back to --dataset hf" in out
    assert "dataset: hf (Lucius-Morningstar/mailroom-dataset revision=v-test split=train per_class=1 seed=0) -> 5 documents" in out
    assert "samples: 1 " in out and "class_accuracy: 1.0" in out
    (report_path,) = sorted(Path(temp_base_dir).glob("pilot_report_*.json"))
    report = json.loads(report_path.read_text())
    assert report["dataset"]["dataset_id"] == "Lucius-Morningstar/mailroom-dataset"
    (sample,) = report["samples"]
    assert sample["expected_doc_class"] == "contract" and sample["class_match"] is True
    # the fake LLM answers from the row's labels, so a healthy pipeline archives the document
    assert sample["stage"] == "archived" and sample["stage_expected"] is True
    assert sample["subdir"] == "hf"


def test_mock_answers_from_corpus_labels_only_for_hf_rows(fake_corpus, tmp_path):
    from langchain_agents.doc_inventories import sorter_subclass_catalog

    rows, _ = run_pilot.build_hf_manifest("train", 1, 0, base_dir=tmp_path)
    for r in rows:
        assert r["mock_oracle"] == "1"
        catalog = sorter_subclass_catalog(r["expected_doc_class"])
        if r["expected_doc_class"] == "contract":
            assert r["mock_subclass"] == "other"  # contracts answer with contract_subtype "other"
        else:
            assert r["mock_subclass"] in catalog

    # no subclass / fields on the expectation (committed-manifest samples): historical payload
    base = {"doc_type": "contract", "conf": 0.95}
    assert run_pilot._mock_subclass(base) == {} and run_pilot._mock_fields(base) == {}
    oracle = {**base, "subclass": "other", "fields": {"governing_law": "Delaware"}}
    assert run_pilot._mock_subclass(oracle) == {"doc_subclass": "other"}
    assert run_pilot._mock_fields(oracle) == {"governing_law": "Delaware"}
    classify = run_pilot._fake_client(oracle).chat.completions.create(
        messages=[{"role": "user", "content": "Classify this legal document"}])
    assert json.loads(classify.choices[0].message.content)["doc_subclass"] == "other"
    plain = run_pilot._fake_client(base).chat.completions.create(
        messages=[{"role": "user", "content": "Classify this legal document"}])
    assert "doc_subclass" not in json.loads(plain.choices[0].message.content)


def test_summary_and_rows_survive_an_errored_sample(capsys):
    from scripts import run_pilot as rp

    ok = {"id": "a", "expected_doc_class": "contract", "expected_stage": "archived", "stage": "archived",
          "actual_doc_class": "contract", "class_match": True, "classification_confidence": 0.9,
          "llm_calls": 2, "wall_time_s": 1.0}
    err = {"id": "b", "filename": "b.txt", "status": "error", "error": "RuntimeError: boom"}
    summary = rp.summarize([ok, err])
    assert summary["samples"] == 2 and summary["errors"] == 1 and summary["archived"] == 1
    assert summary["class_accuracy"] == 0.5  # an errored sample is not a correct classification
    assert summary["avg_llm_calls"] == 2.0  # averages cover completed samples only
    rp.print_rows([ok, err])
    assert "ERROR RuntimeError: boom" in capsys.readouterr().out
