"""tools/gt-label-workbook: the editable ground-truth workbook builder (no network)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "gt-label-workbook" / "build_gt_workbook.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("build_gt_workbook", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(doc_id, cls, subclass, gt, split="train", filename=None):
    return {
        "document_id": doc_id, "filename": filename or f"{doc_id}.txt", "split": split,
        "expected": cls, "expected_subclass": subclass, "source_corpus": "synthetic",
        "annotation_method": "synthetic", "gt_fields": json.dumps(gt),
    }


@pytest.fixture()
def rows():
    contract = {
        "cuad_clause_labels": json.dumps({
            "Agreement Date": [{"start": 10, "text": "18th day of August"}],
            "Governing Law": [{"start": 50, "text": "State of Delaware"}],
        }),
        "gt_presence": json.dumps({"cuad_clause_labels": "populated"}),
    }
    pending = {"cuad_clause_labels": "{}", "gt_presence": json.dumps({"cuad_clause_labels": "pending_annotation"})}
    mail = {"intent": "request", "subject_matter": "Meeting", "keywords": '["meeting"]',
            "gt_presence": json.dumps({"intent": "populated"})}
    return pd.DataFrame([
        _row("DOC-1", "contract", "Supply", contract),
        _row("DOC-2", "contract", "Supply", pending, split="test"),
        _row("DOC-3", "correspondence", "email", mail),
    ])


def test_every_scored_field_gets_a_value_and_status_column(tool, rows, tmp_path):
    out = tmp_path / "gt.xlsx"
    stats = tool.build_workbook(rows, out, dataset="org/ds", revision="v0")
    assert stats["sheets"] == ["Contracts", "Correspondence"]
    wb = load_workbook(out)
    headers = [c.value for c in wb["Correspondence"][1]]
    # communication_date is scored by the Dojo but never labelled in the dataset.
    assert "communication_date" in headers and "communication_date | status" in headers
    col = headers.index("communication_date") + 1
    assert wb["Correspondence"].cell(row=2, column=col + 1).value == "UNLABELED"
    assert wb["Correspondence"].cell(row=2, column=col).fill.fgColor.rgb.endswith("FFFF00")


def test_contract_fields_come_from_cuad_spans(tool, rows, tmp_path):
    out = tmp_path / "gt.xlsx"
    tool.build_workbook(rows, out, dataset="org/ds", revision="v0")
    ws = load_workbook(out)["Contracts"]
    headers = [c.value for c in ws[1]]
    by_id = {ws.cell(row=r, column=1).value: r for r in range(2, ws.max_row + 1)}
    gl = headers.index("governing_law") + 1
    assert ws.cell(row=by_id["DOC-1"], column=gl).value == "State of Delaware"
    assert ws.cell(row=by_id["DOC-1"], column=gl + 1).value == "populated"
    assert ws.cell(row=by_id["DOC-2"], column=gl + 1).value == "pending_annotation"
    exp = headers.index("expiration_date") + 1  # empty CUAD category: clause absent
    assert ws.cell(row=by_id["DOC-1"], column=exp + 1).value == "schema_documented_absence"
    formula = ws.cell(row=by_id["DOC-1"], column=len(headers)).value
    assert formula.startswith("=COUNTIF(") and "pending_annotation" in formula


def test_date_audit_reports_stated_components_without_the_clock(tool, rows, tmp_path):
    out = tmp_path / "gt.xlsx"
    tool.build_workbook(rows, out, dataset="org/ds", revision="v0")
    ws = load_workbook(out)["Date Audit"]
    audit = {(r[1], r[5]): r for r in ws.iter_rows(min_row=2, values_only=True)}
    yearless = audit[("DOC-1", "agreement_date")]
    assert yearless[6] == "18th day of August"
    assert yearless[8:12] == (None, 8, 18, "NO YEAR")
    assert audit[("DOC-2", "agreement_date")][11] == "MISSING LABEL"
    assert audit[("DOC-3", "communication_date")][11] == "MISSING LABEL"


@pytest.mark.parametrize(
    "text, parts",
    [
        ("18th day of August", (None, 8, 18)),
        ("August 2014", (2014, 8, None)),
        ("March 12,1999", (1999, 3, 12)),
        ("16 day of August. 2012", (2012, 8, 16)),
        ("February 29", (None, 2, 29)),
        ("not a date", None),
    ],
)
def test_fallback_date_parts_match_the_dojo_rules(tool, text, parts):
    got = tool._fallback_date_parts(text)
    assert (None if got is None else tuple(got)) == parts
