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
    assert CLASS_PACKS["contract"]["source"].startswith("huggingface")
    assert CLASS_PACKS["merger_agreement"]["source"].startswith("huggingface")
    assert CLASS_PACKS["compliance_filing"]["source"] == "local-fixture"
    for key in ("contract", "merger_agreement", "corporate_record",
                "correspondence", "insurance_claim"):
        assert CLASS_PACKS[key]["source"].startswith("huggingface")
        # Hub filenames, not invented local stand-ins.
        assert CLASS_PACKS[key]["filename"] not in {
            "contract.txt", "merger_agreement.txt", "bylaws.txt",
            "demand_letter.txt", "fnol.txt",
        }
        assert len(CLASS_PACKS[key]["text"]) > 200


def test_class_subclass_examples_pack_is_v5_pilot():
    from notebooks.huggingface_lab import class_subclass_examples

    pack = class_subclass_examples()
    assert pack["parent"] == "Lucius-Morningstar/mailroom-corpus"
    assert pack["schema"] == "v5"
    assert pack["n_strata"] == 48
    assert len(pack["examples"]) == 48
    strata = {
        (row["expected"], row.get("expected_subclass") or "")
        for row in pack["examples"]
    }
    assert len(strata) == 48
    assert "merger_agreement" in pack["classes"]


def test_legalbench_mini_mock_run():
    assert {t["id"] for t in task_table()} == {"contract_qa", "family_classification"}
    rows = load_mini_qa(n=6, seed=1)
    assert len(rows) == 6
    result = run_mini("contract_qa", n=6, seed=1)
    assert result["n"] == 6
    assert "accuracy" in result["scores"]
    assert "mock" in result["honesty"].lower()
