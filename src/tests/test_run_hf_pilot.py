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
    _inbox_filename,
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


def test_inbox_filename_forces_txt_for_pdf_and_htm():
    assert _inbox_filename("deal.PDF") == "deal.txt"
    assert _inbox_filename("a/b/ex4-1.htm") == "ex4-1.txt"
    assert _inbox_filename("outpatient:1.txt") == "outpatient_1.txt"


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
    assert labels["a.htm"]["expected"] == "corporate_record"
    assert labels["a.htm"]["expected_subclass"] == "bylaws"
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
    assert payload["unique_matters"] is True
    matter_ids = {row["matter_id"] for row in payload["samples"]}
    assert len(matter_ids) == len(payload["samples"])
    metrics = payload["metrics"]
    assert metrics["n"] == len(payload["samples"])
    assert "exact_accuracy" in metrics
    assert "aligned_accuracy" in metrics
    assert "total_cost_usd" in metrics
    assert "per_class" in metrics
    assert payload["honesty"]["compliance_filing"]["in_hf_pilot"] is False
    assert payload["honesty"]["compliance_filing"]["in_corpus"] is False
    assert payload["honesty"]["corporate_record"]["in_corpus"] is True
    assert payload["local_packs"]["compliance_filing"]["n"] == 2
    assert payload["local_packs"]["insurance_contrast"]["gt_homogeneity"] is False
    assert payload["local_packs"]["corporate_extraction"]["hub_extract_is_subclass_only"] is True
    md = reports[0].with_suffix(".md").read_text(encoding="utf-8")
    assert (reports[0].with_suffix(".md")).is_file()
    assert "exact accuracy" in md
    assert "Corpus honesty" in md


def test_unique_name_avoids_collisions():
    from scripts.run_hf_pilot import _unique_name

    used: set[str] = set()
    assert _unique_name("deal.txt", used) == "deal.txt"
    assert _unique_name("deal.txt", used) == "deal__2.txt"
    assert _unique_name("deal.txt", used) == "deal__3.txt"


def test_normalize_consideration_tokens():
    from scripts.run_hf_pilot import infer_merger_consideration, normalize_consideration

    assert normalize_consideration("all_cash") == "all_cash"
    assert normalize_consideration("Mixed Cash/Stock Election") == "mixed_cash_stock_election"
    assert infer_merger_consideration({"contract_value": "all stock"}) == "all_stock"
    assert infer_merger_consideration({
        "key_obligations": ["Merger consideration is cash and stock"]
    }) == "mixed_cash_stock"


def test_subclass_ok_cuad_and_maud():
    from scripts.run_hf_pilot import subclass_ok

    assert subclass_ok("contract", "Distributor", predicted_subtype="distributor") is True
    assert subclass_ok("contract", "license", predicted_subtype="maintenance") is True
    assert subclass_ok("merger_agreement", "all_cash", extracted={"contract_value": "all_cash"}) is True
    assert subclass_ok("corporate_record", "bylaws", extracted={"record_type": "Bylaws"}) is True
    assert subclass_ok(
        "corporate_record",
        "articles_of_incorporation",
        extracted={"record_type": "Certificate of Incorporation"},
    ) is True
    assert subclass_ok("insurance_claim", "outpatient", extracted={"claim_type": "outpatient"}) is True
    assert subclass_ok("insurance_claim", "pde", extracted={"claim_type": "Part D Event"}) is True
    assert subclass_ok(
        "correspondence", "meeting_request", extracted={"communication_type": "meeting invite"}
    ) is True
    assert subclass_ok("contract", "") is None


def test_summarize_rows_counts_cost_and_accuracy():
    from scripts.run_hf_pilot import summarize_rows

    rows = [
        {"expected": "contract", "exact_ok": True, "aligned_ok": True, "subclass_ok": True,
         "llm_cost_usd": 0.01, "llm_tokens": 100, "llm_calls": 2, "wall_time_s": 1.0, "stage": "archived"},
        {"expected": "merger_agreement", "exact_ok": False, "aligned_ok": True, "subclass_ok": False,
         "llm_cost_usd": 0.02, "llm_tokens": 200, "llm_calls": 3, "wall_time_s": 2.0, "stage": "review"},
    ]
    summary = summarize_rows(rows)
    assert summary["n"] == 2
    assert summary["exact_accuracy"] == 0.5
    assert summary["aligned_accuracy"] == 1.0
    assert summary["subclass_accuracy"] == 0.5
    assert summary["total_cost_usd"] == 0.03
    assert summary["total_tokens"] == 300
    assert summary["stages"]["archived"] == 1
    assert summary["per_class"]["contract"]["exact"] == 1


