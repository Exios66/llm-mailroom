"""Interpretation: verdicts, champions, and plain-language readouts.

Turns an analyzed results frame into structured, decision-ready notes: which
run wins, whether the win is significant, which prompt version is best, where
failures concentrate, and how much to trust the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import error_analysis as ea
from .bootstrap import delta_significance

INTERP_EQUIV = ea.EQUIV_METRIC


@dataclass
class InterpretationNote:
    """One categorized interpretation finding with severity, headline, and detail."""
    kind: str            # winner | significance | version | model | hotspot | failure | reliability | cost | regression
    severity: str        # info | warning | critical
    headline: str
    detail: str = ""

    def to_dict(self) -> dict:
        """Serialize the finding as plain text fields."""
        return {"kind": self.kind, "severity": self.severity,
                "headline": self.headline, "detail": self.detail}


@dataclass
class Interpretation:
    """Evaluation findings together with their run-level summary."""
    notes: list[InterpretationNote] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def by_severity(self) -> dict[str, list[InterpretationNote]]:
        """Group notes into info, warning, and critical buckets in their original order."""
        out = {"info": [], "warning": [], "critical": []}
        for note in self.notes:
            out[note.severity].append(note)
        return out

    def to_dict(self) -> dict:
        """Serialize the summary and each interpretation note."""
        return {
            "summary": self.summary,
            "notes": [n.to_dict() for n in self.notes],
        }


def _pct(value, digits: int = 2) -> str:
    """Format a fraction as a percentage, using n/a for missing or NaN values."""
    if value is None or value != value:  # NaN guard
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def interpret(frame, metric: str = ea.DEFAULT_METRIC,
              target: Optional[float] = None,
              min_n: int = 0,
              cost_column: Optional[str] = None) -> Interpretation:
    """Build the full interpretation for a results frame.

    Args:
        frame: normalized results frame (see io.normalize_results_frame).
        metric: the primary score column (default "Subtype Accuracy").
        target: an accuracy goal — runs meeting it are flagged (default None).
        min_n: minimum sample size for the champion.
        cost_column: optional cost column for an efficiency note.

    Returns an Interpretation with ordered notes and a structured summary.
    """
    interp = Interpretation()
    if frame is None or getattr(frame, "empty", True):
        interp.notes.append(InterpretationNote(
            "winner", "warning",
            "No qualifying runs found for the given filters.",
            detail=f"metric={metric}, min_n={min_n}",
        ))
        return interp
    try:
        df = ea.require_metric(frame, metric)
    except ValueError:
        interp.notes.append(InterpretationNote(
            "winner", "warning",
            f"Metric '{metric}' not present in the results frame.",
        ))
        return interp
    best = ea.best_run(df, metric, min_n=min_n)
    if best is None:
        interp.notes.append(InterpretationNote(
            "winner", "warning",
            "No qualifying runs found for the given filters.",
            detail=f"metric={metric}, min_n={min_n}",
        ))
        return interp

    best_name = str(best.get("Experiment Name", "?"))
    best_val = float(best[metric])
    best_n = int(best["n"]) if "n" in best.index and best["n"] == best["n"] else None
    equiv = float(best[ea.EQUIV_METRIC]) if ea.EQUIV_METRIC in best.index and \
        best[ea.EQUIV_METRIC] == best[ea.EQUIV_METRIC] else None

    summary: dict = {
        "metric": metric,
        "winner": best_name,
        "winner_score": best_val,
        "winner_n": best_n,
        "winner_equiv_score": equiv,
        "n_runs": int(len(df)),
    }
    interp.summary = summary

    interp.notes.append(InterpretationNote(
        "winner", "info",
        f"Best run: {best_name} at {_pct(best_val)} "
        f"({best_n} docs)" + (f", {_pct(equiv)} equivalence-adjusted" if equiv is not None else "") + ".",
    ))
    if best_n is not None and best_n < 50:
        interp.notes.append(InterpretationNote(
            "winner", "warning",
            f"Champion rests on a small sample ({best_n} docs) — treat the "
            f"score as provisional and re-run at full scale before promoting.",
        ))

    # -- significance vs runner-up -------------------------------------------
    runner = ea.runner_up(df, metric, min_n=min_n)
    if runner is not None:
        runner_val = float(runner[metric])
        delta = best_val - runner_val
        ci = None
        # Wilson-based rough significance when only aggregates exist.
        if best_n and runner.get("n") == runner["n"]:
            from .bootstrap import wilson_ci

            bci = wilson_ci(best_val, best_n)
            rci = wilson_ci(float(runner[metric]), int(runner["n"]))
            if bci and rci:
                # The delta is significant when the two intervals don't overlap
                # at the midpoint-adjusted level; approximate: disjoint means
                # the gap is outside both half-widths.
                gap = bci["lo"] - rci["hi"]
                ci = {"delta": round(delta, 4), "ci_lo": round(rci["hi"], 4),
                      "ci_hi": round(bci["lo"], 4), "significant": gap > 0}
        if ci and ci["significant"]:
            interp.notes.append(InterpretationNote(
                "significance", "info",
                f"The win over the runner-up ({str(runner.get('Experiment Name', '?'))}, "
                f"{_pct(runner_val)}) is significant: the CIs do not overlap "
                f"(gap {_pct(ci['delta'])}).",
            ))
        else:
            interp.notes.append(InterpretationNote(
                "significance", "warning",
                f"The win over the runner-up ({_pct(runner_val)}) is NOT statistically "
                f"significant at the aggregate level (delta {_pct(delta)} within "
                f"the confidence overlap).",
            ))

    # -- target gate ----------------------------------------------------------
    if target is not None:
        if best_val >= target:
            interp.notes.append(InterpretationNote(
                "target", "info",
                f"Target met: {_pct(best_val)} >= {_pct(target)}.",
            ))
        else:
            interp.notes.append(InterpretationNote(
                "target", "warning",
                f"Target not met: best is {_pct(best_val)} vs {_pct(target)} "
                f"(shortfall {_pct(target - best_val)}).",
            ))

    # -- prompt-version comparison --------------------------------------------
    if "prompt_version" in df.columns:
        version_sum = ea.prompt_version_summary(df, metric)
        # ignore unparseable placeholder groups ("?") and single-run pilots
        real_versions = version_sum[
            (version_sum["prompt_version"] != "?")
            & (version_sum["n_runs"] >= 2)
        ]
        if real_versions.empty:
            real_versions = version_sum[version_sum["prompt_version"] != "?"]
        if len(real_versions) > 1:
            top = real_versions.iloc[0]
            second = real_versions.iloc[1]
            interp.notes.append(InterpretationNote(
                "version", "info",
                f"Best prompt version: {top['prompt_version']} "
                f"(mean {_pct(top['mean'])}, {top['n_runs']} runs) — "
                f"{second['prompt_version']} trails at {_pct(second['mean'])}.",
            ))
        elif len(real_versions) == 1:
            top = real_versions.iloc[0]
            interp.notes.append(InterpretationNote(
                "version", "info",
                f"Single prompt version in the frame: {top['prompt_version']} "
                f"(mean {_pct(top['mean'])}, {top['n_runs']} runs).",
            ))

    # -- model comparison ------------------------------------------------------
    if "model" in df.columns:
        model_sum = ea.model_summary(df, metric)
        if len(model_sum) > 1:
            best_model = model_sum.iloc[0]
            interp.notes.append(InterpretationNote(
                "model", "info",
                f"Model leaderboard: {best_model['model']} "
                f"(mean {_pct(best_model['mean'])}, {best_model['n_runs']} runs).",
            ))

    # -- per-subtype hotspots ---------------------------------------------------
    hotspots = ea.subtype_hotspots(df, k=5)
    if not hotspots.empty:
        worst = hotspots.iloc[0]
        detail = ", ".join(f"{name} {_pct(row['mean'])}"
                           for name, row in hotspots.iterrows())
        interp.notes.append(InterpretationNote(
            "hotspot", "warning",
            f"Weakest subtype: {worst.name} (mean {_pct(worst['mean'])} across "
            f"runs). Check its confusion pattern before prompt changes.",
            detail=detail,
        ))

    # -- failure-mode drivers ---------------------------------------------------
    modes = ea.failure_mode_summary(df)
    if not modes.empty and modes["n_total"].sum() > 0:
        top_mode = modes.sort_values("n_total", ascending=False).iloc[0]
        if top_mode.name == "family_confusion":
            interp.notes.append(InterpretationNote(
                "failure", "warning",
                f"Dominant failure driver: {top_mode['label']} "
                f"({top_mode['n_total']} of {modes['n_total'].sum()} failures, "
                f"{_pct(top_mode['pct_of_failures'])}).",
                detail="Family-level confusion suggests the subtype taxonomy or "
                       "the distinguishing signal in the prompt needs attention.",
            ))
        elif top_mode.name == "function_over_form":
            interp.notes.append(InterpretationNote(
                "failure", "warning",
                f"Dominant failure driver: {top_mode['label']} "
                f"({top_mode['n_total']} failures) — documents whose function "
                f"overrode their contract form.",
            ))
        else:
            interp.notes.append(InterpretationNote(
                "failure", "info",
                f"Largest failure mode: {top_mode['label']} "
                f"({top_mode['n_total']} failures).",
            ))

    # -- reliability -------------------------------------------------------------
    reliability = ea.reliability_assessment(df, metric)
    if reliability["median_ci_half"] is not None:
        if reliability["median_ci_half"] > 0.03:
            interp.notes.append(InterpretationNote(
                "reliability", "warning",
                f"Results are noisy: median CI half-width "
                f"{_pct(reliability['median_ci_half'])} — prefer same-seed, "
                f"larger-sample runs for verdicts.",
            ))
        else:
            interp.notes.append(InterpretationNote(
                "reliability", "info",
                f"Results are reasonably tight: median CI half-width "
                f"{_pct(reliability['median_ci_half'])} across "
                f"{reliability['n_runs']} runs.",
            ))
    if reliability["runs_with_ci_pct"] is not None and reliability["runs_with_ci_pct"] < 1.0:
        interp.notes.append(InterpretationNote(
            "reliability", "warning",
            f"Only {_pct(reliability['runs_with_ci_pct'], 0)} of runs carry a "
            f"reported CI; Wilson intervals were synthesized from n where "
            f"possible.",
        ))

    # -- cost efficiency ---------------------------------------------------------
    if cost_column and cost_column in df.columns:
        cost = df[cost_column]
        cost = pd.to_numeric(cost, errors="coerce").dropna()
        if len(cost):
            best_cost = float(best.get(cost_column) or float("nan"))
            interp.notes.append(InterpretationNote(
                "cost", "info",
                f"Winner cost: ${best_cost:,.4f} (range ${cost.min():,.4f}–"
                f"${cost.max():,.4f} across runs).",
            ))

    # -- regressions --------------------------------------------------------------
    alerts = ea.regression_alerts(df, metric)
    if not alerts.empty:
        top_alert = alerts.iloc[0]
        interp.notes.append(InterpretationNote(
            "regression", "critical",
            f"Regression detected: {top_alert['Experiment Name']} dropped "
            f"{_pct(top_alert['delta'])} vs its previous run of "
            f"prompt {top_alert['prompt_version']}.",
            detail=f"{len(alerts)} regression(s) >= 2 points found. "
                   "Investigate before promoting that configuration.",
        ))

    return interp


def render_notes(interp: Interpretation) -> str:
    """Plain-text rendering of an interpretation (CLI / report use)."""
    lines = []
    for note in interp.notes:
        marker = {"info": "[i]", "warning": "[!]", "critical": "[!!]"}[note.severity]
        lines.append(f"{marker} {note.headline}")
        if note.detail:
            lines.append(f"    {note.detail}")
    return "\n".join(lines)


__all__ = ["Interpretation", "InterpretationNote", "interpret", "render_notes"]
