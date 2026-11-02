"""Loaders for evaluation artifacts: Excel result workbooks, codebook CSVs,
and normalized analysis frames.

Two consumption paths are supported:

1. **Raw per-run workbooks** (the ``Sorter_Experiment_Results.xlsx`` /
   ``Sorter_Model_Sweep_Results.xlsx`` artifacts, and any workbook written by
   :mod:`llm_dojo_scoring.export`) — one row per experiment run, with the
   documented column schema. Read via :func:`read_workbook`; enriched into a
   typed analysis frame via :func:`normalize_results_frame`.
2. **Experiment-log JSONL** (``reports/experiment_log.jsonl``) — the full
   per-run records, loadable via :func:`load_log` and usable directly with
   ``experiment.dotted_get`` / ``export`` column specs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .experiment import load_records as _load_records

# Columns that carry proportions (0-1) rather than raw counts.
PERCENT_COLUMN_HINTS = (
    "accuracy", "confidence", "presence", "rate", "precision", "recall",
    "f1", "r2", "schema", "verified", "hallucination",
)


@dataclass
class ResultSet:
    """A loaded evaluation artifact (workbook + optional codebook)."""

    path: Path
    frame: pd.DataFrame
    codebook: Optional[pd.DataFrame] = None
    kind: str = "unknown"  # "sorter" | "extraction" | "sweep" | "unknown"
    source: str = "xlsx"   # "xlsx" | "jsonl" | "csv"

    @property
    def n_runs(self) -> int:
        """Return the number of run rows in the results frame."""
        return len(self.frame)

    def metric_columns(self) -> list[str]:
        """Columns that look like proportion metrics (candidate targets)."""
        return [
            c for c in self.frame.columns
            if any(h in c.lower() for h in PERCENT_COLUMN_HINTS)
            and c.lower() not in ("prompt version", "model")
        ]


def _infer_kind(frame: pd.DataFrame, path: Path) -> str:
    """Infer sorter, sweep, extraction, or unknown from result column names."""
    cols = [str(c).lower() for c in frame.columns]
    joined = " ".join(cols)
    if "notes" in joined and "subtype accuracy" in joined:
        return "sweep"  # sweep workbook carries the trailing Notes column
    if "subtype accuracy" in joined and "doc type accuracy" in joined:
        if "failures:" in joined or "n total:" in joined:
            return "sorter"
        return "sweep"
    if "overall extraction" in joined or "entity list f1" in joined:
        return "extraction"
    return "unknown"


def read_workbook(path: str | Path, sheet: str = "Eval Results") -> ResultSet:
    """Read an Eval Results workbook (with optional Codebook sheet)."""
    path = Path(path)
    frame = pd.read_excel(path, sheet_name=sheet)
    codebook = None
    try:
        sheets = pd.ExcelFile(path).sheet_names
        if any(s.lower() == "codebook" for s in sheets):
            codebook = pd.read_excel(path, sheet_name="Codebook")
    except Exception:
        pass
    return ResultSet(
        path=path, frame=frame, codebook=codebook,
        kind=_infer_kind(frame, path), source="xlsx",
    )


def read_codebook(path: str | Path) -> pd.DataFrame:
    """Read a 5-column variable dictionary CSV (Variable, Description, Type,
    Source, Example / Values)."""
    return pd.read_csv(Path(path))


def load_log(path: str | Path, task: str | None = None) -> ResultSet:
    """Load an experiment-log JSONL as a ResultSet.

    ``task`` filters to one task family ("subtype_classification",
    "contract_entity_extraction", ...) when given. The frame is the flat
    table of records; per-record nested values remain reachable through
    ``ResultSet.frame.iloc[i].to_dict()`` + ``experiment.dotted_get``.
    """
    path = Path(path)
    records = _load_records(path)
    if task is not None:
        records = [r for r in records if r.get("task") == task]
    frame = pd.DataFrame.from_records(records) if records else pd.DataFrame()
    return ResultSet(
        path=path, frame=frame, codebook=None,
        kind=_infer_kind(frame, path) if len(frame) else "unknown",
        source="jsonl",
    )


# ---------------------------------------------------------------------------
# Experiment-name parsing
# ---------------------------------------------------------------------------

# {model-slug}_{prompt-version}_subtype[_suffix] e.g.
# qwen3.7-flash_sorter_v12_subtype_langfuse
_NAME_RE = re.compile(
    r"^(?P<model>[a-z0-9.-]+?)_(?:(?P<prompt_kind>sorter|contracts_specialist|legalbench_task)"
    r"_)??(?P<prompt>v?\d+)(?:_(?P<dim>[a-z]+))?(?:_(?P<suffix>.+))?$"
)
# Fallback: pull a prompt-version fragment out of arbitrary run names
# (e.g. "pilot_langfuse_sorter_v5").
_PROMPT_FRAGMENT_RE = re.compile(
    r"(?:sorter|contracts_specialist|legalbench_task)_(v?\d+)"
)


def parse_experiment_name(name: Any) -> dict:
    """Parse a run identifier into structured parts.

    Returns ``{"raw", "model_slug", "prompt_kind", "prompt_version",
    "dimension", "suffix", "friendly_model"}`` with None for missing parts.
    """
    raw = str(name or "")
    m = _NAME_RE.match(raw.strip())
    if not m:
        fragment = _PROMPT_FRAGMENT_RE.search(raw)
        return {
            "raw": raw,
            "model_slug": raw.split("_")[0] or None,
            "prompt_kind": None,
            "prompt_version": fragment.group(1) if fragment else None,
            "dimension": None,
            "suffix": None,
            "friendly_model": display_model(raw.split("_")[0]),
        }
    parts = m.groupdict()
    return {
        "raw": raw,
        "model_slug": parts["model"],
        "prompt_kind": parts["prompt_kind"],
        "prompt_version": parts["prompt"],
        "dimension": parts["dim"],
        "suffix": parts["suffix"],
        "friendly_model": display_model(parts["model"]),
    }


def display_model(model: Optional[str]) -> Optional[str]:
    """Map a model slug to its display name (fallback: the slug itself)."""
    from .config import get_settings

    if not model:
        return None
    display = get_settings().model_display
    return display.get(model, model)


# ---------------------------------------------------------------------------
# Frame normalization
# ---------------------------------------------------------------------------


def normalize_results_frame(frame: pd.DataFrame, kind: str | None = None) -> pd.DataFrame:
    """Enrich a raw results frame into an analysis-ready frame.

    Adds/coerces:
    - ``DATE`` -> datetime ``date``
    - ``MODEL`` / ``Prompt Version`` -> friendly ``model``, ``prompt_version``
    - ``SAMPLE (n)`` -> int ``n``
    - ``Experiment Name`` -> ``experiment_key`` (slug, for dedup)
    - a ``key`` column = ``{model}_{prompt_version}`` for comparison groups
    - guarantees float dtype on the CI columns
    """
    df = frame.copy()
    if "DATE" in df.columns:
        df["date"] = pd.to_datetime(df["DATE"], errors="coerce")

    if "Experiment Name" in df.columns:
        parsed = df["Experiment Name"].map(parse_experiment_name)
        df["model_slug"] = parsed.map(lambda d: d["model_slug"])
        df["prompt_version"] = parsed.map(lambda d: d["prompt_version"])
        df["suffix"] = parsed.map(lambda d: d["suffix"])
        df["experiment_key"] = df["Experiment Name"].astype(str).str.lower()
    if "MODEL" in df.columns:
        df["model"] = df["MODEL"].map(display_model)
    elif "model_slug" in df.columns:
        df["model"] = df["model_slug"].map(display_model)
    if "SAMPLE (n)" in df.columns:
        df["n"] = pd.to_numeric(df["SAMPLE (n)"], errors="coerce").astype("Int64")
    if "n" not in df.columns and "SAMPLE (n)" not in df.columns:
        df["n"] = pd.to_numeric(df.get("n_rows"), errors="coerce").astype("Int64")

    if "model" in df.columns and "prompt_version" in df.columns:
        df["key"] = (df["model"].fillna("?") + "_" + df["prompt_version"].fillna("?"))

    # Coerce known CI columns to float so error bars plot cleanly.
    for col in df.columns:
        low = col.lower()
        if ("ci" in low and ("lo" in low or "hi" in low or "half" in low)) \
                or low.endswith("(equiv)") or "accuracy" in low:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def as_percent(frame: pd.DataFrame, column: str, dropna: bool = True) -> pd.Series:
    """Proportion column as percentages (0.9234 -> 92.34) for display."""
    values = pd.to_numeric(frame[column], errors="coerce")
    if dropna:
        values = values.dropna()
    return values * 100.0


__all__ = [
    "ResultSet", "read_workbook", "read_codebook", "load_log",
    "parse_experiment_name", "display_model", "normalize_results_frame",
    "as_percent",
]
