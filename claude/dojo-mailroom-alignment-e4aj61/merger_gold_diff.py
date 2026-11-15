"""Differential probe: mailroom merger gold vs Dojo 0.30.0 hub_merger_expected_fields.

Synthetic Hub merger rows only. Run from the mailroom worktree with the Dojo 0.30.0
overlay on PYTHONPATH and `src` importable.
"""
import json
import sys

sys.path.insert(0, "src")

from llm_dojo_scoring import get_suite  # noqa: E402
from llm_dojo_scoring.schemas.maud_schema import hub_merger_expected_fields  # noqa: E402
from observability.extraction_gt import catalog_expected_fields  # noqa: E402

TOC = "Type of Consideration"
OTHER_Q = "Accuracy of Target R&W Closing Condition"

CASES = {
    "A agree (cash/cash)": ("all_cash", {TOC: {"answer": "All Cash"}}, None),
    "B subclass vs answer conflict": ("all_cash", {TOC: {"answer": "All Stock"}}, None),
    "C subclass other, answer stock": ("other", {TOC: {"answer": "All Stock"}}, None),
    "D 'nan' answer": ("all_cash", {TOC: {"answer": "All Cash"}, OTHER_Q: {"answer": "nan"}}, None),
    "E gt_ambiguous record": (
        "all_cash",
        {TOC: {"answer": "All Cash"}, OTHER_Q: {"answer": "Accurate in all respects", "gt_ambiguous": True}},
        None,
    ),
    "F multi-answer list": (
        "all_cash",
        {TOC: {"answer": "All Cash"}, OTHER_Q: {"answer": ["Accurate in all respects", "Accurate in all material respects"]}},
        None,
    ),
    "G labels pending_annotation": ("all_stock", {TOC: {"answer": "All Stock"}}, {"maud_clause_labels": "pending_annotation"}),
    "H subclass other, no labels": ("other", {}, None),
}


def mailroom_gold(subclass, labels, presence):
    sample = {"expected_hf_class": "merger_agreement", "expected_subclass": subclass}
    if labels:
        sample["maud_clause_labels"] = json.dumps(labels)
    if presence:
        sample["gt_presence"] = json.dumps(presence)
    got = catalog_expected_fields(sample)
    return {k: got[k] for k in ("maud_clauses", "merger_consideration") if k in got}


def dojo_gold(subclass, labels, presence):
    row = {"expected_subclass": subclass}
    if labels:
        row["maud_clause_labels"] = json.dumps(labels)
    if presence:
        row["gt_presence"] = json.dumps(presence)
    return hub_merger_expected_fields(row, subclass=subclass)


def score(expected, predicted):
    res = get_suite("merger_agreement").score_document(expected=expected, predicted=predicted)
    d = res if isinstance(res, dict) else getattr(res, "__dict__", {})
    keys = [k for k in d if "micro" in k or k in ("overall_score", "score")]
    return {k: d[k] for k in keys} if keys else d


n_diff = 0
for name, (sub, labels, presence) in CASES.items():
    m, d = mailroom_gold(sub, labels, presence), dojo_gold(sub, labels, presence)
    same = m == d
    n_diff += not same
    print(f"\n## {name}: {'SAME' if same else 'DIFFERENT'}")
    print("  mailroom:", m)
    print("  dojo    :", d)
    if not same and d:
        # A prediction that is exactly right by the Dojo's definition, scored against each gold.
        print("  score(pred=dojo gold, exp=mailroom gold):", score(m, d))
        print("  score(pred=dojo gold, exp=dojo gold)    :", score(d, d))
print(f"\n{n_diff} of {len(CASES)} cases differ")
