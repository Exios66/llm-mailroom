"""Staged interim GT overlay: pinned, opt-in, status-tagged, Hub-superseded (observability.interim_gt)."""

import hashlib
import json

import pytest

from observability import interim_gt
from observability.extraction_gt import build_expected_fields, labelled_expected_fields
from observability.interim_gt import (
    InterimOverlayError,
    active_overlay,
    doc_sha,
    load_interim_overlay,
)

CORP_TEXT = "BYLAWS OF EXAMPLE HOLDINGS, INC., a Delaware corporation, adopted January 5, 2021."
PDE_TEXT = "MEDICARE PRESCRIPTION DRUG BENEFIT -- PHARMACY STATEMENT (Part D)\nFill Reference: 100\n"


def _lab(value, status, **extra):
    return {"value": value, "status": status, "use_as_truth": status != "disputed", **extra}


def _write_overlay(root, records_by_file):
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, records in records_by_file.items():
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        (root / name).write_text(body, encoding="utf-8")
        files[name] = hashlib.sha256(body.encode()).hexdigest()
    manifest = json.dumps({"schema": interim_gt.SCHEMA, "files": files}, indent=2, sort_keys=True)
    (root / "MANIFEST.json").write_text(manifest, encoding="utf-8")
    return hashlib.sha256(manifest.encode()).hexdigest()


def _corp_record(**labels):
    return {
        "doc_class": "corporate_record",
        "doc_id": "DOC-corp",
        "doc_subclass": "bylaws",
        "doc_sha256": doc_sha(CORP_TEXT),
        "labels": labels,
    }


def _corp_sample(**extra):
    return {
        "hub_id": "DOC-corp",
        "text": CORP_TEXT,
        "expected_hf_class": "corporate_record",
        "expected_subclass": "bylaws",
        **extra,
    }


@pytest.fixture
def corp_overlay(tmp_path):
    sha = _write_overlay(tmp_path / "interim", {"corporate_record.jsonl": [_corp_record(
        entity_name=_lab("Example Holdings, Inc.", "gap_fill", basis="gate_accepted_grounded", support=0.97),
        jurisdiction=_lab("Delaware", "gap_fill", basis="gate_accepted_grounded", support=0.95),
        filing_number=_lab(None, "gap_fill", basis="gate_accepted_grounded", support=0.99),
        effective_date=_lab("2021-01-05", "disputed", basis="gate_vs_hub", hub_value="2021-01-06"),
        record_type=_lab("bylaws", "derived", basis="hub_subclass", hub_value=None),
    )]})
    return load_interim_overlay(tmp_path / "interim", sha256=sha)


def test_overlay_is_off_by_default(monkeypatch):
    monkeypatch.delenv(interim_gt.ENV_DIR, raising=False)
    monkeypatch.delenv(interim_gt.ENV_SHA256, raising=False)
    assert active_overlay() is None
    fields, meta = build_expected_fields(_corp_sample())
    assert "interim_gt" not in meta
    assert not any(str(src).startswith("interim:") for src in meta["sources"].values())
    # Without the overlay a post-hoc regex guess may fill the field; it is never gold.
    assert meta["sources"].get("entity_name") in (None, "posthoc")
    gold = labelled_expected_fields({"expected_fields": fields, "expected_fields_sources": meta["sources"]})
    assert "entity_name" not in gold


def test_env_opt_in_needs_both_the_directory_and_the_pin(monkeypatch, tmp_path, corp_overlay):
    monkeypatch.setenv(interim_gt.ENV_DIR, corp_overlay.root)
    monkeypatch.delenv(interim_gt.ENV_SHA256, raising=False)
    with pytest.raises(InterimOverlayError, match="set both"):
        active_overlay()
    monkeypatch.setenv(interim_gt.ENV_SHA256, corp_overlay.manifest_sha256)
    assert active_overlay().manifest_sha256 == corp_overlay.manifest_sha256
    _fields, meta = build_expected_fields(_corp_sample())
    assert meta["interim_gt"]["overlay_sha256"] == corp_overlay.manifest_sha256


