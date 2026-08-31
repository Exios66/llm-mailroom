"""KANBAN-078 — network-free pins for the Docile-style dataset browser.

Covers notebooks/dataset_browser.py: manifest parsing + provenance rule,
catalog overlay (missing / schema-less / populated read-only DB), summary
honest numbers, text + HTML rendering (incl. HTML escaping of untrusted-ish
manifest fields), and the notebook file itself staying valid nbformat 4 with
its module-import cell intact. No network, no LLM calls, no Jupyter runtime.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from notebooks.dataset_browser import (
    DatasetBrowser,
    _CATALOG_COLUMNS,
    join_catalog,
    load_catalog_state,
    load_manifest,
    pdf_first_page_text,
    render_record_html,
    render_text_table,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "examples" / "samples" / "manifest.csv"

# docs/examples/ is a pruned heavy asset in the monorepo (sample PDFs +
# manifest). The upstream llm-mailroom repo is the reference for these.
pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(),
    reason="docs/examples/samples/manifest.csv absent (pruned heavy asset; see upstream repo)",
)



@pytest.fixture(scope="module")
def records():
    return load_manifest(MANIFEST)


def test_manifest_loads_all_rows_with_provenance(records):
    assert len(records) == 25
    real = [r for r in records if r.is_real]
    synth = [r for r in records if not r.is_real]
    # Mirrors prepare_samples.is_real_sample: CUAD* or external/* = REAL.
    assert len(real) == 15 and len(synth) == 10
    assert all(r.source.startswith(("CUAD", "external/")) for r in real)
    assert all(not r.source.startswith(("CUAD", "external/")) for r in synth)


def test_expected_fields_parse_to_dicts(records):
    contract_01 = next(r for r in records if r.id == "contract_01")
    assert isinstance(contract_01.expected_fields, dict)
    assert "parties" in contract_01.expected_fields
    assert "governing_law" in contract_01.expected_fields


def test_catalog_state_missing_db_is_empty(tmp_path):
    assert load_catalog_state(db_path=tmp_path / "nope.db") == {}


def test_catalog_state_schemaless_db_degrades_to_empty(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # exists, no `documents` table yet
    assert load_catalog_state(db_path=db) == {}


def test_catalog_overlay_joins_by_filename_readonly(tmp_path):
    db = tmp_path / "mailroom.db"
    cols = list(_CATALOG_COLUMNS)
    con = sqlite3.connect(db)
    con.execute(
        f"CREATE TABLE documents ({', '.join(f'{c} TEXT' for c in cols)})"
    )
    payload = {
        "doc_id": "doc_x", "matter_id": "m1",
        "original_filename": "contract_01_affiliate_agreement.pdf",
        "stage": "archived", "doc_type": "contract",
        "classification_confidence": 0.97, "extraction_confidence": 0.93,
        "extracted_data": json.dumps({"governing_law": "Delaware"}),
        "escalation_reason": None, "model": "test-model",
        "prompt_version": "v0", "cost_usd": 0.001, "latency_s": 1.5,
    }
    con.execute(
        f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [payload[c] for c in _CATALOG_COLUMNS],
    )
    con.commit()
    con.close()

    catalog = load_catalog_state(db_path=db)
    assert set(catalog) == {"contract_01_affiliate_agreement.pdf"}
    entry = catalog["contract_01_affiliate_agreement.pdf"]
    assert entry["stage"] == "archived"
    assert entry["extracted_data"] == {"governing_law": "Delaware"}  # decoded

    joined = join_catalog(load_manifest(MANIFEST), catalog)
    row = next(r for r in joined if r["filename"].startswith("contract_01"))
    assert row["in_catalog"] and row["catalog"]["model"] == "test-model"
    other = next(r for r in joined if r["id"] == "corporate_01")
    assert not other["in_catalog"] and other["catalog"] == {}

    s = summarize(joined)
    assert s["total_samples"] == 25
    assert s["in_catalog"] == 1
    assert s["by_observed_stage"] == {"archived": 1, "not_run": 24}


def test_text_table_lists_every_sample(records):
    table = render_text_table(join_catalog(records, {}))
    for rec in records:
        assert rec.id in table


def test_html_renderer_escapes_markup(records):
    row = next(r for r in join_catalog(records, {}) if r["id"] == "corporate_01")
    row["notes"] = "<script>alert(1)</script>"
    html_out = render_record_html(row, include_pdf_text=False)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_pdf_text_never_raises_on_missing_file():
    assert pdf_first_page_text("/nonexistent/path.pdf") == ""


def test_browser_falls_back_to_text_mode_without_widgets(records, capsys, monkeypatch):
    # Force the text-mode constructor even when `[notebooks]` extra (ipywidgets)
    # is installed in the environment — CI is extra-less, local/dev often isn't.
    import builtins

    real_import = builtins.__import__

    def _block_ipywidgets(name, *args, **kwargs):
        if name.split(".")[0] == "ipywidgets":
            raise ImportError("blocked for text-mode pin")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_ipywidgets)
    browser = DatasetBrowser(records, catalog={})
    captured = capsys.readouterr().out
    assert "ipywidgets not installed" in captured
    assert "total_samples" in captured
    browser.show("contract_01")  # text detail path must work too
    assert "contract_01" in capsys.readouterr().out


def test_notebook_stays_valid_nbformat_with_module_import():
    nb = json.loads((REPO_ROOT / "notebooks" / "dataset_browser.ipynb").read_text())
    assert nb["nbformat"] == 4
    code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert any("dataset_browser" in "".join(c["source"]) for c in code_cells)
