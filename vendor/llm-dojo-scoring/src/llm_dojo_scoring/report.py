"""Markdown report builder — the full interpretation write-up.

Assembles the analysis (error_analysis + interpret) into a self-contained
Markdown report with tables and optional plot references. Used by the CLI
(``dojo-analyze``) and importable directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from . import error_analysis as ea
from . import interpret as interp
from .config import CONTRACT_SUBTYPE_LABELS


def _pct(value, digits: int = 2) -> str:
    """Format a fractional value as a percentage, using n/a for missing values."""
    if value is None or value != value:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _md_table(frame: pd.DataFrame, max_rows: int = 40) -> str:
    """Render a bounded Markdown table with compact floats and a truncation note."""
    if frame is None or frame.empty:
        return "_no data_"
    head = frame.head(max_rows)
    cols = [str(c) for c in head.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in head.iterrows():
        cells = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, float):
                cells.append(f"{v:.4f}".rstrip("0").rstrip("."))
            elif v is None or v != v:
                cells.append("")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    if len(head) < len(frame):
        lines.append(f"_... and {len(frame) - len(head)} more rows_")
    return "\n".join(lines)


def build_report(frame: pd.DataFrame, *, path: Optional[str] = None,
                 metric: str = ea.DEFAULT_METRIC,
                 target: Optional[float] = None,
                 min_n: int = 0,
                 cost_column: Optional[str] = None,
                 plot_paths: Optional[dict[str, str]] = None,
                 title: Optional[str] = None) -> str:
    """Build (and optionally write) the full Markdown report for a frame."""
    df = frame.copy()
    interpretation = interp.interpret(df, metric=metric, target=target,
                                      min_n=min_n, cost_column=cost_column)
    summary = interpretation.summary
    rel = ea.reliability_assessment(df, metric)

    lines: list[str] = []
    lines.append(f"# {title or 'Evaluation Report'}")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
                 f"by llm-dojo-scoring · {len(df)} runs · metric `{metric}`_")
    lines.append("")

    # -- verdicts ------------------------------------------------------------
    lines.append("## Verdicts")
    lines.append("")
    lines.append(interp.render_notes(interpretation))
    lines.append("")

    # -- champion table -------------------------------------------------------
    if summary.get("winner"):
        lines.append("## Champion")
        lines.append("")
        winner = summary["winner"]
        lines.append(_md_table(pd.DataFrame([{
            "Experiment": winner,
            metric: summary["winner_score"],
            "n docs": summary["winner_n"],
            "equiv": summary.get("winner_equiv_score"),
        }])))
        lines.append("")

    # -- headline table --------------------------------------------------------
    lines.append("## Runs")
    lines.append("")
    run_cols = []
    for c in ("DATE", "Experiment Name", "SAMPLE (n)", "MODEL", "Prompt Version",
              "Temperature", metric, ea.EQUIV_METRIC, "Average Confidence",
              ea.CI_LO, ea.CI_HI):
        if c in df.columns:
            run_cols.append(c)
    if run_cols:
        lines.append(_md_table(df[run_cols]))
        lines.append("")

    # -- prompt version ---------------------------------------------------------
    if "prompt_version" in df.columns:
        lines.append("## Prompt versions")
        lines.append("")
        lines.append(_md_table(ea.prompt_version_summary(df, metric)))
        lines.append("")

    # -- models ------------------------------------------------------------------
    if "model" in df.columns:
        lines.append("## Models")
        lines.append("")
        lines.append(_md_table(ea.model_summary(df, metric)))
        lines.append("")

    # -- per-subtype --------------------------------------------------------------
    sub = ea.per_subtype_summary(df)
    if not sub.empty:
        lines.append("## Per-subtype accuracy")
        lines.append("")
        lines.append("| subtype | label | mean | best | worst | spread |")
        lines.append("|---|---|---|---|---|---|")
        for subtype, row in sub.iterrows():
            label = CONTRACT_SUBTYPE_LABELS.get(subtype, "")
            lines.append(
                f"| {subtype} | {label} | {_pct(row['mean'])} | {_pct(row['best'])} "
                f"| {_pct(row['worst'])} | {_pct(row['spread'])} |")
        lines.append("")

    # -- failure modes -------------------------------------------------------------
    modes = ea.failure_mode_summary(df)
    if not modes.empty and modes["n_total"].sum() > 0:
        lines.append("## Failure modes")
        lines.append("")
        lines.append("| mode | label | n | % of failures | % of docs | runs reporting |")
        lines.append("|---|---|---|---|---|---|")
        for mode, row in modes.iterrows():
            lines.append(
                f"| {mode} | {row['label']} | {int(row['n_total'])} | "
                f"{_pct(row['pct_of_failures'])} | {_pct(row['pct_of_docs'])} | {int(row['n_runs'])} |")
        lines.append("")

    # -- reliability -----------------------------------------------------------------
    lines.append("## Reliability")
    lines.append("")
    lines.append(_md_table(pd.DataFrame([{
        "runs": rel["n_runs"],
        "median n": rel["median_n"],
        "median CI half": rel["median_ci_half"],
        "max CI half": rel["max_ci_half"],
        "runs with CI": f"{rel['runs_with_ci']} ({_pct(rel['runs_with_ci_pct'] or 0, 0)})",
    }])))
    lines.append("")

    # -- regressions --------------------------------------------------------------------
    alerts = ea.regression_alerts(df, metric)
    if not alerts.empty:
        lines.append("## Regressions (same prompt, previous run)")
        lines.append("")
        lines.append(_md_table(alerts))
        lines.append("")

    # -- plots ---------------------------------------------------------------------------
    if plot_paths:
        lines.append("## Plots")
        lines.append("")
        for key in ("metric_ci", "prompt_version", "model", "per_subtype",
                    "failure_modes", "confidence", "cost"):
            if key in plot_paths:
                lines.append(f"![{key}]({plot_paths[key]})")
                lines.append("")

    report = "\n".join(lines)
    if path:
        from pathlib import Path

        Path(path).write_text(report, encoding="utf-8")
    return report


__all__ = ["build_report"]
