"""Error analysis over run-level results frames.

Works on the normalized frame from :func:`llm_dojo_scoring.io.normalize_results_frame`
(e.g. the Sorter_Experiment_Results.xlsx artifacts): aggregates, trend
detection, per-subtype hotspots, failure-mode drivers, and reliability
assessments. Everything is pure pandas/numpy — no plotting here (see
:mod:`llm_dojo_scoring.visualize`).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .bootstrap import wilson_ci
from .config import PER_SUBTYPE, SORTER_FAILURE_MODES, get_settings

DEFAULT_METRIC = "Subtype Accuracy"
CI_LO = "Subtype Accuracy CI (lo)"
CI_HI = "Subtype Accuracy CI (hi)"
EQUIV_METRIC = "Subtype Accuracy (equiv)"

FAILURE_COLUMNS = [
    "Failures: equivalent_family",
    "Failures: family_confusion",
    "Failures: function_over_form",
    "Failures: other_fallback",
]


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
    """Convert a frame column to numeric values, coercing invalid entries to NaN."""
    return pd.to_numeric(frame[column], errors="coerce")


def require_metric(frame: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Validate a metric column and return a copy containing only numeric metric rows."""
    if metric not in frame.columns:
        raise ValueError(
            f"metric '{metric}' not in frame columns: {list(frame.columns)[:20]}..."
        )
    out = frame.copy()
    out[metric] = _num(out, metric)
    return out.dropna(subset=[metric])


def best_run(frame: pd.DataFrame, metric: str = DEFAULT_METRIC,
             min_n: int = 0) -> Optional[pd.Series]:
    """The run with the highest metric value (ties: larger n first), restricted
    to runs with ``SAMPLE (n) >= min_n``."""
    df = require_metric(frame, metric)
    if "n" in df.columns:
        df = df[df["n"].fillna(0) >= min_n]
    if df.empty:
        return None
    df = df.sort_values([metric, "n"], ascending=False)
    return df.iloc[0]


def runner_up(frame: pd.DataFrame, metric: str = DEFAULT_METRIC,
              min_n: int = 0) -> Optional[pd.Series]:
    """The second-best run (same filters as :func:`best_run`)."""
    df = require_metric(frame, metric)
    if "n" in df.columns:
        df = df[df["n"].fillna(0) >= min_n]
    df = df.sort_values([metric, "n"], ascending=False)
    if len(df) < 2:
        return None
    return df.iloc[1]


