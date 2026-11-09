#!/usr/bin/env python3
"""Propose per-class confidence cutoffs from ``RunRecord`` JSONL, with an evidence gate.

Reads per-document run records (``schemas/run_record.py``), fits cutoffs on the
TRAIN split only, and writes a *candidate* policy YAML. It never edits
``src/config/taxonomy.yaml``; the candidate is judged by ``hillclimb_eval.py``
(replay, guards, SCORE) on the held-out split.

What is proposed, per class, and from which pairs::

    high             sorter attempt 1: (confidence routing saw, class == gold), one pair per
                     DOCUMENT -> cross_fit_cutoffs at target = 1 - C_h / C_e (0.95 at the defaults)
    judge_band_high  EVERY scored extraction attempt of the gold class: (confidence,
                     extraction_score >= field_floor), class-correct documents only (sorter
                     attempt 1 named gold) -> the same cross-fit at the same target; in routing
                     this is the auto-archive cut (>= goes straight to the report). Routing can
                     accept attempt 1 before a retry runs, so the final attempt alone would
                     overstate accuracy (``policy_replay.extraction_pairs``)
    low              the same extraction pairs at ``--low-target`` (retry / judge boundary);
                     note ``low`` is one key shared by the sorter and extraction bands, and
                     only extraction evidence moves it

Extraction evidence is counted in ATTEMPTS (``unit: attempts``): a document with three scored
attempts is three pairs, so ``n``, ``n_accepted`` and ``errors`` for ``judge_band_high`` / ``low``
are attempts, and the cross-fit accuracy is per attempt. Attempts of one document are correlated,
so the SAMPLE-SIZE rule (``required``, ``shortfall``, ``--min-low-n``) counts distinct documents
instead: ``n_accepted_docs`` (documents with an accepted attempt) and ``errors_docs`` (documents
with a wrong accepted attempt). ``n_docs`` is the number of documents behind ``n``.

Evidence gate (a class is NEVER given a cutoff it has not earned): the cross-fit
held-out Wilson lower bound (scored on the cutoff as emitted: rounded up to 3 places and
clamped, not the raw tuned value) must reach the target, and the number of accepted
documents must meet ``required_samples(errors, risk)`` (Clopper-Pearson, ``--risk``); for the
extraction keys that is distinct documents, not attempts.
Anything else keeps the current taxonomy value and is marked
``insufficient_evidence`` with the reason and the sample shortfall. A calibrated cut that
would leave the emitted policy with ``low`` at or above ``high`` or ``judge_band_high`` is
refused too (``low_not_below_*`` / ``*_not_above_low``).

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
import random
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


def deployed_cutoff(cutoff: float) -> float:
    """The cutoff as it is emitted in the candidate policy: rounded up to 3 places, then clamped."""
    return min(max(_ceil3(cutoff), CLAMP_LO), CLAMP_HI)


def deployed_cross_fit(
    pairs: Sequence[tuple[float, bool]],
    tune_target: float,
    *,
    folds: int,
    seed: int,
    confidence: float,
    docs: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Held-out accuracy of the cutoff AS EMITTED (rounded up and clamped), not the raw tuned one.

    The Dojo's ``cross_fit_cutoffs`` tunes each fold's cutoff on the other folds and scores the
    held-out fold with that raw value. The policy carries ``deployed_cutoff(cutoff)`` instead, which
    can accept a different set (the clamp at ``CLAMP_HI`` widens it). The folds are the Dojo's own
    (same seeded shuffle, ``fold::folds`` slices); only the fold cutoff is transformed, so the
    evidence gate judges the set that would actually be deployed.

    ``docs`` (parallel to ``pairs``, the document behind each pair) switches to DOCUMENT-grouped
    folds for evidence whose pairs are extraction attempts: every attempt of a document sits in the
    same fold (no train/held-out leakage between a document's attempts), and the held-out unit is
    the document, not the attempt: a held-out document counts once if any of its attempts clears
    the fold cutoff, and counts as right only if every such attempt is right (routing would have
    accepted a wrong one). ``holdout_accepted`` and the Wilson interval are then in documents, so
    correlated attempts cannot narrow the interval.
    """
    from statistics import NormalDist

    from llm_dojo_scoring.analysis.calibration import cutoff_at_target, wilson_ci

    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    if docs is not None:
        ids = sorted(set(docs))
        random.Random(seed).shuffle(ids)
        fold_of = {d: i % folds for i, d in enumerate(ids)}
        right = kept = 0
        for fold in range(folds):
            training = [row for row, d in zip(pairs, docs) if fold_of[d] != fold]
            fold_cutoff = cutoff_at_target(training, tune_target)
            if fold_cutoff is None:
                continue
            cut = deployed_cutoff(fold_cutoff)
            verdict: dict[int, bool] = {}
            for (conf, ok), d in zip(pairs, docs):
                if fold_of[d] == fold and conf >= cut:
                    verdict[d] = verdict.get(d, True) and ok
            right += sum(1 for v in verdict.values() if v)
            kept += len(verdict)
        return {
            "holdout_accepted": kept,
            "holdout_accuracy": right / kept if kept else None,
            "holdout_accuracy_ci": wilson_ci(right / kept, kept, z=z) if kept else None,
            "holdout_share_accepted": kept / len(ids) if ids else None,
        }

    shuffled = list(pairs)
    random.Random(seed).shuffle(shuffled)
    right = kept = 0
    for fold in range(folds):
        held = shuffled[fold::folds]
        training = [row for index, row in enumerate(shuffled) if index % folds != fold]
        fold_cutoff = cutoff_at_target(training, tune_target)
        if fold_cutoff is None:
            continue
        cut = deployed_cutoff(fold_cutoff)
        accepted = [ok for conf, ok in held if conf >= cut]
        right += sum(1 for ok in accepted if ok)
        kept += len(accepted)
    return {
        "holdout_accepted": kept,
        "holdout_accuracy": right / kept if kept else None,
        "holdout_accuracy_ci": wilson_ci(right / kept, kept, z=z) if kept else None,
        "holdout_share_accepted": kept / len(shuffled) if shuffled else None,
    }


