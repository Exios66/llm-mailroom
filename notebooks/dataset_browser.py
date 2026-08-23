"""Mailroom dataset browser — Docile-style tooling.

Pattern borrowed from rossumai/docile (`docile/tools/dataset_browser.py` +
a thin `dataset_browser.ipynb`): a reusable module does the actual work;
the notebook next to it only loads data and renders the browser.

What it browses
---------------
The pilot sample set defined by `docs/examples/samples/manifest.csv`
(30 rows: id, subdir/filename, expected doc class/stage, size tier,
provenance `source` — CUAD / external/ = REAL committed legal documents,
anything else = synthetic .txt rendered to PDF only for mock runs — license,
notes, and ground-truth `expected_fields` JSON), optionally joined with the
pipeline's own catalog state (`data/mailroom.db`, read-only) so you can see,
per sample, what the pipeline actually did: stage reached, confidences,
extracted payload, model/prompt provenance, cost and latency.

No network access, no LLM calls: everything here reads local files.
"""

from __future__ import annotations

import csv
import html
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "examples" / "samples" / "manifest.csv"
DEFAULT_SAMPLES_DIR = REPO_ROOT / "data" / "samples"
DEFAULT_DB = REPO_ROOT / "data" / "mailroom.db"

# Mirrors scripts/prepare_samples.py:is_real_sample — the provenance rule.
_REAL_PREFIXES = ("CUAD", "external/")


@dataclass
class SampleRecord:
    """One manifest row plus derived materialization info."""

    id: str
    subdir: str
    filename: str
    expected_doc_class: str
    expected_stage: str
    size_tier: str
    source: str
    license: str
    notes: str
    dataset: str
    expected_fields: dict
    is_real: bool
    pdf_path: str | None  # None until prepare_samples has materialized it