def test_loader_refuses_a_wrong_pin_an_edited_file_and_an_unlisted_file(tmp_path):
    root = tmp_path / "interim"
    sha = _write_overlay(root, {"corporate_record.jsonl": [_corp_record(
        entity_name=_lab("Example Holdings, Inc.", "gap_fill"))]})
    with pytest.raises(InterimOverlayError, match="does not match the pin"):
        load_interim_overlay(root, sha256="0" * 64)
    (root / "corporate_record.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(InterimOverlayError, match="differs from the manifest"):
        load_interim_overlay(root, sha256=sha)
    sha = _write_overlay(root, {"corporate_record.jsonl": [_corp_record()]})
    (root / "stray.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(InterimOverlayError, match="not in the manifest"):
        load_interim_overlay(root, sha256=sha)


def test_loader_refuses_a_label_whose_truth_flag_contradicts_its_status(tmp_path):
    bad = _lab("Example Holdings, Inc.", "disputed")
    bad["use_as_truth"] = True
    root = tmp_path / "interim"
    sha = _write_overlay(root, {"corporate_record.jsonl": [_corp_record(entity_name=bad)]})
    with pytest.raises(InterimOverlayError, match="use_as_truth"):
        load_interim_overlay(root, sha256=sha)


def test_truth_labels_are_applied_tagged_and_scored_as_gold(corp_overlay):
    fields, meta = build_expected_fields(_corp_sample(), overlay=corp_overlay)
    sources = meta["sources"]
    assert fields["entity_name"] == "Example Holdings, Inc."
    assert sources["entity_name"] == "interim:gap_fill"
    assert sources["jurisdiction"] == "interim:gap_fill"
    report = meta["interim_gt"]
    assert report["overlay_sha256"] == corp_overlay.manifest_sha256
    assert report["applied"] == {"entity_name": "gap_fill", "jurisdiction": "gap_fill"}
    assert report["disputed"] == ["effective_date"]
    assert report["absent"] == ["filing_number"]
    # record_type: the Hub-built gold already derives it from the subclass, so the Hub wins.
    assert "record_type" in report["superseded"]
    gold = labelled_expected_fields({"expected_fields": fields, "expected_fields_sources": sources})
    assert gold["entity_name"] == "Example Holdings, Inc."
    assert gold.get("effective_date") != "2021-01-05"


def test_a_label_the_hub_publishes_supersedes_the_overlay(corp_overlay):
    sample = _corp_sample(expected_fields={"entity_name": "Example Holdings Incorporated"})
    fields, meta = build_expected_fields(sample, overlay=corp_overlay)
    assert fields["entity_name"] == "Example Holdings Incorporated"
    assert meta["sources"]["entity_name"] == "hub"
    assert "entity_name" in meta["interim_gt"]["superseded"]


def test_a_correction_applies_only_against_the_hub_value_it_corrected(tmp_path):
    record = _corp_record(entity_name=_lab(
        "Example Holdings, Inc.", "correction", basis="documented_defect", hub_value="Example Holdngs"))
    root = tmp_path / "interim"
    overlay = load_interim_overlay(root, sha256=_write_overlay(root, {"corporate_record.jsonl": [record]}))
    fields, meta = build_expected_fields(
        _corp_sample(expected_fields={"entity_name": "Example Holdngs"}), overlay=overlay)
    assert (fields["entity_name"], meta["sources"]["entity_name"]) == ("Example Holdings, Inc.", "interim:correction")
    # A revised Hub label differs from the corrected one: the Hub wins.
    fields, meta = build_expected_fields(
        _corp_sample(expected_fields={"entity_name": "Example Holdings Corp."}), overlay=overlay)
    assert fields["entity_name"] == "Example Holdings Corp."
    assert meta["interim_gt"]["superseded"] == ["entity_name"]


def test_labels_bind_to_the_exact_document_text(corp_overlay):
    fields, meta = build_expected_fields(_corp_sample(text=CORP_TEXT + " Amended."), overlay=corp_overlay)
    assert fields.get("entity_name") != "Example Holdings, Inc."
    assert meta["sources"].get("entity_name") in (None, "posthoc")
    assert meta["interim_gt"]["applied"] == {}
    assert meta["interim_gt"]["rejected"] == ["*: document text differs from the labelled text"]


def test_cms_claim_type_health_is_correct_gold_not_a_defect(tmp_path):
    """#280: a CMS row's claim_type is health. An overlay "correcting" it to a table is rejected."""
    record = {
        "doc_class": "insurance_claim",
        "doc_id": "DOC-pde",
        "doc_subclass": "pde",
        "doc_sha256": doc_sha(PDE_TEXT),
        "labels": {
            "claim_type": _lab("pde", "correction", basis="cms_claim_type_collapsed", hub_value="health"),
            "cms_source_table": _lab("pde", "derived", basis="cms_subclass", hub_value=None),
        },
    }
    root = tmp_path / "interim"
    overlay = load_interim_overlay(root, sha256=_write_overlay(root, {"insurance_claim.jsonl": [record]}))
    sample = {"hub_id": "DOC-pde", "text": PDE_TEXT, "expected_hf_class": "insurance_claim",
              "expected_subclass": "pde", "claim_type": "health"}
    fields, meta = build_expected_fields(sample, overlay=overlay)
    assert fields["claim_type"] == "health"
    assert meta["sources"]["claim_type"] == "hub"
    assert meta["interim_gt"]["rejected"] == ["claim_type: a CMS row's claim_type is health"]
    assert fields["cms_source_table"] == "pde"


def test_hf_pilot_rows_and_report_record_the_overlay(monkeypatch, corp_overlay):
    from scripts import run_hf_pilot

    monkeypatch.delenv(interim_gt.ENV_DIR, raising=False)
    monkeypatch.delenv(interim_gt.ENV_SHA256, raising=False)
    assert run_hf_pilot.interim_gt_provenance() is None
    monkeypatch.setenv(interim_gt.ENV_DIR, corp_overlay.root)
    monkeypatch.setenv(interim_gt.ENV_SHA256, corp_overlay.manifest_sha256)
    prov = run_hf_pilot.interim_gt_provenance()
    assert prov == {"schema": interim_gt.SCHEMA, "overlay_sha256": corp_overlay.manifest_sha256, "documents": 1}
    meta = run_hf_pilot.expected_fields_meta(_corp_sample())
    assert meta["interim_gt"]["overlay_sha256"] == corp_overlay.manifest_sha256


def test_a_label_outside_the_closed_inventory_vocabulary_is_rejected(tmp_path):
    record = _corp_record(record_type=_lab("board_resolution", "derived", basis="hub_subclass", hub_value=None))
    record["doc_subclass"] = "board_resolution"
    root = tmp_path / "interim"
    overlay = load_interim_overlay(root, sha256=_write_overlay(root, {"corporate_record.jsonl": [record]}))
    fields, meta = build_expected_fields(_corp_sample(expected_subclass="board_resolution"), overlay=overlay)
    assert "record_type" not in fields
    assert meta["interim_gt"]["rejected"] == [
        "record_type: 'board_resolution' is not in the closed record_type vocabulary"
    ]


def test_live_manifest_rows_key_the_overlay_by_document_id():
    from scripts.run_hf_pilot import parse_hf_row

    base = {"filename": "bylaws_1.txt", "doc_text": CORP_TEXT * 5, "expected_doc_class": "corporate_record"}
    assert parse_hf_row({**base, "document_id": "DOC-corp"})["hub_id"] == "DOC-corp"
    assert parse_hf_row({**base, "id": "DOC-hub", "document_id": "DOC-corp"})["hub_id"] == "DOC-hub"


def test_resume_refuses_a_different_overlay(monkeypatch, corp_overlay):
    from scripts import run_hf_pilot

    monkeypatch.delenv(interim_gt.ENV_DIR, raising=False)
    monkeypatch.delenv(interim_gt.ENV_SHA256, raising=False)
    run_hf_pilot.check_resume_gold({"samples": [], "interim_gt": None})
    run_hf_pilot.check_resume_gold({"samples": []})  # a report from before the overlay: Hub-only
    with_overlay = {"samples": [], "interim_gt": corp_overlay.provenance()}
    with pytest.raises(SystemExit, match="differs"):
        run_hf_pilot.check_resume_gold(with_overlay)
    monkeypatch.setenv(interim_gt.ENV_DIR, corp_overlay.root)
    monkeypatch.setenv(interim_gt.ENV_SHA256, corp_overlay.manifest_sha256)
    run_hf_pilot.check_resume_gold(with_overlay)
    with pytest.raises(SystemExit, match="differs"):
        run_hf_pilot.check_resume_gold({"samples": [], "interim_gt": None})
