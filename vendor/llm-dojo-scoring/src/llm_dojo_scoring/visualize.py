"""Visualization for evaluation results (matplotlib).

All functions take a normalized results frame (see
:mod:`llm_dojo_scoring.io.normalize_results_frame`) and return a
``matplotlib.figure.Figure``; the CLI saves them to PNG. Backend is left to
the caller (CLI and tests force Agg for headless use).
"""

from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import error_analysis as ea
from .config import PER_SUBTYPE

_BEST_COLOR = "#1F4E79"
_OTHER_COLOR = "#5B9BD5"
_FAIL_COLOR = "#C00000"


def _sorted_metric(frame: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Select rows with a usable metric and sort them from highest to lowest."""
    return ea.require_metric(frame, metric).sort_values(metric, ascending=False)


def plot_metric_ci(frame: pd.DataFrame, metric: str = ea.DEFAULT_METRIC,
                   figsize=(11, 6), title: str | None = None) -> plt.Figure:
    """Horizontal bar chart of the metric per run with CI error bars (the
    headline plot)."""
    df = _sorted_metric(frame, metric)
    lo_col = df.get("Subtype Accuracy CI (lo)")
    hi_col = df.get("Subtype Accuracy CI (hi)")
    lo = pd.to_numeric(lo_col, errors="coerce") if lo_col is not None else pd.Series(dtype=float)
    hi = pd.to_numeric(hi_col, errors="coerce") if hi_col is not None else pd.Series(dtype=float)
    xerr = None
    if lo.notna().any() and hi.notna().any():
        lo = lo.fillna(df[metric])
        hi = hi.fillna(df[metric])
        xerr = np.vstack([df[metric] - lo, hi - df[metric]])
    labels = [
        str(r.get("Experiment Name", f"run {i}"))
        for i, (_, r) in enumerate(df.iterrows())
    ]
    fig, ax = plt.subplots(figsize=figsize)
    y = np.arange(len(df))
    colors = [df.index[i] == df.index[0] for i in range(len(df))]
    ax.barh(y, df[metric], height=0.6, color=[_BEST_COLOR if c else _OTHER_COLOR for c in colors],
            xerr=xerr, error_kw={"elinewidth": 1.0, "capsize": 3, "color": "#333333"})
    for i, (_, row) in enumerate(df.iterrows()):
        ax.text(float(row[metric]) + 0.005, i, f"{float(row[metric]) * 100:.1f}%",
                va="center", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlim(0, 1.05)
    ax.set_xlabel(metric)
    ax.set_title(title or f"{metric} by run (CI where reported)")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return fig


def plot_prompt_version(frame: pd.DataFrame, metric: str = ea.DEFAULT_METRIC,
                        figsize=(10, 5), title: str | None = None) -> plt.Figure:
    """Bar chart of mean metric per prompt version with best/worst whiskers."""
    summary = ea.prompt_version_summary(frame, metric)
    if summary.empty:
        raise ValueError("no prompt_version data to plot")
    fig, ax = plt.subplots(figsize=figsize)
    versions = [str(v) for v in summary["prompt_version"]]
    x = np.arange(len(versions))
    ax.bar(x, summary["mean"], color=_OTHER_COLOR, width=0.6)
    for i, (_, row) in enumerate(summary.iterrows()):
        ax.plot([i, i], [row["worst"], row["best"]], color="#333333", lw=1)
        ax.text(i, float(row["mean"]) + 0.01, f"{float(row['mean']) * 100:.1f}%",
                ha="center", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(versions, rotation=0)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel(metric)
    ax.set_title(title or f"{metric} by prompt version (mean + range)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return fig


def plot_model_comparison(frame: pd.DataFrame, metric: str = ea.DEFAULT_METRIC,
                          figsize=(9, 5), title: str | None = None) -> plt.Figure:
    """Bar chart of mean metric per model."""
    summary = ea.model_summary(frame, metric)
    if summary.empty:
        raise ValueError("no model data to plot")
    fig, ax = plt.subplots(figsize=figsize)
    models = [str(m) for m in summary["model"]]
    x = np.arange(len(models))
    ax.bar(x, summary["mean"], color=_OTHER_COLOR, width=0.6)
    for i, (_, row) in enumerate(summary.iterrows()):
        ax.text(i, float(row["mean"]) + 0.01, f"{float(row['mean']) * 100:.1f}%",
                ha="center", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(models, rotation=15, ha="right")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel(metric)
    ax.set_title(title or f"{metric} by model")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return fig


def plot_per_subtype_heatmap(frame: pd.DataFrame,
                             runs: Optional[int] = None,
                             figsize: Optional[tuple] = None,
                             title: str | None = None) -> plt.Figure:
    """Runs x subtype accuracy heatmap (the per-class hotspot view).

    Rows are the top ``runs`` runs (by Subtype Accuracy), columns the
    canonical PER_SUBTYPE order; cell values are the per-subtype accuracies.
    """
    df = frame.copy()
    if "Subtype Accuracy" in df.columns:
        df = df.sort_values("Subtype Accuracy", ascending=False)
    if runs:
        df = df.head(runs)
    cols = [f"Accuracy: {s}" for s in PER_SUBTYPE if f"Accuracy: {s}" in df.columns]
    if not cols:
        raise ValueError("no per-subtype Accuracy columns in frame")
    matrix = pd.to_numeric(df[cols].stack(), errors="coerce").unstack().values
    if figsize is None:
        figsize = (13, max(4, 0.4 * len(df)))
    fig, ax = plt.subplots(figsize=figsize)
    im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    labels = [str(r.get("Experiment Name", f"run {i}"))[:44]
              for i, (_, r) in enumerate(df.iterrows())]
    ax.set_yticks(np.arange(len(labels)))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xticks(np.arange(len(cols)))
    ax.set_xticklabels([c.replace("Accuracy: ", "") for c in cols],
                       rotation=60, ha="right", fontsize=8)
    ax.set_title(title or "Per-subtype accuracy by run")
    fig.colorbar(im, ax=ax, shrink=0.6, label="accuracy")
    fig.tight_layout()
    return fig


def plot_failure_modes(frame: pd.DataFrame,
                       figsize=(10, 5), title: str | None = None) -> plt.Figure:
    """Stacked horizontal bars of the four failure modes per run."""
    modes = ea.failure_mode_summary(frame)
    if modes.empty:
        raise ValueError("no failure-mode columns in frame")
    cols = [f"Failures: {m}" for m in modes.index if f"Failures: {m}" in frame.columns]
    if not cols:
        raise ValueError("no failure-mode columns in frame")
    df = frame.copy()
    df = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0)
    df = df.sort_values(list(cols), ascending=False)
    fig, ax = plt.subplots(figsize=figsize)
    y = np.arange(len(df))
    left = np.zeros(len(df))
    colors = ["#2E7D32", "#C00000", "#ED7D31", "#BFBFBF"]
    for col, color in zip(cols, colors):
        mode = col.replace("Failures: ", "")
        ax.barh(y, df[col], left=left, color=color, label=mode)
        left += df[col]
    labels = [str(r.get("Experiment Name", f"run {i}"))[:44]
              for i, (_, r) in enumerate(df.iterrows())]
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("failed documents")
    ax.set_title(title or "Failure modes by run")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return fig


def plot_confidence_scatter(frame: pd.DataFrame,
                            metric: str = ea.DEFAULT_METRIC,
                            figsize=(8, 6), title: str | None = None) -> plt.Figure:
    """Confidence vs accuracy scatter — the calibration/overconfidence view."""
    df = ea.require_metric(frame, metric)
    if "Average Confidence" not in df.columns:
        raise ValueError("no Average Confidence column in frame")
    conf = pd.to_numeric(df["Average Confidence"], errors="coerce")
    fig, ax = plt.subplots(figsize=figsize)
    ax.scatter(conf, df[metric], s=40, color=_OTHER_COLOR, alpha=0.85)
    ax.plot([0, 1], [0, 1], ls="--", color="#999999", lw=1,
            label="perfect calibration")
    for i, (_, row) in enumerate(df.iterrows()):
        if conf.iloc[i] == conf.iloc[i]:
            label = str(row.get("Experiment Name", ""))[:32]
            ax.annotate(label, (float(conf.iloc[i]), float(row[metric])),
                        fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Average Confidence")
    ax.set_ylabel(metric)
    ax.set_title(title or f"Confidence vs {metric}")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def plot_cost_efficiency(frame: pd.DataFrame, metric: str = ea.DEFAULT_METRIC,
                         cost_column: str = "Cost Estimated USD",
                         figsize=(8, 6), title: str | None = None) -> plt.Figure:
    """Accuracy vs cost scatter — the value-for-money view (log x-axis)."""
    df = ea.require_metric(frame, metric)
    if cost_column not in df.columns:
        raise ValueError(f"cost column '{cost_column}' not in frame")
    cost = pd.to_numeric(df[cost_column], errors="coerce")
    valid = cost.notna() & (cost > 0)
    if not valid.any():
        raise ValueError("no positive cost values to plot")
    df, cost = df[valid], cost[valid]
    fig, ax = plt.subplots(figsize=figsize)
    ax.scatter(cost, df[metric], s=40, color=_OTHER_COLOR, alpha=0.85)
    for i, (_, row) in enumerate(df.iterrows()):
        label = str(row.get("Experiment Name", ""))[:32]
        ax.annotate(label, (float(cost.iloc[i]), float(row[metric])),
                    fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.set_xscale("log")
    ax.set_xlabel(f"Cost USD ({cost_column})")
    ax.set_ylabel(metric)
    ax.set_title(title or f"Cost efficiency ({metric} vs cost)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


PLOT_FACTORY = {
    "metric_ci": plot_metric_ci,
    "prompt_version": plot_prompt_version,
    "model": plot_model_comparison,
    "per_subtype": plot_per_subtype_heatmap,
    "failure_modes": plot_failure_modes,
    "confidence": plot_confidence_scatter,
    "cost": plot_cost_efficiency,
}


def build_all_plots(frame: pd.DataFrame, metric: str = ea.DEFAULT_METRIC,
                    cost_column: Optional[str] = None) -> dict[str, plt.Figure]:
    """Build every plot that the frame's columns support.

    Returns ``{plot_key: Figure}``. Skips (with a warning) plots whose
    required columns are missing.
    """
    import warnings

    plots: dict[str, plt.Figure] = {}
    for key, factory in PLOT_FACTORY.items():
        try:
            if key == "cost":
                if not cost_column:
                    continue
                plots[key] = factory(frame, metric, cost_column=cost_column)
            elif key in ("per_subtype", "failure_modes"):
                # these factories do not take the metric positionally
                plots[key] = factory(frame)
            else:
                plots[key] = factory(frame, metric)
        except (ValueError, KeyError, TypeError) as exc:
            warnings.warn(f"skipping {key} plot: {exc}", stacklevel=2)
    return plots


def save_plots(plots: dict[str, plt.Figure], outdir: str,
               prefix: str = "dojo") -> list[str]:
    """Save figures to ``outdir`` as ``{prefix}_{key}.png``; returns paths."""
    import os

    os.makedirs(outdir, exist_ok=True)
    paths = []
    for key, fig in plots.items():
        path = os.path.join(outdir, f"{prefix}_{key}.png")
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths.append(path)
    return paths


__all__ = [
    "plot_metric_ci", "plot_prompt_version", "plot_model_comparison",
    "plot_per_subtype_heatmap", "plot_failure_modes",
    "plot_confidence_scatter", "plot_cost_efficiency",
    "build_all_plots", "save_plots",
]
