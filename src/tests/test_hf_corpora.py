"""Hugging Face corpus registry — docclass-merged v8 is the full surface."""

from pipeline.hf_corpora import (
    FULL_CORPUS_ID,
    FULL_CORPUS_REVISION,
    FULL_CORPUS_SCHEMA,
    HUB_CLASSES,
    adapt_hub_row,
    example_for_class,
    example_rows,
    examples_by_class,
    hub_sample,
    pipeline_corpora,
    resolve_corpus,
    set_active_corpus,
)


def test_v8_full_corpus_is_docclass_merged():
    corp = resolve_corpus("v8")
    assert corp["id"] == FULL_CORPUS_ID
    assert corp["schema"] == FULL_CORPUS_SCHEMA == "v8"
    assert corp["revision"] == FULL_CORPUS_REVISION
    assert corp["n_docs"] == 2000
    assert corp["pipeline"] is True
    assert tuple(corp["classes"]) == HUB_CLASSES
    assert "merger_agreement" in corp["classes"]
    assert "compliance_filing" not in corp["classes"]
    # v5/v7 aliases resolve to the same slug — the registry carries ONE full
    # corpus entry; the HUB-019 v7 freeze stays resolvable as a Hub commit
    # (bb57c5ad) for historical traces, not as a separate registry surface.
    assert resolve_corpus("v5")["slug"] == "docclass-merged"
    assert resolve_corpus("v7") is resolve_corpus("v8")


def test_pipeline_corpora_include_enron_and_claims():
    slugs = {c["slug"] for c in pipeline_corpora()}
    assert slugs == {
        "docclass-merged",
        "docclass-pilot",
        "enron-correspondence-dedup",
        "cms-desynpuf-insurance-claims",
        "mailroom-cuad-contracts-full",
        "mailroom-cuad-contracts",
    }
    enron = resolve_corpus("enron")
    assert enron["n_docs"] == 247523
    assert enron["classes"] == ("correspondence",)
    assert resolve_corpus("legalbench-full")["pipeline"] is False


def test_class_subclass_pack_covers_every_stratum():
    rows = example_rows()
    strata = {(r["expected"], r.get("expected_subclass") or "") for r in rows}
    assert len(strata) == 48
    by_class = examples_by_class()
    assert set(by_class) == set(HUB_CLASSES)
    merger = example_for_class("merger_agreement")
    assert merger["expected"] == "merger_agreement"
    assert merger["expected_subclass"] in {
        "all_cash", "all_stock", "mixed_cash_stock",
        "mixed_cash_stock_election", "other",
    }
    assert "AGREEMENT" in merger["doc_text"].upper() or "MERGER" in merger["doc_text"].upper()
    contract = example_for_class("contract")
    assert contract["filename"] != merger["filename"]
    sample = hub_sample(contract)
    assert sample["expected_hf_class"] == "contract"
    assert sample["text"]


def test_adapt_enron_and_cuad_rows():
    enron = adapt_hub_row(
        {"filename": "allen-p/_sent_mail/1.", "text": "Here is our forecast"},
        resolve_corpus("enron"),
    )
    assert enron["doc_text"] == "Here is our forecast"
    assert enron["expected"] == "correspondence"
    cuad = adapt_hub_row(
        {
            "id": "cuad-License Agreement",
            "input": '{"doc_text": "LICENSE AGREEMENT between Acme and Beta."}',
            "metadata": {"category": "License_Agreements"},
        },
        resolve_corpus("cuad"),
    )
    assert cuad["expected"] == "contract"
    assert "LICENSE AGREEMENT" in cuad["doc_text"]
    assert cuad["expected_subclass"] == "License_Agreements"


def test_set_active_corpus_roundtrip():
    try:
        assert set_active_corpus("claims")["slug"] == "cms-desynpuf-insurance-claims"
        assert set_active_corpus("Lucius-Morningstar/docclass-pilot")["slug"] == "docclass-pilot"
    finally:
        set_active_corpus("docclass-merged")
