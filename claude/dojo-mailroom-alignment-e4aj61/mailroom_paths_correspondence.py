"""Correspondence topic/sentiment scoring through the mailroom paths, Dojo 0.28.1 vs 0.30.0. Synthetic rows."""
import json, sys
sys.path.insert(0, "src")
import llm_dojo_scoring as dojo
from llm_dojo_scoring import get_field_types
from observability.extraction_gt import build_expected_fields
from observability.suite_scoring import score_document_payload
from scripts.run_hf_pilot import labelled_fields

BASE = {"expected_hf_class": "correspondence", "expected_subclass": "email", "text": "",
        "subject_matter": "Quarterly budget"}
CASES = {
    "gold_has_topic_pred_matches": ({"content_topic": "finance", "sentiment_label": "neutral"},
                                    {"content_topic": "finance", "sentiment_label": "neutral"}),
    "gold_has_topic_pred_wrong": ({"content_topic": "finance"}, {"content_topic": "legal"}),
    "gold_nan_pred_states_topic": ({"content_topic": "nan", "sentiment_label": "nan"},
                                   {"content_topic": "finance", "sentiment_label": "neutral"}),
    "gold_absent_pred_states_topic": ({}, {"content_topic": "finance"}),
}
out = {"dojo": dojo.__version__}
for name, (gt_extra, pred_extra) in CASES.items():
    sample = {**BASE, **gt_extra}
    fields, meta = build_expected_fields(sample)
    gold = labelled_fields(fields, meta["sources"])
    pred = {"communication_type": "email", "subject_matter": "Quarterly budget", **pred_extra}
    pl = score_document_payload("correspondence", pred, gold, field_types=get_field_types("correspondence")) or {}
    ex = pl.get("extraction")
    out[name] = {"gold_keys": sorted(gold),
                 "overall": getattr(ex, "overall_score", None),
                 "metric_id": pl.get("metric_id"),
                 **{k: pl.get(k) for k in sorted(pl) if k.startswith(("content_topic", "sentiment"))}}
print(json.dumps(out, sort_keys=True, default=str))