def _parse_expected_fields(raw: str) -> dict:
    """Manifest CSV stores ground-truth fields as a JSON object; be tolerant
    of blank/None and malformed values rather than crashing the browser."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    samples_dir: Path = DEFAULT_SAMPLES_DIR,
) -> list[SampleRecord]:
    """Parse the pilot manifest into SampleRecords."""
    records: list[SampleRecord] = []
    with open(manifest_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            source = (row.get("source") or "").strip()
            rel = Path(row["subdir"]) / row["filename"]
            pdf = samples_dir / rel
            records.append(
                SampleRecord(
                    id=row["id"],
                    subdir=row["subdir"],
                    filename=row["filename"],
                    expected_doc_class=row.get("expected_doc_class", ""),
                    expected_stage=row.get("expected_stage", ""),
                    size_tier=row.get("size_tier", ""),
                    source=source,
                    license=row.get("license", ""),
                    notes=row.get("notes", ""),
                    dataset=row.get("dataset", ""),
                    expected_fields=_parse_expected_fields(row.get("expected_fields") or ""),
                    is_real=source.startswith(_REAL_PREFIXES),
                    pdf_path=str(pdf) if pdf.exists() else None,
                )
            )
    return records


_CATALOG_COLUMNS = (
    "doc_id",
    "matter_id",
    "original_filename",
    "stage",
    "doc_type",
    "classification_confidence",
    "extraction_confidence",
    "extracted_data",
    "escalation_reason",
    "model",
    "prompt_version",
    "cost_usd",
    "latency_s",
)


def load_catalog_state(db_path: Path = DEFAULT_DB) -> dict[str, dict]:
    """Read-only snapshot of the pipeline catalog, keyed by original filename.

    Returns {} when the database does not exist yet (fresh checkout — run the
    pipeline once to get catalog overlays). Opens SQLite in URI read-only mode
    so browsing can never create or mutate the DB (WAL sidecars included).
    """
    if not db_path.exists():
        return {}
    uri = f"file:{db_path.resolve()}?mode=ro"
    cols = ", ".join(_CATALOG_COLUMNS)
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"SELECT {cols} FROM documents").fetchall()
    except sqlite3.OperationalError:
        return {}  # db exists but schema not created yet — nothing to overlay
    finally:
        con.close()
    state: dict[str, dict] = {}
    for r in rows:
        entry = {k: r[k] for k in _CATALOG_COLUMNS}
        raw = entry.get("extracted_data")
        if isinstance(raw, str):
            try:
                entry["extracted_data"] = json.loads(raw)
            except json.JSONDecodeError:
                pass  # leave as string; the browser renders either fine
        # Last row wins on duplicate filenames (re-runs overwrite by doc_id,
        # but a filename can legitimately appear twice across matters).
        state[r["original_filename"]] = entry
    return state


def join_catalog(
    records: list[SampleRecord],
    catalog: dict[str, dict],
) -> list[dict]:
    """Merge manifest rows (ground truth) with catalog rows (observed)."""
    joined: list[dict] = []
    for rec in records:
        row = asdict(rec)
        entry = catalog.get(rec.filename)
        row["in_catalog"] = entry is not None
        row["catalog"] = entry or {}
        joined.append(row)
    return joined


def summarize(joined: list[dict]) -> dict:
    """Small honest-numbers overview for a markdown/print header."""
    by_class: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    for row in joined:
        by_class[row["expected_doc_class"]] = by_class.get(row["expected_doc_class"], 0) + 1
        stage = row["catalog"].get("stage") or ("not_run" if not row["in_catalog"] else "unknown")
        by_stage[stage] = by_stage.get(stage, 0) + 1
    return {
        "total_samples": len(joined),
        "real_documents": sum(1 for r in joined if r["is_real"]),
        "synthetic": sum(1 for r in joined if not r["is_real"]),
        "materialized_pdfs": sum(1 for r in joined if r["pdf_path"]),
        "in_catalog": sum(1 for r in joined if r["in_catalog"]),
        "by_expected_class": dict(sorted(by_class.items())),
        "by_observed_stage": dict(sorted(by_stage.items())),
    }


def pdf_first_page_text(pdf_path: str | Path, max_chars: int = 1200) -> str:
    """First-page text via pdfplumber (already a pipeline dependency);
    empty string for missing/unreadable PDFs — never raises."""
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
        return text[:max_chars]
    except Exception:
        return ""


def render_record_html(row: dict, include_pdf_text: bool = True) -> str:
    """One sample as an HTML block: identity/provenance table, ground-truth
    fields, observed catalog record, optional first-page PDF text."""

    def esc(v) -> str:
        return html.escape(str(v))

    def kv_table(pairs, title):
        rows = "".join(
            f"<tr><td style='padding:2px 10px 2px 0;font-weight:bold'>{esc(k)}</td>"
            f"<td style='padding:2px 0'><code>{esc(v)}</code></td></tr>"
            if isinstance(v, str) and len(str(v)) < 90
            else f"<tr><td style='padding:2px 10px 2px 0;font-weight:bold'>{esc(k)}</td>"
            f"<td style='padding:2px 0'><pre style='margin:0;white-space:pre-wrap'>"
            f"{esc(json.dumps(v, indent=2) if not isinstance(v, str) else v)}</pre></td></tr>"
            for k, v in pairs
        )
        return f"<h4>{esc(title)}</h4><table>{rows}</table>"

    parts = [
        f"<h3>{esc(row['id'])} — {esc(row['filename'])}</h3>",
        kv_table(
            [
                ("expected class", row["expected_doc_class"]),
                ("expected stage", row["expected_stage"]),
                ("size tier", row["size_tier"]),
                ("real / synthetic", "REAL document" if row["is_real"] else "synthetic"),
                ("source", row["source"]),
                ("license", row["license"]),
                ("pdf materialized", "yes" if row["pdf_path"] else "NO (run prepare_samples)"),
                ("notes", row["notes"]),
            ],
            "Provenance",
        ),
    ]
    parts.append(kv_table(sorted(row["expected_fields"].items()), "Ground-truth fields (manifest)"))
    if row["in_catalog"]:
        parts.append(kv_table(sorted(row["catalog"].items()), "Observed (catalog)"))
    else:
        parts.append("<p><em>Not in catalog yet — this sample has not been through the "
                     "pipeline (or MAILROOM_BASE_DIR points elsewhere).</em></p>")
    if include_pdf_text and row["pdf_path"]:
        text = pdf_first_page_text(row["pdf_path"])
        if text:
            parts.append(
                "<h4>PDF first-page text</h4><pre style='white-space:pre-wrap;"
                f"background:#f6f6f6;padding:8px'>{esc(text)}</pre>"
            )
    return "".join(parts)


def render_text_table(joined: list[dict]) -> str:
    """Dependency-free listing used when ipywidgets isn't installed."""
    lines = ["* = real document, - = synthetic; stage '-' = not in catalog", ""]
    lines.append(f"{'id':<14} {'class':<18} {'stage':<14} src  filename")
    lines.append("-" * 88)
    for row in joined:
        mark = "*" if row["is_real"] else "-"
        stage = row["catalog"].get("stage") or "-"
        lines.append(
            f"{row['id']:<14} {row['expected_doc_class']:<18} {stage:<14} "
            f"{mark:<4} {row['filename']}"
        )
    return "\n".join(lines)


