"""Pins for notebooks 09–13 labs (huggingface / legalbench / class packs).

Network-free: Hugging Face helpers read committed Dataset Viewer snapshots;
LegalBench runs the mock runner on the miniature CUAD fixture.
"""

from __future__ import annotations

from notebooks.huggingface_lab import (
    catalog,
    filter_rows,
    list_datasets,
    preview,
    row_to_doc_text,
    search,
)
from notebooks.legalbench_lab import load_mini_qa, run_mini, task_table
from notebooks.pipeline_lab import CLASS_PACKS, LEGACY_SPECIALIST_CANNED


def test_hf_catalog_has_seven_lucius_datasets():
    cat = catalog()
    assert cat["org"] == "Lucius-Morningstar"
    ids = [d["id"] for d in cat["datasets"]]
    assert len(ids) == 7
    assert all(i.startswith("Lucius-Morningstar/") for i in ids)
    claims = next(d for d in cat["datasets"] if d["id"].endswith("cms-desynpuf-insurance-claims"))
    assert "insurance_claim" in claims["mailroom_classes"]


def test_hf_preview_and_search_are_offline():
    p = preview("Lucius-Morningstar/cms-desynpuf-insurance-claims")
    assert p["source"] == "offline-snapshot"
    assert p["rows"]
    text = row_to_doc_text(p["rows"][0])
    assert "MEDICARE" in text.upper() or len(text) > 40
    hits = search("Lucius-Morningstar/enron-correspondence-dedup", "forecast")
    assert hits["source"].startswith("offline")
    assert hits["rows"]


def test_hf_filter_equality_on_snapshot():
    filt = filter_rows(
        "Lucius-Morningstar/cms-desynpuf-insurance-claims",
        where="expected=insurance_claim",
    )
    assert filt["rows"]
    assert all(r.get("expected") == "insurance_claim" for r in filt["rows"])


def test_class_packs_cover_every_taxonomy_class():
    from pipeline.config import load_config

    keys = [c["key"] for c in load_config()["doc_classes"]]
    assert set(CLASS_PACKS) == set(keys)
    assert len(LEGACY_SPECIALIST_CANNED) == 4  # live classes except langchain contract + merger


def test_legalbench_mini_mock_run():
    assert {t["id"] for t in task_table()} == {"contract_qa", "family_classification"}
    rows = load_mini_qa(n=6, seed=1)
    assert len(rows) == 6
    result = run_mini("contract_qa", n=6, seed=1)
    assert result["n"] == 6
    assert "accuracy" in result["scores"]
    assert "mock" in result["honesty"].lower()
