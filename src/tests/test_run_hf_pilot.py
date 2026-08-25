"""HF docclass pilot runner — The-Mailroom production-pilot contract."""

import json
import os

from scripts.run_hf_pilot import (
    ALIGN,
    DATASET_ID,
    HF_CLASSES,
    check_contract,
    parse_hf_row,
    pipeline_class,
    select_stratified,
    _safe_filename,
)


def test_pipeline_class_aligns_merger_agreement():
    assert pipeline_class("merger_agreement") == "contract"
    assert pipeline_class("insurance_claim") == "insurance_claim"
    assert ALIGN["merger_agreement"] == "contract"


def test_parse_hf_row_reads_nested_metadata():
    row = parse_hf_row({
        "filename": "contract_88_merger_agreement.txt",
        "doc_text": "x" * 300,
        "metadata": json.dumps({
            "expected_doc_type": "merger_agreement",
            "expected_subclass": "all_cash",
            "chars": "300",
        }),
    })
    assert row["expected_hf_class"] == "merger_agreement"
    assert row["expected_subclass"] == "all_cash"
    assert row["chars"] == 300


def test_parse_hf_row_uses_ground_truth_expected_field():
    row = parse_hf_row({
        "filename": "deal.htm",
        "doc_text": "x" * 300,
        "expected": "merger_agreement",
        "expected_subclass": "mixed_cash_stock",
    })
    assert row["expected_hf_class"] == "merger_agreement"
    assert row["expected_subclass"] == "mixed_cash_stock"


def test_parse_hf_row_rejects_cuad_folder_as_class():
    assert parse_hf_row({
        "filename": "license.pdf",
        "doc_text": "x" * 300,
        "metadata": {"expected_doc_type": "", "category": "License_Agreements"},
    }) is None
    assert parse_hf_row({
        "filename": "license.pdf",
        "doc_text": "x" * 300,
        "expected": "License_Agreements",
        "expected_subclass": "License_Agreements",
    }) is None


def test_parse_hf_row_joins_ground_truth_labels():
    default = {
        "filename": "outpatient_1.txt",
        "doc_text": "CMS MEDICARE " + "x" * 300,
        "metadata": {"expected_doc_type": "", "category": ""},
    }
    labels = {
        "outpatient_1.txt": {
            "expected": "insurance_claim",
            "expected_subclass": "outpatient",
        }
    }
    row = parse_hf_row(default, labels)
    assert row["expected_hf_class"] == "insurance_claim"
    assert row["expected_subclass"] == "outpatient"


def test_select_stratified_picks_nearest_to_target():
    rows = []
    for cls in HF_CLASSES:
        rows.append({"expected_hf_class": cls, "chars": 1000, "filename": f"{cls}-short.txt"})
        rows.append({"expected_hf_class": cls, "chars": 6100, "filename": f"{cls}-near.txt"})
        rows.append({"expected_hf_class": cls, "chars": 40000, "filename": f"{cls}-huge.txt"})
    picked = select_stratified(rows, per_class=1, max_chars=25000, target_chars=6000)
    assert len(picked) == 5
    assert {r["expected_hf_class"] for r in picked} == set(HF_CLASSES)
    assert all(r["filename"].endswith("-near.txt") for r in picked)


def test_select_stratified_keeps_oversized_merger():
    rows = [{"expected_hf_class": "merger_agreement", "chars": 340354, "filename": "maud.htm"}]
    for cls in HF_CLASSES:
        if cls != "merger_agreement":
            rows.append({"expected_hf_class": cls, "chars": 6100, "filename": f"{cls}.txt"})
    picked = select_stratified(rows, per_class=1, max_chars=25000, target_chars=6000)
    assert len(picked) == 5
    merger = next(r for r in picked if r["expected_hf_class"] == "merger_agreement")
    assert merger["chars"] == 340354


def test_safe_filename_strips_path_and_caps():
    assert _safe_filename("a/b/c.txt") == "c.txt"
    assert _safe_filename("noext") == "noext.txt"


def test_load_ground_truth_labels_reads_expected_fields(monkeypatch):
    from scripts.run_hf_pilot import load_ground_truth_labels

    monkeypatch.setattr(
        "scripts.run_hf_pilot._paginate_viewer",
        lambda **kw: [
            {
                "filename": "a.htm",
                "expected": "corporate_record",
                "expected_subclass": "bylaws",
            },
            {
                "filename": "b.pdf",
                "expected": "contract",
                "expected_subclass": "Distributor",
            },
            {
                "filename": "skip.pdf",
                "expected": "License_Agreements",
                "expected_subclass": "License_Agreements",
            },
        ] if kw.get("config") == "ground_truth" else [],
    )
    labels = load_ground_truth_labels(split="train", max_scan=100)
    assert labels["a.htm"] == {
        "expected": "corporate_record",
        "expected_subclass": "bylaws",
    }
    assert labels["b.pdf"]["expected"] == "contract"
    assert "skip.pdf" not in labels
    monkeypatch.delenv("MAILROOM_DOCCLASS_PROMPTS", raising=False)
    import sys
    from scripts import run_hf_pilot as mod

    monkeypatch.setattr(sys, "argv", ["run_hf_pilot.py", "--check", "--docclass"])
    assert mod.main() == 0
    assert os.environ.get("MAILROOM_DOCCLASS_PROMPTS") == "1"
    monkeypatch.delenv("MAILROOM_DOCCLASS_PROMPTS", raising=False)


def test_check_contract_prints_ok(capsys):
    assert check_contract() == 0
    out = capsys.readouterr().out
    assert "check ok" in out
    payload = json.loads(out.split("check ok ", 1)[1])
    assert payload["dataset"] == DATASET_ID
    assert payload["intake"] is True


def test_public_ground_truth_omits_expected_fields():
    from graph.build_graph import _public_ground_truth

    public = _public_ground_truth({
        "expected_doc_class": "contract",
        "expected_hf_class": "merger_agreement",
        "expected_subclass": "all_cash",
        "expected_fields": {"parties": ["A"]},
    })
    assert public["expected_hf_class"] == "merger_agreement"
    assert public["expected_doc_class"] == "contract"
    assert "expected_fields" not in public


def test_hf_pilot_mock_writes_report(temp_base_dir, mock_openai_client, mock_langchain_llm, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    monkeypatch.setenv("MAILROOM_HF_PILOT_DIR", str(temp_base_dir / "hf_pilot"))
    monkeypatch.setenv("MAILROOM_VISION_ENABLED", "0")
    from scripts import run_hf_pilot as mod

    monkeypatch.setattr(mod, "_mock_samples", lambda per_class: [{
        "filename": "hf_contract.txt",
        "text": "SERVICES AGREEMENT between Acme and Beta. " * 20,
        "expected_hf_class": "contract",
        "expected_subclass": "fixture",
        "chars": 400,
    }])
    import sys
    monkeypatch.setattr(sys, "argv", ["run_hf_pilot.py", "--mock", "--per-class", "1"])
    assert mod.main() == 0
    reports = list((temp_base_dir / "hf_pilot").glob("*/report.json"))
    assert reports
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["session_id"].startswith("pilot-hf-")
    assert payload["samples"][0]["expected"] == "contract"
    assert payload["samples"][0]["local_filename"] == "hf_contract.txt"
    assert "stage" in payload["samples"][0]