def test_latest_hf_reports_orders_newest(tmp_path):
    from scripts.run_hf_pilot import latest_hf_reports

    older = tmp_path / "20260825T010000Z"
    newer = tmp_path / "20260825T020000Z"
    older.mkdir()
    newer.mkdir()
    (older / "report.json").write_text("{}", encoding="utf-8")
    (newer / "report.json").write_text("{}", encoding="utf-8")
    found = latest_hf_reports(1, root=tmp_path)
    assert found == [newer / "report.json"]


def test_hf_samples_from_report_uses_stored_text(tmp_path):
    from scripts.run_hf_pilot import hf_samples_from_report

    report = {
        "samples": [{
            "local_filename": "deal.txt",
            "predicted": "contract",
            "expected_doc_class": "contract",
            "expected": "contract",
            "expected_subclass": "service",
            "extracted_data": {"parties": ["A"]},
            "trace_id": "abc",
            "doc_text": "SERVICES AGREEMENT",
        }]
    }
    samples = hf_samples_from_report(report, tmp_path / "report.json")
    assert samples[0]["trace_id"] == "abc"
    assert samples[0]["extracted_data"]["parties"] == ["A"]
    assert "SERVICES" in samples[0]["doc_text"]


def test_remaining_samples_retries_errors_only():
    from scripts.run_hf_pilot import remaining_samples

    samples = [
        {"filename": "a.txt", "expected_hf_class": "contract"},
        {"filename": "b.txt", "expected_hf_class": "contract"},
        {"filename": "c.txt", "expected_hf_class": "contract"},
    ]
    rows = [
        {"filename": "a.txt", "stage": "archived", "predicted": "contract"},
        {"filename": "b.txt", "stage": "error", "predicted": None},
    ]
    left = remaining_samples(samples, rows)
    assert [s["filename"] for s in left] == ["b.txt", "c.txt"]


def test_finalize_report_writes_metrics_and_markdown(tmp_path):
    from scripts.run_hf_pilot import finalize_report

    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "session_id": "pilot-hf-test",
        "dataset": "Lucius-Morningstar/docclass-merged",
        "split": "train",
        "mode": "real",
        "errors": 0,
        "samples": [
            {
                "filename": "deal.txt",
                "expected": "contract",
                "predicted": "contract",
                "expected_subclass": "Distributor",
                "predicted_subtype": "distributor",
                "stage": "archived",
                "exact_ok": True,
                "aligned_ok": True,
                "llm_cost_usd": 0.01,
                "llm_tokens": 50,
                "llm_calls": 3,
                "wall_time_s": 1.2,
                "extracted_data": {"cuad_family": "distributor", "parties": ["A"]},
            }
        ],
    }), encoding="utf-8")
    payload = finalize_report(report_path)
    assert payload["metrics"]["n"] == 1
    assert payload["metrics"]["exact_accuracy"] == 1.0
    assert payload["metrics"]["total_cost_usd"] == 0.01
    assert payload["samples"][0]["subclass_ok"] is True
    md = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "exact accuracy" in md
    assert "cost USD" in md


def test_summarize_rows_includes_extraction_mean():
    from scripts.run_hf_pilot import summarize_rows

    summary = summarize_rows([
        {"expected": "contract", "exact_ok": True, "aligned_ok": True,
         "llm_cost_usd": 0.01, "extraction_overall_score": 0.8, "stage": "archived"},
        {"expected": "contract", "exact_ok": True, "aligned_ok": True,
         "llm_cost_usd": 0.01, "extraction_overall_score": 1.0, "stage": "archived"},
    ])
    assert summary["extraction_n"] == 2
    assert summary["extraction_overall_mean"] == 0.9

