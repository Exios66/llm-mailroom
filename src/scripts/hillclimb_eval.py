#!/usr/bin/env python3
"""Score a confidence-threshold policy on run records: the hill climb's objective.

Replays the policy through the real routing functions over ``RunRecord`` JSONL
(``pipeline/policy_replay.py``) and prints metrics, guard verdicts and, as the
very last line, ``SCORE: 0.XXXX``.

    U = 1 - mean(cost per doc) / C_h        higher is better
    cost: auto-archived correct 0 | auto-archived wrong (or unknown) C_e |
          human review or unobserved C_h | + LLM USD / review_cost_usd * C_h

Guards (any failure -> exit 2, the SCORE line is still printed):
    G1 wrong-auto Clopper-Pearson upper bound <= --risk (default 0.02)
    G2 per-class wrong-auto rate <= 0.05 for classes with >= 30 auto-archives
    G3 unobserved share <= 0.01
    G4 mean LLM USD/doc <= --cost-budget-usd (off unless given)

Tune on split=train only; the SCORE is reported on split=test (all records, with
a warning, when the corpus is unsplit). ``--oracle`` replays with the gold class
in routing state (not production-faithful; report it separately).

Exit codes: 0 ok | 2 guard failure | 1 bad input.

Usage::

    PYTHONPATH=src python src/scripts/hillclimb_eval.py --records runs.jsonl \\
        [--policy candidate.yaml] [--baseline-policy base.yaml] [--split test] [--json out.json] \\
        [--merge-rule {min,cr6}] [--baseline-merge-rule {min,cr6}]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline import policy_replay as pr  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 1 = bad input; 2 is reserved for guard failure
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _f(x: float | None, spec: str = ".4f") -> str:
    return "-" if x is None else format(x, spec)


def print_report(label: str, res: Mapping[str, Any], out=None) -> None:
    out = out or sys.stdout
    c = res["outcomes"]
    a = res["auto"]
    print(f"== {label}: {res['n_docs']} docs{' (ORACLE: gold class in routing state)' if res['oracle'] else ''}", file=out)
    print(
        f"  auto-archived {c['auto_archived']} ({_pct(res['stp_rate'])} STP) | "
        f"human review {c['human_review']} ({_pct(res['review_rate'])}) | "
        f"unobserved {c['unobserved']} ({_pct(res['unobserved_rate'])})",
        file=out,
    )
    print(
        f"  auto-archived: correct {a['correct']} wrong {a['wrong']} unscored {a['unscored']} | "
        f"wrong rate {_pct(a['wrong_rate'])} (CP upper {_f(a['wrong_cp_upper'])}) | "
        f"wrong per doc {_pct(a['wrong_rate_of_docs'])}",
        file=out,
    )
    print(
        f"  cost: mean {_f(res['mean_cost_units'])} C_h/doc | LLM ${_f(res['llm_usd_per_doc'])}/doc | "
        f"{_f(res['llm_calls_per_doc'], '.2f')} LLM calls/doc (lower-bound estimate)",
        file=out,
    )
    if res["unobserved_reasons"]:
        print(f"  unobserved reasons: {res['unobserved_reasons']}", file=out)
    print(
        f"  replay fidelity (next to G3): {res['replayed_with_confidence_only_handoff_diff']} route(s) reused an "
        f"extraction from a collapsed candidate whose handoff confidence differed (confidence-only; counted as "
        f"observed, not unobserved)",
        file=out,
    )
    print(
        f"  schema evidence: {res['auto_archived_schema_unknown']} of {c['auto_archived']} auto-archive(s) "
        f"passed with schema_valid unknown (routing blocks only on an explicit False)",
        file=out,
    )
    print(f"  {'class':<18}{'n':>6}{'auto':>6}{'ok':>6}{'wrong':>6}{'unsc':>5}{'human':>6}{'unobs':>6}{'wrong%':>8}{'CP up':>8}", file=out)
    for cls, r in sorted(res["per_class"].items()):
        print(
            f"  {cls:<18}{r['n']:>6}{r['auto_archived']:>6}{r['correct']:>6}{r['wrong']:>6}{r['unscored']:>5}"
            f"{r['human_review']:>6}{r['unobserved']:>6}{_pct(r['wrong_rate']):>8}{_f(r['wrong_cp_upper']):>8}",
            file=out,
        )
    if res["sorter_calibration"]:
        print(f"  sorter attempt-1 calibration: {'class':<18}{'n':>6}{'acc':>7}{'ECE':>7}{'Brier':>7}{'AUROC':>7}", file=out)
        for cls, s in res["sorter_calibration"].items():
            print(
                f"                                {cls:<18}{s['n']:>6}{_f(s['accuracy'], '.3f'):>7}"
                f"{_f(s['ece'], '.3f'):>7}{_f(s['brier'], '.3f'):>7}{_f(s['auroc'], '.3f'):>7}",
                file=out,
            )
    print(f"  U = {_f(res['score'])}", file=out)


def guard_lines(res: Mapping[str, Any], prefix: str = "GUARD") -> list[str]:
    lines = []
    for g in res["guards"]:
        verdict = "SKIP" if g.get("skipped") else ("PASS" if g["pass"] else "FAIL")
        lines.append(f"{prefix} {g['id']} {verdict} {g['detail']}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    ap = _Parser(description=__doc__.split("\n")[0])
    ap.add_argument("--records", required=True, help="RunRecord JSONL")
    ap.add_argument("--policy", help="policy YAML ({confidence: ...} or a bare block); default: taxonomy")
    ap.add_argument("--baseline-policy", help="policy YAML to compare against (paired bootstrap of delta U)")
    ap.add_argument("--split", default="test", choices=pr.SPLITS)
    ap.add_argument("--c-human", type=float, default=1.0)
    ap.add_argument("--c-error", type=float, default=20.0)
    ap.add_argument("--review-cost-usd", type=float, default=4.0)
    ap.add_argument("--field-floor", type=float, default=0.80)
    ap.add_argument("--risk", type=float, default=0.02)
    ap.add_argument("--cost-budget-usd", type=float, default=None)
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--merge-rule", choices=("min", "cr6"), default="min",
                    help="chunk-window confidence merge for the candidate: min over all windows, or "
                         "cr6 = min over windows that produced fields (needs window evidence in the records)")
    ap.add_argument("--baseline-merge-rule", choices=("min", "cr6"), default=None,
                    help="merge rule for the baseline (default: same as --merge-rule). Given without "
                         "--baseline-policy, the baseline is the candidate policy under this rule")
    ap.add_argument("--json", dest="json_out", help="write the full result as JSON")
    args = ap.parse_args(argv)

    try:
        params = pr.ObjectiveParams(
            c_human=args.c_human, c_error=args.c_error, review_cost_usd=args.review_cost_usd,
            field_floor=args.field_floor, risk=args.risk, cost_budget_usd=args.cost_budget_usd,
            oracle=args.oracle,
        )
        params.accept_target  # validates c_error > c_human > 0
        records = pr.load_records(args.records)
        if not records:
            raise ValueError("no records in input")
        selected, warnings = pr.select_split(records, args.split)
        if not selected:
            raise ValueError(f"no records with split={args.split!r}")
        policy = pr.load_policy(args.policy)
        baseline = pr.load_policy(args.baseline_policy) if args.baseline_policy else None
        base_rule = args.baseline_merge_rule or args.merge_rule
        if baseline is None and base_rule != args.merge_rule:
            baseline = policy  # same policy, other merge rule: the CR6 comparison
        result = pr.evaluate(selected, policy, params, merge_rule=args.merge_rule)
        base_result = pr.evaluate(selected, baseline, params, merge_rule=base_rule) if baseline is not None else None
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    modes = sorted({r.run.mode for r in selected})
    if "synthetic" in modes:
        print("NOTE: SYNTHETIC records -- harness validation only, not evidence about any model.")
    print(f"merge rule: candidate={args.merge_rule}" + (f" baseline={base_rule}" if base_result is not None else ""))
    print(f"records: {len(selected)} (split={args.split}, modes={modes}); accept target 1 - C_h/C_e = {params.accept_target:.3f}")
    print_report("candidate" if base_result is not None else "policy", result)

    delta = None
    if base_result is not None:
        print_report("baseline", base_result)
        delta = pr.paired_delta(result, base_result, params.c_human)
        if delta is None:
            print("DELTA: not measurable (fewer than 2 paired documents)")
        else:
            verdict = "significant" if delta["significant"] else "not significant"
            print(
                f"DELTA U (candidate - baseline) = {delta['delta']:+.4f}  95% CI "
                f"[{delta['ci_lo']:+.4f}, {delta['ci_hi']:+.4f}]  ({verdict}; paired bootstrap, n={delta['n_paired']})"
            )
        for line in guard_lines(base_result, prefix="BASELINE GUARD"):
            print(line)

    for line in guard_lines(result):
        print(line)

    if args.json_out:
        payload = {
            "candidate": pr.result_for_json(result),
            "baseline": pr.result_for_json(base_result) if base_result is not None else None,
            "delta": delta,
            "split": args.split,
            "warnings": warnings,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"SCORE: {result['score']:.4f}")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
