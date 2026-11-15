"""Before/after (Dojo 0.28.1 vs 0.30.0) of the merger scoring paths the mailroom really uses.

Synthetic rows only. Run with `python -I`; src comes from sys.path. Path under test:
  sample -> observability.extraction_gt.build_expected_fields
         -> run_hf_pilot.labelled_fields -> run_hf_pilot.score_row_extraction
         -> observability.suite_scoring.score_document_payload -> Dojo suite.score_document
Prints one JSON object keyed by scenario.
"""
import json
import sys

sys.path.insert(0, "src")

import llm_dojo_scoring as dojo  # noqa: E402
from observability.extraction_gt import build_expected_fields  # noqa: E402
from observability.suite_scoring import score_document_payload  # noqa: E402
from scripts.run_hf_pilot import labelled_fields, score_row_extraction  # noqa: E402
from llm_dojo_scoring import get_field_types  # noqa: E402

LABELS = {
    "Type of Consideration": {"answer": "All Cash"},
    "Breach of No Shop": {"answer": "Yes"},
    "Breach of Meeting Covenant": {"answer": "No"},
}
HUB = {"parties": ["Acme Inc", "Beta LLC"], "governing_law": "Delaware"}

GOLDS = {
    "gold_labels+lines": lambda: {
        "expected_hf_class": "merger_agreement", "expected_subclass": "all_cash", "text": "",
        "maud_clauses": [f"{q}: {v['answer']}" for q, v in LABELS.items()],
        "maud_clause_labels": json.dumps(LABELS), **HUB},
    "gold_lines_only": lambda: {
        "expected_hf_class": "merger_agreement", "expected_subclass": "all_cash", "text": "",
        "maud_clauses": [f"{q}: {v['answer']}" for q, v in LABELS.items()], **HUB},
    "gold_maud_only": lambda: {
        "expected_hf_class": "merger_agreement", "expected_subclass": "", "text": "",
        "maud_clauses": [f"{q}: {v['answer']}" for q, v in LABELS.items()],
        "maud_clause_labels": json.dumps(LABELS)},
    "gold_subclass_only": lambda: {
        "expected_hf_class": "merger_agreement", "expected_subclass": "all_cash", "text": "", **HUB},
}
LINES = ["Type of Consideration: All Cash", "Breach of No Shop: Yes", "Breach of Meeting Covenant: No"]
PREDS = {
    "perfect": {**HUB, "merger_consideration": "all_cash", "maud_clauses": LINES},
    "one_wrong_answer": {**HUB, "merger_consideration": "all_cash",
                         "maud_clauses": [LINES[0], "Breach of No Shop: No", LINES[2]]},
    "all_answers_wrong": {**HUB, "merger_consideration": "all_cash",
                          "maud_clauses": ["Type of Consideration: All Stock", "Breach of No Shop: No",
                                           "Breach of Meeting Covenant: Yes"]},
    "one_question_omitted": {**HUB, "merger_consideration": "all_cash", "maud_clauses": LINES[:2]},
    "bullets_and_case": {**HUB, "merger_consideration": "all_cash",
                         "maud_clauses": ["- type of consideration: all cash", "* BREACH OF NO SHOP: yes",
                                          "1. Breach of Meeting Covenant: No"]},
    "extra_unlabelled_question": {**HUB, "merger_consideration": "all_cash",
                                  "maud_clauses": LINES + ["Breach of Fiduciary Duty: Yes"]},
    "empty_maud_clauses": {**HUB, "merger_consideration": "all_cash", "maud_clauses": []},
}
KEYS = ("overall_score", "n_fields", "needs_judge_review", "metric_id", "field_micro_f1",
        "field_micro_precision", "field_micro_recall", "maud_question_accuracy", "scorer_version",
        "extraction_status", "unscorable_reason")


def _one(gold, pred):
    res = score_row_extraction(pred, gold, "merger_agreement") or {}
    pl = score_document_payload("merger_agreement", pred, gold, field_types=get_field_types("merger_agreement"))
    ex = (pl or {}).get("extraction")
    return {"overall_score": res.get("overall_score"), "metric_id": res.get("metric_id"),
            "field_scores": {k: round(v, 3) for k, v in sorted(ex.field_scores.items())} if ex else None}


def run():
    out = {"dojo": dojo.__version__}
    for gname, make in GOLDS.items():
        sample = make()
        fields, meta = build_expected_fields(sample)
        gold = labelled_fields(fields, meta["sources"])
        out[f"{gname}|gold_keys"] = sorted(gold)
        for pname, pred in PREDS.items():
            if gname == "gold_maud_only":
                pred = {"maud_clauses": pred["maud_clauses"]}
                out[f"{gname}|perfect_with_consideration"] = _one(
                    gold, {"maud_clauses": LINES, "merger_consideration": "all_cash"})
                if pname not in ("perfect", "one_wrong_answer", "all_answers_wrong"):
                    continue
            if gname == "gold_subclass_only" and pname in ("one_wrong_answer", "all_answers_wrong",
                                                          "one_question_omitted", "extra_unlabelled_question"):
                continue
            res = score_row_extraction(pred, gold, "merger_agreement") or {}
            out[f"{gname}|{pname}"] = {k: res.get(k) for k in KEYS if k in res}
            # per-field detail straight from the payload
            pl = score_document_payload("merger_agreement", pred, gold,
                                        field_types=get_field_types("merger_agreement"))
            for k in ("extraction_f1", "extraction_f2", "extraction_precision", "extraction_recall", "entity_list_f1", "metric_id"):
                if pl and k in pl:
                    out[f"{gname}|{pname}"][f"payload_{k}"] = pl[k]
            ex = (pl or {}).get("extraction")
            if ex is not None:
                out[f"{gname}|{pname}"]["field_scores"] = {k: round(v, 3) for k, v in sorted(ex.field_scores.items())}
    # Not a mailroom path (catalog always flattens labels to maud_clauses): the raw-label shape.
    raw_exp = {**HUB, "maud_clause_labels": LABELS}
    for pname in ("perfect", "one_wrong_answer"):
        pl = score_document_payload("merger_agreement", PREDS[pname], raw_exp,
                                    field_types=get_field_types("merger_agreement"))
        ex = (pl or {}).get("extraction")
        out[f"RAW_labels_not_mailroom|{pname}"] = {
            "overall_score": getattr(ex, "overall_score", None), "metric_id": (pl or {}).get("metric_id"),
            "field_scores": {k: round(v, 3) for k, v in sorted((ex.field_scores if ex else {}).items())}}
    return out


print(json.dumps(run(), sort_keys=True))
