"""Human labels -> calibration records, and the review tray priority order."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from pipeline.review_priority import oldest_age_hours, priority, sort_queue
from schemas.run_record import Gold, RunRecord, to_review_record

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_review_labels as exp  # noqa: E402

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

CFG = {
    "cause_weights": {"transient_exhausted": 3.0, "schema_invalid": 3.0,
                      "low_extraction_confidence": 1.5, "sorter_low_confidence": 1.2},
    "class_tier_weight": {"contract": 1.3, "correspondence": 1.0},
    "tier_cutoffs": {"high": 3.0, "normal": 1.5},
    "age_half_life_hours": 24,
    "sla_hours": {"high": 4, "normal": 24, "low": 72},
}


def _item(doc_id, causes, doc_type="correspondence", age_h=1.0):
    ts = (NOW - dt.timedelta(hours=age_h)).isoformat()
    return {"doc_id": doc_id, "doc_type": doc_type, "review_causes": causes,
            "updated_at": ts, "created_at": ts}


# ---------------------------------------------------------------- priority

def test_failure_causes_outrank_low_confidence():
    high = priority(_item("a", ["transient_exhausted"]), NOW, cfg=CFG)
    normal = priority(_item("b", ["low_extraction_confidence"]), NOW, cfg=CFG)
    low = priority(_item("c", ["sorter_low_confidence"]), NOW, cfg=CFG)
    assert (high["tier"], normal["tier"], low["tier"]) == ("high", "normal", "low")
    assert high["score"] > normal["score"] > low["score"]


def test_class_severity_raises_the_score():
    a = priority(_item("a", ["low_extraction_confidence"], "contract"), NOW, cfg=CFG)
    b = priority(_item("b", ["low_extraction_confidence"], "correspondence"), NOW, cfg=CFG)
    assert a["score"] > b["score"]


def test_age_raises_the_score_but_not_the_tier():
    young = priority(_item("a", ["sorter_low_confidence"], age_h=1), NOW, cfg=CFG)
    old = priority(_item("b", ["sorter_low_confidence"], age_h=200), NOW, cfg=CFG)
    assert old["score"] > young["score"]
    assert old["tier"] == young["tier"] == "low"


def test_sla_flag_follows_the_tier_limit():
    fresh = priority(_item("a", ["transient_exhausted"], age_h=3), NOW, cfg=CFG)
    stale = priority(_item("b", ["transient_exhausted"], age_h=5), NOW, cfg=CFG)
    slow_ok = priority(_item("c", ["sorter_low_confidence"], age_h=5), NOW, cfg=CFG)
    assert fresh["sla_breached"] is False
    assert stale["sla_breached"] is True
    assert slow_ok["sla_breached"] is False


def test_item_without_causes_is_neutral_and_reports_it():
    result = priority({"doc_id": "x", "doc_type": "correspondence"}, NOW, cfg=CFG)
    assert result["reasons"] == ["no_cause_recorded"]
    assert result["age_hours"] is None and result["sla_breached"] is False


def test_causes_fall_back_to_the_catalog_review_block():
    item = {"doc_type": "contract",
            "extracted_data": {"_review": {"review_causes": ["schema_invalid"]}}}
    assert priority(item, NOW, cfg=CFG)["reasons"] == ["schema_invalid"]


def test_sort_is_priority_then_oldest_first_and_adds_fields_only():
    items = [
        _item("young-low", ["sorter_low_confidence"], age_h=1),
        _item("old-low", ["sorter_low_confidence"], age_h=1),
        _item("urgent", ["schema_invalid"], age_h=0.5),
    ]
    items[1]["updated_at"] = items[1]["created_at"] = (NOW - dt.timedelta(hours=1)).isoformat()
    # identical score for the two low items: the older one (smaller timestamp) leads
    items[0]["updated_at"] = items[0]["created_at"] = (NOW - dt.timedelta(hours=1)).isoformat()
    items[1]["updated_at"] = items[1]["created_at"] = (NOW - dt.timedelta(hours=1.0001)).isoformat()
    ordered = sort_queue(items, NOW, cfg=CFG)
    assert [i["doc_id"] for i in ordered] == ["urgent", "old-low", "young-low"]
    assert all("priority" in i and "sla_breached" in i for i in ordered)
    assert oldest_age_hours(ordered) == pytest.approx(1.0, abs=0.01)


def test_shipped_taxonomy_block_loads_and_orders():
    high = priority(_item("a", ["chunk_failed"], "contract"), NOW)
    low = priority(_item("b", ["sorter_low_confidence"], "correspondence"), NOW)
    assert high["tier"] == "high" and low["tier"] == "low"


# ------------------------------------------------------------ label export

def _manifest(doc_id, *, model_class, human_class, decision="approved",
              resolution="approve_as_is", corrections=None, resolved="2026-05-30T10:00:00+00:00",
              extraction_source=None, stage="archived"):
    return {
        "doc_id": doc_id, "original_filename": f"{doc_id}.pdf", "stage": stage,
        "review_causes": ["sorter_low_confidence"],
        "extraction_source": extraction_source,
        "human_label": {
            "decision": decision, "resolution": resolution, "doc_type": human_class,
            "doc_subclass": None, "field_corrections": corrections,
            "reviewer": "r1", "resolved_at": resolved,
            "model_snapshot": {
                "doc_type": model_class, "doc_subclass": None,
                "classification_confidence": 0.8, "extraction_confidence": 0.7,
                "extracted_data": {}, "reviewer_doc_type": model_class,
                "reviewer_confidence": 0.9,
            },
        },
    }


@pytest.fixture
def mdir(tmp_path):
    d = tmp_path / "manifests"
    d.mkdir()
    rows = [
        _manifest("wasted", model_class="contract", human_class="contract"),
        _manifest("reclass", model_class="contract", human_class="correspondence",
                  resolution="reclassify", resolved="2026-05-31T10:00:00+00:00"),
        _manifest("fieldfix", model_class="contract", human_class="contract",
                  resolution="edit_fields", corrections={"effective_date": "2026-01-01",
                                                         "parties": ["A"]},
                  resolved="2026-06-01T09:00:00+00:00"),
        _manifest("human-extract", model_class="contract", human_class="contract",
                  resolution="edit_fields", corrections={"parties": ["B"]},
                  extraction_source="human", resolved="2026-06-01T10:00:00+00:00"),
    ]
    for r in rows:
        (d / f"{r['doc_id']}.json").write_text(json.dumps(r))
    (d / "plain.json").write_text(json.dumps({"doc_id": "plain", "stage": "archived"}))
    (d / "broken.json").write_text("{not json")
    return d


def test_export_writes_run_records_with_human_gold(mdir, tmp_path):
    out = tmp_path / "labels.jsonl"
    summary = exp.export(mdir, out=out, today=dt.date(2026, 6, 2))
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 4 == summary["n_labels"]
    rec = RunRecord.model_validate(next(l for l in lines if l["doc_id"] == "reclass"))
    assert rec.run.run_id == "human-labels-2026-06-02" == summary["run_id"]
    assert rec.run.mode == "real" and rec.observed.full_information is False
    assert rec.gold.label_source == "human"
    assert rec.gold.expected_doc_type == "correspondence"
    assert rec.sorter[0].doc_type == "contract" and rec.sorter[0].confidence == 0.8
    assert rec.reviewer.ran and rec.reviewer.reviewer_confidence == 0.9
    assert rec.reviewer.causes == ["sorter_low_confidence"]


def test_human_extraction_is_excluded_from_confidence_pairs(mdir, tmp_path):
    out = tmp_path / "labels.jsonl"
    exp.export(mdir, out=out)
    by_id = {r["doc_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert by_id["human-extract"]["extraction"] == []
    assert len(by_id["fieldfix"]["extraction"]) == 1


def test_summary_rates_and_counts(mdir):
    s = exp.summarize(exp.load_labelled_manifests(mdir))
    assert s["n_labels"] == 4
    assert s["wasted_reviews"] == 1 and s["wasted_review_rate"] == 0.25
    assert s["corrections"] == 3 and s["correction_rate"] == 0.75
    assert s["per_class_agreement"]["contract"] == {"n": 4, "agree": 3, "agreement": 0.75}
    assert s["field_edit_counts"] == {"parties": 2, "effective_date": 1}
    assert s["resolutions"]["approve_as_is"] == 1


def test_since_filters_by_resolution_time(mdir):
    since = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    ids = [m["doc_id"] for m in exp.load_labelled_manifests(mdir, since=since)]
    assert ids == ["fieldfix", "human-extract"]


def test_rejected_without_class_is_not_a_wasted_review_or_agreement(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    row = _manifest("rej", model_class="contract", human_class=None, decision="rejected",
                    resolution="reject", stage="failed")
    (d / "rej.json").write_text(json.dumps(row))
    s = exp.summarize(exp.load_labelled_manifests(d))
    assert s["wasted_reviews"] == 0 and s["per_class_agreement"] == {}


def test_main_summary_only_prints_json(mdir, capsys):
    assert exp.main(["--manifests-dir", str(mdir), "--summary-only"]) == 0
    assert json.loads(capsys.readouterr().out)["n_labels"] == 4
    assert exp.main(["--manifests-dir", str(mdir), "--since", "nonsense"]) == 2


# ------------------------------------------------------- run record fields

def test_to_review_record_emits_human_keys():
    rec = RunRecord(
        run={"run_id": "r", "mode": "real"}, doc_id="d",
        gold=Gold(expected_doc_type="contract", human_decision="approved",
                  human_doc_type="contract", human_doc_subclass="nda", label_source="human"),
    )
    row = to_review_record(rec)
    assert (row["human_decision"], row["human_doc_type"], row["human_doc_subclass"]) == (
        "approved", "contract", "nda")
    from llm_dojo_scoring.scoring.reviewer import ReviewRecord

    parsed = ReviewRecord.from_mapping(row)
    assert parsed.human_decision == "approved" and parsed.human_doc_type == "contract"


def test_corpus_gold_still_validates_without_human_fields():
    gold = Gold(expected_doc_type="contract")
    assert gold.label_source is None and gold.human_fields is None
