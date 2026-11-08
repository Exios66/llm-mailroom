#!/usr/bin/env python3
"""Propose per-class confidence cutoffs from ``RunRecord`` JSONL, with an evidence gate.

Reads per-document run records (``schemas/run_record.py``), fits cutoffs on the
TRAIN split only, and writes a *candidate* policy YAML. It never edits
``src/config/taxonomy.yaml``; the candidate is judged by ``hillclimb_eval.py``
(replay, guards, SCORE) on the held-out split.

What is proposed, per class, and from which pairs::

    high             sorter attempt 1: (confidence routing saw, class == gold)
                     -> cross_fit_cutoffs at target = 1 - C_h / C_e (0.95 at the defaults)
    judge_band_high  final extraction attempt: (confidence, extraction_score >= field_floor),
                     class-correct documents only -> the same cross-fit at the same target;
                     in routing this is the auto-archive cut (>= goes straight to the report)
    low              the same extraction pairs at ``--low-target`` (retry / judge boundary);
                     note ``low`` is one key shared by the sorter and extraction bands, and
                     only extraction evidence moves it

Evidence gate (a class is NEVER given a cutoff it has not earned): the cross-fit
held-out Wilson lower bound must reach the target, and the number of accepted
documents must meet ``required_samples(errors, risk)`` (Clopper-Pearson, ``--risk``).
Anything else keeps the current taxonomy value and is marked
``insufficient_evidence`` with the reason and the sample shortfall.

The reviewer lane is scored with the Dojo's ``score_review_lane`` (evidence only;
no reviewer cutoff is proposed).

Usage::

    PYTHONPATH=src python src/scripts/calibrate_thresholds.py \\
        --records runs.jsonl --out candidate_policy.yaml [--split train] [--c-error 20]
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

import yaml  # noqa: E402

from pipeline import policy_replay as pr  # noqa: E402
from schemas.run_record import RunRecord, to_review_record  # noqa: E402

CLAMP_LO, CLAMP_HI = 0.5, 0.999


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 1 = bad input (2 is reserved for guard failure elsewhere)
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _ceil3(x: float) -> float:
    return math.ceil(x * 1000.0 - 1e-9) / 1000.0


def effective_value(policy: Mapping[str, Any], cls: str, key: str) -> float | None:
    """The value routing would use for ``cls``: the by_class entry, else the global."""
    entry = (policy.get("by_class") or {}).get(cls) or {}
    value = entry.get(key, policy.get(key))
    return float(value) if value is not None else None


def cutoff_evidence(
    pairs: Sequence[tuple[float, bool]],
    target: float,
    *,
    folds: int,
    seed: int,
    confidence: float,
    risk: float | None,
    min_accepted: int = 0,
    target_step: float = 0.0025,
    max_target: float = 0.999,
) -> dict[str, Any]:
    """The lowest cutoff whose held-out evidence earns ``target``.

    ``cutoff_at_target`` returns the lowest cutoff whose accepted SET reaches the
    tuning target, so on smooth data that set sits AT the target and its held-out
    Wilson lower bound falls just under it: gating a cutoff tuned at ``target``
    against ``target`` itself almost never passes. So the cutoff is TUNED at
    successively higher accuracy targets (``target_step`` increments, up to
    ``max_target``) and GATED against the requested ``target``: the first tuning
    target whose cross-fit held-out Wilson lower bound exceeds ``target`` (and
    whose accepted set meets the sample-size rule) wins; ``effective_target`` is
    that tuning target. ``target_step=0`` disables escalation. When nothing
    passes, the evidence at the requested target is returned, with the reason.
    """
    first = _evidence_at(pairs, target, target, folds=folds, seed=seed, confidence=confidence,
                         risk=risk, min_accepted=min_accepted)
    if first["status"] == "calibrated" or target_step <= 0 or len(pairs) < 2:
        return first
    t = target + target_step
    while t <= max_target + 1e-12:
        ev = _evidence_at(pairs, round(t, 6), target, folds=folds, seed=seed, confidence=confidence,
                          risk=risk, min_accepted=min_accepted)
        if ev["status"] == "calibrated":
            ev["escalated_from"] = first["reason"]
            return ev
        t += target_step
    first["reason"] = f"{first['reason']}; no tuning target up to {max_target} passes the gate"
    return first


def _evidence_at(
    pairs: Sequence[tuple[float, bool]],
    tune_target: float,
    target: float,
    *,
    folds: int,
    seed: int,
    confidence: float,
    risk: float | None,
    min_accepted: int = 0,
) -> dict[str, Any]:
    """Tune a cutoff at ``tune_target``; decide whether the evidence earns ``target``.

    ``risk`` set: the accepted set must satisfy ``required_samples(errors, risk)``.
    ``risk=None``: only ``min_accepted`` is required (used for the retry/judge ``low``).
    """
    from llm_dojo_scoring.analysis.calibration import (
        auroc,
        brier_score,
        cross_fit_cutoffs,
        cutoff_at_target,
        expected_calibration_error,
        required_samples,
    )

    n = len(pairs)
    out: dict[str, Any] = {
        "n": n,
        "accuracy": (sum(1 for _, ok in pairs if ok) / n) if n else None,
        "ece": expected_calibration_error(pairs) if n else None,
        "brier": brier_score(pairs) if n else None,
        "auroc": auroc([c for c, _ in pairs], [ok for _, ok in pairs]) if n else None,
        "target": target,
        "effective_target": tune_target,
        "cutoff": None,
        "proposed": None,
        "cross_fit": None,
        "n_accepted": 0,
        "errors": 0,
        "required": None,
        "shortfall": None,
        "status": "insufficient_evidence",
        "reason": None,
    }
    if n < 2:
        out["reason"] = "too_few_documents"
        out["shortfall"] = None
        return out
    cf = cross_fit_cutoffs(list(pairs), target=tune_target, folds=min(folds, n), seed=seed, confidence=confidence)
    out["cross_fit"] = cf
    cutoff = cutoff_at_target(list(pairs), tune_target)
    out["cutoff"] = cutoff
    if cutoff is None:
        out["reason"] = "no_cutoff_reaches_target"
        return out
    proposed = min(max(_ceil3(cutoff), CLAMP_LO), CLAMP_HI)
    out["clamped"] = proposed != _ceil3(cutoff)
    accepted = [ok for c, ok in pairs if c >= proposed]
    out["n_accepted"] = len(accepted)
    out["errors"] = sum(1 for ok in accepted if not ok)
    if risk is not None:
        required = required_samples(out["errors"], risk=risk, confidence=confidence)
    else:
        required = min_accepted
    out["required"] = required
    out["shortfall"] = None if required is None else max(0, required - len(accepted))
    lo = (cf.get("holdout_accuracy_ci") or {}).get("lo")
    # `lo` is rounded to 4 places by the Dojo; strict > keeps rounding from lifting a short bound.
    if lo is None or not lo > target:
        out["reason"] = "holdout_wilson_lower_below_target"
    elif required is None:
        out["reason"] = "no_sample_size_clears_risk"
    elif out["shortfall"] > 0:
        out["reason"] = "sample_shortfall"
    else:
        out["status"] = "calibrated"
        out["proposed"] = proposed
    return out


def _reviewer_summary(records: Sequence[RunRecord], base_policy: Mapping[str, Any], *, risk: float,
                      confidence: float, target: float) -> dict[str, dict[str, Any]]:
    from llm_dojo_scoring.scoring.reviewer import score_review_lane

    by_class: dict[str, list[dict[str, Any]]] = {}
    with pr.policy_override(base_policy):
        for rec in records:
            if not rec.reviewer.ran or not rec.sorter:
                continue
            first = next((a for a in rec.sorter if a.attempt == 1), None)
            if first is None or first.doc_type is None:
                continue
            row = to_review_record(rec)
            row["route"] = pr.reviewer_route(rec)
            by_class.setdefault(first.doc_type, []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for cls, rows in sorted(by_class.items()):
        card = score_review_lane(rows, risk=risk, confidence=confidence, target_accuracy=target)
        acc = card.get("acceptance", {})
        summary[cls] = {
            "n": card.get("n"),
            "class_accuracy": card.get("quality", {}).get("class_accuracy"),
            "ece": card.get("calibration", {}).get("ece"),
            "brier": card.get("calibration", {}).get("brier"),
            **{
                k: acc.get(k)
                for k in (
                    "n_accepted",
                    "n_wrong_accepts",
                    "wrong_accept_rate",
                    "wrong_accept_upper_95",
                    "required_accepts",
                    "sample_shortfall",
                    "meets_risk_bar",
                )
            },
        }
    return summary


def calibrate(
    records: Sequence[RunRecord],
    base_policy: Mapping[str, Any],
    *,
    c_human: float = 1.0,
    c_error: float = 20.0,
    folds: int = 5,
    seed: int = 42,
    risk: float = 0.02,
    confidence: float = 0.95,
    field_floor: float = 0.80,
    low_target: float = 0.90,
    min_low_n: int = 30,
    target_step: float = 0.0025,
) -> dict[str, Any]:
    """Fit cutoffs on ``records`` (pass the TRAIN split) and build the candidate policy."""
    target = pr.accept_target(c_human, c_error)
    sorter = pr.sorter_pairs(records)
    extraction = pr.extraction_pairs(records, field_floor)
    classes = sorted(set(sorter) | set(extraction))
    candidate = copy.deepcopy(dict(base_policy))
    by_class = candidate.setdefault("by_class", {})
    report: dict[str, Any] = {}
    kw = dict(folds=folds, seed=seed, confidence=confidence)

    for cls in classes:
        entry: dict[str, Any] = {}
        high = cutoff_evidence(sorter.get(cls, []), target, risk=risk, target_step=target_step, **kw)
        high["current"] = effective_value(base_policy, cls, "high")
        entry["high"] = high

        jbh = cutoff_evidence(extraction.get(cls, []), target, risk=risk, target_step=target_step, **kw)
        jbh["current"] = effective_value(base_policy, cls, "judge_band_high")
        entry["judge_band_high"] = jbh

        low = cutoff_evidence(
            extraction.get(cls, []), low_target, risk=None, min_accepted=min_low_n,
            target_step=target_step, max_target=target, **kw
        )
        low["current"] = effective_value(base_policy, cls, "low")
        # `low` must sit under the auto-archive cut that will be in force.
        band_high = jbh["proposed"] if jbh["status"] == "calibrated" else jbh["current"]
        if low["status"] == "calibrated" and band_high is not None and low["proposed"] >= band_high:
            low.update(status="insufficient_evidence", proposed=None, reason="low_not_below_judge_band_high")
        entry["low"] = low

        changed: dict[str, float] = {}
        for key in ("high", "judge_band_high", "low"):
            if entry[key]["status"] == "calibrated":
                changed[key] = entry[key]["proposed"]
        if changed:
            merged = dict(by_class.get(cls) or {})
            merged.update(changed)
            by_class[cls] = merged
        report[cls] = entry

    return {
        "params": {
            "c_human": c_human,
            "c_error": c_error,
            "target": target,
            "folds": folds,
            "seed": seed,
            "risk": risk,
            "confidence": confidence,
            "field_floor": field_floor,
            "low_target": low_target,
            "min_low_n": min_low_n,
            "target_step": target_step,
        },
        "classes": report,
        "reviewer": _reviewer_summary(records, base_policy, risk=risk, confidence=confidence, target=target),
        "candidate_policy": candidate,
    }


def _fmt(x: Any, spec: str = ".3f") -> str:
    return "-" if x is None else format(x, spec)


def print_table(result: Mapping[str, Any], out=None) -> None:
    out = out or sys.stdout
    p = result["params"]
    print(
        f"target accuracy = 1 - C_h/C_e = {p['target']:.3f} | risk {p['risk']} | "
        f"folds {p['folds']} seed {p['seed']} | field_floor {p['field_floor']}",
        file=out,
    )
    header = (
        f"{'class':<18}{'cutoff':<16}{'n':>6}{'acc':>7}{'ece':>7}{'auroc':>7}"
        f"{'cutoff':>8}{'effT':>7}{'holdout':>9}{'CI lo':>7}{'accept':>7}{'err':>5}{'need':>6}{'short':>6}"
        f"{'current':>9}{'proposed':>9}  status"
    )
    print(header, file=out)
    for cls, entry in result["classes"].items():
        for key in ("high", "judge_band_high", "low"):
            e = entry[key]
            cf = e["cross_fit"] or {}
            ci = (cf.get("holdout_accuracy_ci") or {}).get("lo")
            status = e["status"] if e["status"] == "calibrated" else f"insufficient_evidence ({e['reason']})"
            print(
                f"{cls:<18}{key:<16}{e['n']:>6}{_fmt(e['accuracy']):>7}{_fmt(e['ece']):>7}"
                f"{_fmt(e['auroc']):>7}{_fmt(e['cutoff'], '.4f'):>8}{_fmt(e.get('effective_target'), '.4f'):>7}{_fmt(cf.get('holdout_accuracy')):>9}"
                f"{_fmt(ci):>7}{e['n_accepted']:>7}{e['errors']:>5}{_fmt(e['required'], 'd'):>6}"
                f"{_fmt(e['shortfall'], 'd'):>6}{_fmt(e['current'], '.3f'):>9}{_fmt(e['proposed'], '.3f'):>9}  {status}",
                file=out,
            )
    if result["reviewer"]:
        print("\nreviewer lane (score_review_lane; evidence only):", file=out)
        for cls, r in result["reviewer"].items():
            print(
                f"  {cls:<18} n={r['n']} class_acc={_fmt(r['class_accuracy'])} accepted={r['n_accepted']} "
                f"wrong={r['n_wrong_accepts']} upper95={_fmt(r['wrong_accept_upper_95'], '.4f')} "
                f"shortfall={r['sample_shortfall']} meets_risk_bar={r['meets_risk_bar']}",
                file=out,
            )


def write_candidate(result: Mapping[str, Any], path: str | Path, *, meta: Mapping[str, Any]) -> None:
    """Write ``{confidence: ...}`` (what the loader reads) plus an audit ``calibration:`` block."""
    audit = {
        "generated_by": "scripts/calibrate_thresholds.py",
        **meta,
        "params": result["params"],
        "classes": {
            cls: {
                key: {
                    "status": e["status"],
                    "reason": e["reason"],
                    "n": e["n"],
                    "n_accepted": e["n_accepted"],
                    "errors": e["errors"],
                    "required": e["required"],
                    "shortfall": e["shortfall"],
                    "cutoff": e["cutoff"],
                    "effective_target": e.get("effective_target"),
                    "current": e["current"],
                    "proposed": e["proposed"],
                    "holdout_accuracy": (e["cross_fit"] or {}).get("holdout_accuracy"),
                    "holdout_accuracy_ci_lo": ((e["cross_fit"] or {}).get("holdout_accuracy_ci") or {}).get("lo"),
                }
                for key, e in entry.items()
            }
            for cls, entry in result["classes"].items()
        },
    }
    doc = {"confidence": result["candidate_policy"], "calibration": audit}
    Path(path).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    ap = _Parser(description=__doc__.split("\n")[0])
    ap.add_argument("--records", required=True, help="RunRecord JSONL")
    ap.add_argument("--out", required=True, help="candidate policy YAML to write")
    ap.add_argument("--base-policy", help="policy YAML to start from (default: taxonomy confidence block)")
    ap.add_argument("--split", default="train", choices=pr.SPLITS)
    ap.add_argument("--c-human", type=float, default=1.0)
    ap.add_argument("--c-error", type=float, default=20.0)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--risk", type=float, default=0.02, help="wrong-auto ceiling for the sample-size gate")
    ap.add_argument("--field-floor", type=float, default=0.80)
    ap.add_argument("--low-target", type=float, default=0.90, help="accuracy target for the retry/judge `low`")
    ap.add_argument("--min-low-n", type=int, default=30)
    ap.add_argument("--target-step", type=float, default=0.0025,
                    help="raise the accuracy target by this step until the evidence gate passes (0 = strict, no escalation)")
    args = ap.parse_args(argv)

    try:
        target = pr.accept_target(args.c_human, args.c_error)
        records = pr.load_records(args.records)
        if not records:
            raise ValueError("no records in input")
        base = pr.load_policy(args.base_policy)
        selected, warnings = pr.select_split(records, args.split)
        gt = [r for r in selected if r.run.gt_in_state]
        if gt:
            warnings.append(f"excluded {len(gt)} record(s) with gt_in_state=True (ground truth in state changes routing)")
            selected = [r for r in selected if not r.run.gt_in_state]
        if not selected:
            raise ValueError(f"no usable records for split={args.split!r}")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    modes = Counter(r.run.mode for r in selected)
    if modes.get("synthetic"):
        print("NOTE: SYNTHETIC records -- harness validation only, not evidence about any model.")
    print(f"calibrating on {len(selected)} records (split={args.split}, modes={dict(modes)})")

    result = calibrate(
        selected, base,
        c_human=args.c_human, c_error=args.c_error, folds=args.folds, seed=args.seed,
        risk=args.risk, field_floor=args.field_floor, low_target=args.low_target, min_low_n=args.min_low_n,
        target_step=args.target_step,
    )
    print_table(result)
    write_candidate(
        result, args.out,
        meta={"split": args.split, "n_records": len(selected), "modes": dict(modes), "target": target},
    )
    n_cal = sum(1 for e in result["classes"].values() for k in e.values() if k["status"] == "calibrated")
    n_all = sum(len(e) for e in result["classes"].values())
    print(f"\nwrote {args.out}: {n_cal}/{n_all} cutoffs calibrated, the rest keep their current value")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