def render_text_detail(row: dict) -> str:
    out = [f"== {row['id']} ({row['expected_doc_class']}) — {row['filename']}",
           f"   source: {row['source']} [{row['license']}]",
           f"   expected: stage={row['expected_stage']} tier={row['size_tier']}",
           f"   notes: {row['notes']}"]
    if row["expected_fields"]:
        out.append("   ground-truth fields:")
        for k in sorted(row["expected_fields"]):
            out.append(f"     - {k}")
    if row["in_catalog"]:
        c = row["catalog"]
        out += [f"   catalog: stage={c.get('stage')} doc_type={c.get('doc_type')}",
                f"   model={c.get('model')} prompt={c.get('prompt_version')} "
                f"cost=${c.get('cost_usd') or 0:.4f} latency={c.get('latency_s') or 0:.2f}s"]
    else:
        out.append("   catalog: (not run)")
    return "\n".join(out)


class DatasetBrowser:
    """Interactive per-sample browser (Docile's DatasetBrowser analog).

    With ipywidgets installed: dropdown + rich HTML pane. Without it, prints
    the dependency-free text listing and stays fully usable via
    ``browser.show('contract_01')``.
    """

    def __init__(
        self,
        records: list[SampleRecord] | None = None,
        catalog: dict[str, dict] | None = None,
        include_pdf_text: bool = True,
    ):
        self.records = records if records is not None else load_manifest()
        if catalog is None:
            catalog = load_catalog_state()
        self.rows = join_catalog(self.records, catalog)
        self.by_id = {r["id"]: r for r in self.rows}
        self.include_pdf_text = include_pdf_text
        self._widget = None
        try:
            import ipywidgets  # noqa: F401

            self._build_widgets()
        except ImportError:
            print(summarize(self.rows))
            print()
            print("ipywidgets not installed — text mode. pip install 'llm-mailroom[notebooks]' "
                  "for the interactive widget.\n")
            print(render_text_table(self.rows))
            print("\nbrowser.show('<sample_id>') for details.")

    def _build_widgets(self) -> None:
        import ipywidgets as widgets
        from IPython.display import display

        opts = [(f"{r['id']}  ({r['expected_doc_class']})", r["id"]) for r in self.rows]
        self._picker = widgets.Dropdown(options=opts, description="sample:",
                                        layout=widgets.Layout(width="480px"))
        self._out = widgets.Output(layout=widgets.Layout(border="1px solid #ccc",
                                                         padding="8px", max_height="640px"))
        self._picker.observe(self._on_pick, names="value")
        self._ui = widgets.VBox([self._picker, self._out])
        self.show(self.rows[0]["id"])
        display(self._ui)
        self._widget = True

    def _on_pick(self, change) -> None:
        self.show(change["new"])

    def show(self, sample_id: str) -> None:
        row = self.by_id[sample_id]
        if self._widget is not None:
            from IPython.display import HTML, display

            with self._out:
                self._out.clear_output(wait=True)
                display(HTML(render_record_html(row, self.include_pdf_text)))
        else:
            print(render_text_detail(row))
