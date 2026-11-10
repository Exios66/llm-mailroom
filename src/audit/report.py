"""``audit-report``: one Markdown section per live document type (spec §7, #235 acceptance 3).

Each section names the type's extraction schema, n, the extraction model and its
route (``Modal`` when the model is a gateway GPU tier alias in
``taxonomy.yaml``, else ``API``), then F1, F2 and every other collected metric
of ``audit_row_metrics``. A metric no document of the type collected reads
``not collected``; it is never filled in. The records come from the audit DB
or from the warehouse's ``audit_rows_<date>.parquet`` files, and both give the
same report.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from audit.metrics import METRIC_KEYS

NOT_COLLECTED = "not collected"
#: Text-valued keys: shown as counts (scoring status) or used for grouping, never averaged.
_TEXT_KEYS = ("outcome", "doc_type", "doc_subclass", "scoring_status", "model_scoring_status")
#: F1 and F2 first, then the rest in ``METRIC_KEYS`` order.
REPORT_METRICS: tuple[str, ...] = ("extraction_f1", "extraction_f2") + tuple(
    k for k in METRIC_KEYS if k not in _TEXT_KEYS + ("extraction_f1", "extraction_f2"))


def _record(detail: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict[str, Any]:
    extraction = detail.get("extraction") if isinstance(detail.get("extraction"), Mapping) else {}
    return {**{k: metrics.get(k) for k in METRIC_KEYS}, "model": extraction.get("model")}


async def records_from_db() -> list[dict[str, Any]]:
    from audit.metrics import metrics_for
    from audit.readers import all_rows

    return [_record(full.get("detail") or {}, metrics_for(full)) for full in await all_rows()]


def records_from_parquet(directory: Path) -> list[dict[str, Any]]:
    """The newest export of each row across every ``audit_rows_<date>.parquet`` in ``directory``."""
    from storage.warehouse import _read_parquet_rows

    latest: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(directory).glob("audit_rows_*.parquet")):
        for row in _read_parquet_rows(path):
            key = str(row.get("entry_id"))
            if key not in latest or str(row.get("exported_at") or "") >= str(latest[key].get("exported_at") or ""):
                latest[key] = row
    return [_record(json.loads(r.get("detail_json") or "{}"), r) for r in latest.values()]


def _modal_aliases() -> set[str]:
    from pipeline.config import load_config

    tiers = ((load_config().get("gateway") or {}).get("tiers") or {}).values()
    return {str(t["alias"]) for t in tiers if isinstance(t, Mapping) and t.get("alias")}


def _counts(values: Iterable[str]) -> str:
    c = Counter(values)
    return ", ".join(f"{k} ({n})" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) or NOT_COLLECTED


def _cell(values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return NOT_COLLECTED
    if all(isinstance(v, bool) for v in vals):
        return f"{100.0 * sum(vals) / len(vals):.1f}% (n={len(vals)})"
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return f"{sum(nums) / len(nums):.4f} (n={len(nums)})" if nums else NOT_COLLECTED


def build_report(records: list[Mapping[str, Any]]) -> str:
    from pipeline.config import get_extraction_schema_name

    modal = _modal_aliases()
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for rec in records:
        by_type.setdefault(str(rec.get("doc_type") or "(unclassified)"), []).append(rec)
    outcomes = Counter(str(r.get("outcome")) for r in records)
    lines = ["# Audit report", "",
             f"Documents: {len(records)} ({', '.join(f'{k} {n}' for k, n in sorted(outcomes.items())) or 'none'})",
             ""]
    other = []
    for doc_type in sorted(by_type):
        recs = by_type[doc_type]
        schema = get_extraction_schema_name(doc_type) if doc_type != "(unclassified)" else None
        if not schema:
            other.append(f"{doc_type}: {len(recs)}")
            continue
        models = [r["model"] for r in recs if r.get("model")]
        lines += [f"## {doc_type}", "", "| Field | Value |", "|:---|:---|",
                  f"| Schema | {schema} |", f"| n | {len(recs)} |",
                  f"| Model | {_counts(models)} |",
                  f"| Route | {_counts('Modal' if m in modal else 'API' for m in models)} |",
                  f"| Scoring status | {_counts(str(r['scoring_status']) for r in recs if r.get('scoring_status'))} |"]
        lines += [f"| {key} | {_cell([r.get(key) for r in recs])} |" for key in REPORT_METRICS]
        lines.append("")
    if other:
        lines += ["## Other document types", "", "No extraction schema: " + ", ".join(other), ""]
    return "\n".join(lines)


async def render_report(*, parquet_dir: Path | None = None) -> str:
    """The report from the audit DB, or from the Parquet files in ``parquet_dir``."""
    records = records_from_parquet(parquet_dir) if parquet_dir is not None else await records_from_db()
    return build_report(records)


__all__ = ["NOT_COLLECTED", "REPORT_METRICS", "build_report", "records_from_db", "records_from_parquet",
           "render_report"]