def per_group_summary(frame: pd.DataFrame, group: str,
                      metric: str = DEFAULT_METRIC) -> pd.DataFrame:
    """Aggregate the metric by a grouping column (e.g. ``prompt_version`` or
    ``model``): n runs, mean, best, worst, spread, and the winner's key.

    Only runs with ``n >= 1`` count (blank rows are dropped by require_metric).
    """
    df = require_metric(frame, metric)
    if group not in df.columns:
        raise ValueError(f"group '{group}' not in frame columns")
    rows: list[dict] = []
    for value, sub in df.groupby(df[group].fillna("?"), sort=False):
        best_idx = sub[metric].idxmax()
        rows.append({
            group: value,
            "n_runs": int(len(sub)),
            "mean": round(float(sub[metric].mean()), 4),
            "best": round(float(sub[metric].max()), 4),
            "worst": round(float(sub[metric].min()), 4),
            "spread": round(float(sub[metric].max() - sub[metric].min()), 4),
            "best_run": str(sub.loc[best_idx].get("Experiment Name", "")),
            "best_n": _num(sub.loc[best_idx:best_idx], "n").iloc[0] if "n" in sub.columns else None,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("mean", ascending=False).reset_index(drop=True)
    return out


def metric_trend(frame: pd.DataFrame, metric: str = DEFAULT_METRIC) -> pd.DataFrame:
    """Chronological view of the metric with per-run CI (when available) —
    the input for the trend plot. Columns: experiment, date, metric, lo, hi,
    n, model, prompt_version."""
    df = require_metric(frame, metric)
    out = df.sort_values("date", na_position="last").copy()
    lo = _num(out, CI_LO) if CI_LO in out.columns else pd.Series(np.nan, index=out.index)
    hi = _num(out, CI_HI) if CI_HI in out.columns else pd.Series(np.nan, index=out.index)
    # Fill missing CI from Wilson on (metric, n) when support exists.
    for idx in out.index:
        if pd.isna(lo.get(idx, np.nan)) or pd.isna(hi.get(idx, np.nan)):
            n = out.at[idx, "n"] if "n" in out.columns else None
            if n and not pd.isna(n) and int(n) >= 2:
                ci = wilson_ci(float(out.at[idx, metric]), int(n))
                if ci:
                    lo.at[idx] = ci["lo"]
                    hi.at[idx] = ci["hi"]
    out["lo"] = lo
    out["hi"] = hi
    return out


def prompt_version_summary(frame: pd.DataFrame,
                           metric: str = DEFAULT_METRIC) -> pd.DataFrame:
    """Per-prompt-version aggregation with best-run CI (the comparison the
    prompt sweep is built for)."""
    return per_group_summary(frame, "prompt_version", metric)


def model_summary(frame: pd.DataFrame, metric: str = DEFAULT_METRIC) -> pd.DataFrame:
    """Per-model aggregation (mean of the metric across that model's runs)."""
    return per_group_summary(frame, "model", metric)


def failure_mode_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Totals and rates for the four sorter failure modes across runs.

    Returns a DataFrame indexed by mode with columns: n_total (summed across
    runs), pct_of_failures, pct_of_docs, n_runs (runs reporting the mode).
    """
    present = [c for c in FAILURE_COLUMNS if c in frame.columns]
    if not present:
        return pd.DataFrame()
    total_fail = 0
    total_docs = 0
    rows: dict[str, dict] = {}
    for col in present:
        mode = col.replace("Failures: ", "")
        values = _num(frame, col).dropna()
        total = int(values.sum())
        total_fail += total
        rows[mode] = {"n_total": total, "n_runs": int((values > 0).sum())}
    if "Failures: n Failed" in frame.columns:
        total_fail = int(_num(frame, "Failures: n Failed").sum())
    if "n" in frame.columns:
        total_docs = int(_num(frame, "n").sum())
    out = pd.DataFrame(rows).T
    out["pct_of_failures"] = out["n_total"] / total_fail if total_fail else 0.0
    out["pct_of_docs"] = out["n_total"] / total_docs if total_docs else 0.0
    order = [m for m in ["equivalent_family", "family_confusion",
                         "function_over_form", "other_fallback"] if m in out.index]
    out = out.loc[order]
    out["label"] = [SORTER_FAILURE_MODES[m]["label"] for m in out.index]
    return out


def per_subtype_summary(frame: pd.DataFrame, best_only: bool = False) -> pd.DataFrame:
    """Per-subtype accuracy across runs.

    Reads the ``Accuracy: {subtype}`` columns. Returns a DataFrame indexed by
    subtype with columns: mean accuracy across runs, best, worst, spread, and
    the best-run experiment name. ``best_only=True`` restricts to the frame's
    single best run (by Subtype Accuracy).
    """
    df = frame
    if best_only:
        best = best_run(frame)
        if best is None:
            return pd.DataFrame()
        df = frame.loc[[best.name]]
    cols = {f"Accuracy: {s}": s for s in PER_SUBTYPE if f"Accuracy: {s}" in frame.columns}
    if not cols:
        return pd.DataFrame()
    rows: dict[str, dict] = {}
    for col, subtype in cols.items():
        values = _num(df, col).dropna()
        if values.empty:
            continue
        rows[subtype] = {
            "mean": round(float(values.mean()), 4),
            "best": round(float(values.max()), 4),
            "worst": round(float(values.min()), 4),
            "spread": round(float(values.max() - values.min()), 4),
            "best_run": str(frame.loc[values.idxmax()].get("Experiment Name", "")),
        }
    out = pd.DataFrame(rows).T
    out.index.name = "subtype"
    return out


def subtype_hotspots(frame: pd.DataFrame, k: int = 5) -> pd.DataFrame:
    """The k subtypes with the LOWEST mean accuracy across runs (the classes
    that drive failures)."""
    summary = per_subtype_summary(frame)
    if summary.empty:
        return summary
    return summary.sort_values("mean").head(k)


def regression_alerts(frame: pd.DataFrame, metric: str = DEFAULT_METRIC,
                      threshold: float = 0.02) -> pd.DataFrame:
    """Runs whose metric dropped >= ``threshold`` vs the previous chrono run
    with the SAME prompt version (a rerun/regression signal)."""
    df = require_metric(frame, metric)
    if df.empty or "prompt_version" not in df.columns:
        return pd.DataFrame(columns=["Experiment Name", "date", metric, "previous", "delta"])
    df = df.sort_values("date").copy()
    alerts: list[dict] = []
    for version, sub in df.groupby("prompt_version", sort=False):
        sub = sub.sort_values("date")
        for i in range(1, len(sub)):
            prev = float(sub.iloc[i - 1][metric])
            cur = float(sub.iloc[i][metric])
            if prev - cur >= threshold:
                alerts.append({
                    "Experiment Name": str(sub.iloc[i].get("Experiment Name", "")),
                    "date": sub.iloc[i].get("date"),
                    "prompt_version": version,
                    metric: cur,
                    "previous": round(prev, 4),
                    "delta": round(prev - cur, 4),
                })
    return pd.DataFrame(alerts)


def reliability_assessment(frame: pd.DataFrame, metric: str = DEFAULT_METRIC) -> dict:
    """How trustworthy are these numbers? Summarizes CI half-widths and
    sample sizes across runs."""
    df = require_metric(frame, metric)
    half = _num(df, "Subtype Accuracy CI (half)").dropna() \
        if "Subtype Accuracy CI (half)" in df.columns else pd.Series(dtype=float)
    n = pd.to_numeric(df.get("n"), errors="coerce").dropna()
    return {
        "n_runs": int(len(df)),
        "median_n": float(n.median()) if len(n) else None,
        "min_n": float(n.min()) if len(n) else None,
        "median_ci_half": round(float(half.median()), 4) if len(half) else None,
        "max_ci_half": round(float(half.max()), 4) if len(half) else None,
        "runs_with_ci": int(len(half)),
        "runs_with_ci_pct": round(len(half) / len(df), 4) if len(df) else None,
    }


def confusion_drivers(frame: pd.DataFrame, k: int = 5) -> pd.DataFrame:
    """Top off-diagonal cells from any per-run confusion insight: uses the
    failure-mode columns as the best proxy available in workbook frames.
    For JSONL records with a real confusion matrix, use
    :func:`failure_modes.confusion_from_rows` on the per-doc results."""
    modes = failure_mode_summary(frame)
    if modes.empty:
        return modes
    return modes.sort_values("n_total", ascending=False).head(k)


__all__ = [
    "DEFAULT_METRIC", "best_run", "runner_up", "per_group_summary",
    "metric_trend", "prompt_version_summary", "model_summary",
    "failure_mode_summary", "per_subtype_summary", "subtype_hotspots",
    "regression_alerts", "reliability_assessment", "confusion_drivers",
]