def effective_value(policy: Mapping[str, Any], cls: str, key: str) -> float | None:
    """The value routing would use for ``cls``: the by_class entry, else the global."""
    entry = (policy.get("by_class") or {}).get(cls) or {}
    value = entry.get(key)
    if value is None:  # routing skips a null class override (pipeline.config.get_confidence_thresholds)
        value = policy.get(key)
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
    unit: str = "documents",
    docs: Sequence[int] | None = None,
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

    ``unit`` names what one pair is (``documents`` or ``attempts``) in the counts and in the
    ``too_few_<unit>`` reason; ``docs`` (parallel to ``pairs``) identifies the document behind
    each pair so ``n_docs`` / ``n_accepted_docs`` can be reported separately from attempts.
    """
    first = _evidence_at(pairs, target, target, folds=folds, seed=seed, confidence=confidence,
                         risk=risk, min_accepted=min_accepted, unit=unit, docs=docs)
    if first["status"] == "calibrated" or target_step <= 0 or len(pairs) < 2:
        return first
    t = target + target_step
    while t <= max_target + 1e-12:
        ev = _evidence_at(pairs, round(t, 6), target, folds=folds, seed=seed, confidence=confidence,
                          risk=risk, min_accepted=min_accepted, unit=unit, docs=docs)
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
    unit: str = "documents",
    docs: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Tune a cutoff at ``tune_target``; decide whether the evidence earns ``target``.

    ``risk`` set: the accepted set must satisfy ``required_samples(errors, risk)``.
    ``risk=None``: only ``min_accepted`` is required (used for the retry/judge ``low``).
    Both rules count DOCUMENTS (see ``n_accepted_docs`` / ``errors_docs``), also when a pair is
    one extraction attempt.
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
        "unit": unit,
        "n_docs": len(set(docs)) if docs is not None else n,
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
        "n_accepted_docs": 0,
        "errors": 0,
        "errors_docs": 0,
        "required": None,
        "shortfall": None,
        "status": "insufficient_evidence",
        "reason": None,
    }
    if n < 2:
        out["reason"] = f"too_few_{unit}"
        out["shortfall"] = None
        return out
    cf = cross_fit_cutoffs(list(pairs), target=tune_target, folds=min(folds, n), seed=seed, confidence=confidence)
    if cf.get("cutoff") is not None:
        # Gate on the cutoff as emitted (rounded up, clamped), not the raw tuned value.
        cf = {k: v for k, v in cf.items() if k != "meets_target"}  # raw-cutoff verdict; the gate below decides
        cf = {**cf, **deployed_cross_fit(
            pairs, tune_target, folds=min(folds, n), seed=seed, confidence=confidence, docs=docs
        )}
    out["cross_fit"] = cf
    cutoff = cutoff_at_target(list(pairs), tune_target)
    out["cutoff"] = cutoff
    if cutoff is None:
        out["reason"] = "no_cutoff_reaches_target"
        return out
    proposed = deployed_cutoff(cutoff)
    out["clamped"] = proposed != _ceil3(cutoff)
    keep = [i for i, (c, _) in enumerate(pairs) if c >= proposed]
    accepted = [pairs[i][1] for i in keep]
    out["n_accepted"] = len(accepted)
    out["errors"] = sum(1 for ok in accepted if not ok)
    # The sample-size rule counts independent documents. Attempts of one document are correlated,
    # so a unit of ``attempts`` is gated on distinct documents: n = documents with an accepted
    # attempt, errors = documents with a wrong accepted attempt (a document routing would have
    # archived wrong). For ``documents`` evidence the two views are the same numbers.
    if docs is None:
        gate_n, gate_errors = len(accepted), out["errors"]
    else:
        gate_n = len({docs[i] for i in keep})
        gate_errors = len({docs[i] for i in keep if not pairs[i][1]})
    out["n_accepted_docs"], out["errors_docs"] = gate_n, gate_errors
    if risk is not None:
        required = required_samples(gate_errors, risk=risk, confidence=confidence)
    else:
        required = min_accepted
    out["required"] = required
    out["shortfall"] = None if required is None else max(0, required - gate_n)
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


def _in_force(ev: Mapping[str, Any]) -> float | None:
    """The value the candidate policy will carry for one key: the calibrated cut, else the current one."""
    return ev["proposed"] if ev["status"] == "calibrated" else ev["current"]


def _demote(ev: dict[str, Any], reason: str) -> None:
    ev.update(status="insufficient_evidence", proposed=None, reason=reason)


def _reconcile_bands(entry: dict[str, dict[str, Any]]) -> None:
    """Keep ``low < judge_band_high`` and ``low < high`` in the values the candidate will carry.

    ``low`` is one key shared by the sorter band (retry below it, ``high`` and up accepted) and the
    extraction band (the judge fires between ``low`` and ``judge_band_high``), so an inverted pair
    breaks routing in either lane. The comparison uses the value IN FORCE for each key (calibrated
    cut, else the current value), so a calibrated ``high`` or ``judge_band_high`` that lands under
    an uncalibrated ``low`` is caught too. A calibrated ``low`` is demoted first (the weaker, more
    shared evidence); otherwise the calibrated upper cut is. A pair already inverted in the base
    policy with nothing calibrated is left as it is (nothing here caused it).
    """
    low = entry["low"]
    for upper_key, reason_low, reason_upper in (
        ("judge_band_high", "low_not_below_judge_band_high", "judge_band_high_not_above_low"),
        ("high", "low_not_below_high", "high_not_above_low"),
    ):
        upper = entry[upper_key]
        # Demoting `low` falls back to its current value, which can still sit above a calibrated
        # upper cut, so look again before moving on.
        for _ in range(2):
            lo_v, up_v = _in_force(low), _in_force(upper)
            if lo_v is None or up_v is None or lo_v < up_v:
                break
            if low["status"] == "calibrated":
                _demote(low, reason_low)
            elif upper["status"] == "calibrated":
                _demote(upper, reason_upper)
            else:
                break


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
    extraction, extraction_docs = pr.extraction_evidence(records, field_floor)
    classes = sorted(set(sorter) | set(extraction))
    candidate = copy.deepcopy(dict(base_policy))
    # A present-but-null "by_class" would ride the whole calibration and only crash at the first
    # merged class; routing reads null as {} (pipeline/config.py), so normalize it up front.
    by_class = candidate.get("by_class") or {}
    candidate["by_class"] = by_class
    report: dict[str, Any] = {}
    kw = dict(folds=folds, seed=seed, confidence=confidence)

    for cls in classes:
        entry: dict[str, Any] = {}
        high = cutoff_evidence(sorter.get(cls, []), target, risk=risk, target_step=target_step, **kw)
        high["current"] = effective_value(base_policy, cls, "high")
        entry["high"] = high

        ext_kw = dict(unit="attempts", docs=extraction_docs.get(cls, []))
        jbh = cutoff_evidence(extraction.get(cls, []), target, risk=risk, target_step=target_step, **ext_kw, **kw)
        jbh["current"] = effective_value(base_policy, cls, "judge_band_high")
        entry["judge_band_high"] = jbh

        low = cutoff_evidence(
            extraction.get(cls, []), low_target, risk=None, min_accepted=min_low_n,
            target_step=target_step, max_target=target, **ext_kw, **kw
        )
        low["current"] = effective_value(base_policy, cls, "low")
        entry["low"] = low
        _reconcile_bands(entry)

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


_UNIT_SHORT = {"documents": "doc", "attempts": "att"}


def print_table(result: Mapping[str, Any], out=None) -> None:
    out = out or sys.stdout
    p = result["params"]
    print(
        f"target accuracy = 1 - C_h/C_e = {p['target']:.3f} | risk {p['risk']} | "
        f"folds {p['folds']} seed {p['seed']} | field_floor {p['field_floor']}",
        file=out,
    )
    header = (
        f"{'class':<18}{'cutoff':<16}{'unit':>6}{'n':>6}{'docs':>6}{'acc':>7}{'ece':>7}{'auroc':>7}"
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
                f"{cls:<18}{key:<16}{_UNIT_SHORT.get(e['unit'], e['unit']):>6}{e['n']:>6}{e['n_docs']:>6}{_fmt(e['accuracy']):>7}{_fmt(e['ece']):>7}"
                f"{_fmt(e['auroc']):>7}{_fmt(e['cutoff'], '.4f'):>8}{_fmt(e.get('effective_target'), '.4f'):>7}{_fmt(cf.get('holdout_accuracy')):>9}"
                f"{_fmt(ci):>7}{e['n_accepted']:>7}{e['errors']:>5}{_fmt(e['required'], 'd'):>6}"
                f"{_fmt(e['shortfall'], 'd'):>6}{_fmt(e['current'], '.3f'):>9}{_fmt(e['proposed'], '.3f'):>9}  {status}",
                file=out,
            )
    print(
        "unit: doc = one pair per document; att = one pair per extraction ATTEMPT (every scored attempt of the gold class is "
        "pooled, so n / accept / err for judge_band_high and low count attempts; docs = distinct documents behind "
        "n; need / short are in documents)",
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
                    "unit": e["unit"],
                    "n": e["n"],
                    "n_docs": e["n_docs"],
                    "n_accepted": e["n_accepted"],
                    "n_accepted_docs": e["n_accepted_docs"],
                    "errors_docs": e["errors_docs"],
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
