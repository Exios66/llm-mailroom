"""Build an editable ground-truth label workbook (.xlsx) from the pinned corpus.

Every field the Dojo scores gets a value column and a status column, per document
class, whether or not the dataset labels it, so missing labels are visible and can be
filled in. A "Date Audit" sheet lists every date label with the components it
actually states (year / month / day); a "Summary" sheet counts coverage with live
formulas.

Usage (from the repository root):

    PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py \
        --out gt_label_workbook.xlsx

Reads ``config=ground_truth`` (train + test) of the pipeline's full corpus at
``FULL_CORPUS_REVISION`` through ``pipeline.hf_corpus_loader.load_config_frame``
(read-only Hub access, cached under ``<MAILROOM_BASE_DIR>/hf_cache``). Pass
``--parquet-dir DIR`` holding ``gt_train.parquet`` / ``gt_test.parquet`` to build
offline. Nothing is written to Hugging Face.

Column lists come from the Dojo (``extraction_schema``, the corporate-record
subclass entity fields, the CUAD category -> field map), so the workbook follows
the scorer when the Dojo pin moves.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from llm_dojo_scoring.schemas.corporate_record_schema import entity_field_types
from llm_dojo_scoring.schemas.corpus import cms_source_table
from llm_dojo_scoring.schemas.cuad_schema import CUAD_CATEGORIES
from llm_dojo_scoring.schemas.extraction_schemas import extraction_schema

CLASSES: tuple[tuple[str, str], ...] = (
    ("contract", "Contracts"),
    ("merger_agreement", "Merger Agreements"),
    ("corporate_record", "Corporate Records"),
    ("correspondence", "Correspondence"),
    ("insurance_claim", "Insurance Claims"),
)
STATUSES = ("populated", "pending_annotation", "schema_documented_absence", "not_applicable", "UNLABELED")
MISSING = ("UNLABELED", "pending_annotation")
DATE_FLAGS = ("MISSING LABEL", "NO YEAR", "NO DAY/MONTH", "UNPARSEABLE", "OK")
CELL_MAX = 32000  # Excel holds 32,767 characters per cell

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
HEAD = Font(name="Arial", size=10, bold=True, color="FFFFFF")
TITLE = Font(name="Arial", size=14, bold=True)
LINK = Font(name="Arial", size=10, color="0563C1", underline="single")
HEAD_ID = PatternFill("solid", fgColor="44546A")
HEAD_VAL = PatternFill("solid", fgColor="2F5597")
HEAD_ST = PatternFill("solid", fgColor="7F7F7F")
TODO = PatternFill("solid", fgColor="FFFF00")
IDFILL = PatternFill("solid", fgColor="F2F2F2")
ID_COLS = (
    ("document_id", 22), ("filename", 34), ("split", 7), ("expected_subclass", 20),
    ("source_corpus", 18), ("annotation_method", 16), ("Hub viewer", 12),
)


# ---------------------------------------------------------------------------
# Date components (audit only: which parts a label states)


class DateParts(NamedTuple):
    year: int | None
    month: int | None
    day: int | None


def _fallback_date_parts(text: Any) -> DateParts | None:
    """Stated (year, month, day) of ``text`` without reading the clock.

    Audit-only stand-in for ``llm_dojo_scoring.scoring.field_scoring._parse_date_parts``
    (llm-dojo-scoring#134), used only while the pinned Dojo predates it. It parses
    twice with two fixed defaults; a component that differs was not stated. It never
    scores anything.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = re.sub(r"\s+", " ", text.strip())
    s = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)
    s = re.sub(r",(?=\d)", ", ", s)
    s = re.sub(r"\b(\d{1,2})[a-z]*(?:\s+[a-z]+)*\s+day\s+of\b", r"\1 day of", s, flags=re.IGNORECASE)
    m = re.search(r"\b(\d{1,2})\s+day\s+of\s+([A-Za-z]+)[,.\s]*(\d{4})?", s, flags=re.IGNORECASE)
    if m:
        s = f"{m.group(2)} {m.group(1)}, {m.group(3)}" if m.group(3) else f"{m.group(2)} {m.group(1)}"
    s = re.sub(r"[(\[\{,]+$", "", s).strip()
    try:
        from dateutil import parser

        a = parser.parse(s, default=_dt.datetime(2000, 1, 1))
        b = parser.parse(s, default=_dt.datetime(2004, 3, 3))
    except (ValueError, OverflowError):
        return None
    parts = DateParts(
        a.year if a.year == b.year else None,
        a.month if a.month == b.month else None,
        a.day if a.day == b.day else None,
    )
    return None if parts.year is None and parts.month is None else parts


def _date_parts_impl() -> tuple[Callable[[Any], Any], str]:
    try:
        from llm_dojo_scoring.scoring.field_scoring import _parse_date_parts

        return _parse_date_parts, "llm_dojo_scoring _parse_date_parts"
    except ImportError:
        return _fallback_date_parts, "audit-only fallback (pinned Dojo predates llm-dojo-scoring#134)"


def date_flag(span: str, status: str, parts_fn: Callable[[Any], Any]) -> tuple[str, tuple]:
    if not span:
        if status in MISSING:
            return "MISSING LABEL", (None, None, None)
        if status == "not_applicable":
            return "not applicable", (None, None, None)
        return "NO DATE (documented absence)", (None, None, None)
    p = parts_fn(span)
    if p is None:
        return "UNPARSEABLE", (None, None, None)
    stated = (p.year, p.month, p.day)
    if p.year is None:
        return "NO YEAR", stated
    if p.month is None or p.day is None:
        return "NO DAY/MONTH", stated
    return "OK", stated


# ---------------------------------------------------------------------------
# Field values


def _json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def render(value: Any) -> str:
    value = _json(value)
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else ""
    return str(value)


def is_empty(value: Any) -> bool:
    value = _json(value)
    return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0)


_CUAD_FIELD = {c.field: c.name for c in CUAD_CATEGORIES if getattr(c, "field", None)}


def class_columns(cls: str, sub: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(column key, header, field type) for every field the Dojo scores for ``cls``."""
    types = dict(extraction_schema(cls).field_types)
    if cls == "corporate_record":
        for subclass in sorted(sub.expected_subclass.astype(str).unique()):
            for name, ftype in entity_field_types(subclass).items():
                types.setdefault(name, ftype)
    cols: list[tuple[str, str, str]] = []
    if cls == "contract":
        for name, ftype in types.items():
            if name == "cuad_clauses":
                continue
            src = f" (from CUAD '{_CUAD_FIELD[name]}')" if name in _CUAD_FIELD else ""
            cols.append((name, name, ftype + src))
        cols += [(f"cuad::{c.name}", f"CUAD: {c.name}", "cuad clause spans")
                 for c in CUAD_CATEGORIES if not getattr(c, "field", None)]
        return cols
    if cls == "merger_agreement":
        cols += [(n, n, t) for n, t in types.items() if n != "maud_clauses"]
        keys = sorted({k for v in sub.gt_fields for k in (_json(_json(json.loads(v)).get("maud_clause_labels")) or {})})
        cols += [(f"maud::{k}", f"MAUD: {k}", "maud answer") for k in keys]
        return cols
    return [(n, n, t) for n, t in types.items()]


def cell_for(cls: str, key: str, gt: dict, presence: dict, subclass: str) -> tuple[str, str]:
    """(value text, status) of one scored field on one row."""
    if cls == "contract":
        cat = key[6:] if key.startswith("cuad::") else _CUAD_FIELD.get(key)
        if cat is not None:
            if presence.get("cuad_clause_labels") == "pending_annotation":
                return "", "pending_annotation"
            cuad = _json(gt.get("cuad_clause_labels")) or {}
            spans = cuad.get(cat) if isinstance(cuad, dict) else None
            if spans:
                return " || ".join(str(s.get("text", "")) if isinstance(s, dict) else str(s) for s in spans), "populated"
            # CUAD annotators read the whole contract: an empty category is a
            # documented absence of the clause, not a labelling gap.
            return "", "schema_documented_absence"
    if cls == "merger_agreement" and key.startswith("maud::"):
        maud = _json(gt.get("maud_clause_labels")) or {}
        value = maud.get(key[6:]) if isinstance(maud, dict) else None
        return (render(value), "populated") if not is_empty(value) else ("", "UNLABELED")
    if cls == "corporate_record" and key not in extraction_schema(cls).field_types:
        if key not in entity_field_types(subclass):
            return "", "not_applicable"
    if cls == "insurance_claim" and key == "cms_source_table":
        table = cms_source_table(cls, subclass)
        return (table, "populated") if table else ("", "not_applicable")
    value, status = gt.get(key), presence.get(key)
    if status in ("not_applicable", "schema_documented_absence", "pending_annotation"):
        return render(value), status
    return ("", "UNLABELED") if is_empty(value) else (render(value), "populated")


# ---------------------------------------------------------------------------
# Workbook


def _header(ws, col: int, text: str, fill, note: str | None = None) -> None:
    cell = ws.cell(row=1, column=col, value=text)
    cell.font, cell.fill = HEAD, fill
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    if note:
        cell.comment = Comment(note, "gt-label-workbook")


def build_workbook(rows: pd.DataFrame, out: Path, *, dataset: str, revision: str,
                   resolved: str | None = None) -> dict[str, Any]:
    """Write the workbook for ``rows`` (the ground_truth config) to ``out``."""
    parts_fn, parts_source = _date_parts_impl()
    rows = rows.reset_index(drop=True)
    truncated: list[str] = []

    def clip(text: str, where: str) -> str:
        if len(text) > CELL_MAX:
            truncated.append(where)
            return text[:CELL_MAX] + " …[TRUNCATED: full value on the Hub]"
        return text

    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    summary = wb.create_sheet("Summary")
    dates = wb.create_sheet("Date Audit")
    date_rows: list[list] = []
    sheets: list[tuple[str, int, str, str]] = []

    for cls, title in CLASSES:
        sub = rows[rows.expected == cls].sort_values(["split", "expected_subclass", "filename"])
        if sub.empty:
            continue
        ws = wb.create_sheet(title)
        cols = class_columns(cls, sub)
        for i, (name, width) in enumerate(ID_COLS, start=1):
            _header(ws, i, name, HEAD_ID)
            ws.column_dimensions[get_column_letter(i)].width = width
        first = len(ID_COLS) + 1
        for j, (_, header, ftype) in enumerate(cols):
            vc = first + 2 * j
            _header(ws, vc, header, HEAD_VAL, f"Scored field type: {ftype}. Edit the value here.")
            _header(ws, vc + 1, f"{header} | status", HEAD_ST,
                    "Presence code. Set it to 'populated' when you add a value.")
            ws.column_dimensions[get_column_letter(vc)].width = 30
            ws.column_dimensions[get_column_letter(vc + 1)].width = 14
        last = first + 2 * len(cols) - 1
        miss = last + 1
        _header(ws, miss, "Labels missing", HEAD_ID,
                "Formula: this row's fields whose status is UNLABELED or pending_annotation.")
        dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=False)
        ws.add_data_validation(dv)
        for r, (_, row) in enumerate(sub.iterrows(), start=2):
            gt = json.loads(row.gt_fields) if isinstance(row.gt_fields, str) else dict(row.gt_fields)
            presence = _json(gt.get("gt_presence")) or {}
            subclass = str(row.expected_subclass)
            for i, v in enumerate([row.document_id, row.filename, row.split, subclass,
                                   row.source_corpus, row.annotation_method], start=1):
                c = ws.cell(row=r, column=i, value=str(v))
                c.font, c.fill = FONT, IDFILL
            link = ws.cell(row=r, column=7, value="open")
            link.hyperlink = f"https://huggingface.co/datasets/{dataset}/viewer/default/{row.split}?q={row.document_id}"
            link.font, link.fill = LINK, IDFILL
            for j, (key, header, ftype) in enumerate(cols):
                value, status = cell_for(cls, key, gt, presence, subclass)
                vcell = ws.cell(row=r, column=first + 2 * j, value=clip(value, f"{title}:{row.document_id}:{header}"))
                scell = ws.cell(row=r, column=first + 2 * j + 1, value=status)
                vcell.font = scell.font = FONT
                if status in MISSING:
                    vcell.fill = TODO
                if ftype.startswith("date"):
                    for span in (value.split(" || ") if value else [""]):
                        flag, stated = date_flag(span, status, parts_fn)
                        date_rows.append([title, row.document_id, row.filename, row.split, subclass,
                                          header, span, status, *stated, flag])
            dv.add(f"{get_column_letter(first + 1)}{r}:{get_column_letter(last)}{r}")
            rng = f"{get_column_letter(first)}{r}:{get_column_letter(last)}{r}"
            ws.cell(row=r, column=miss, value=f'=COUNTIF({rng},"UNLABELED")+COUNTIF({rng},"pending_annotation")').font = BOLD
        ws.freeze_panes = ws.cell(row=2, column=3)
        ws.auto_filter.ref = f"A1:{get_column_letter(miss)}{len(sub) + 1}"
        ws.row_dimensions[1].height = 45
        sheets.append((title, len(sub), get_column_letter(first), get_column_letter(last)))

    heads = ["Sheet", "document_id", "filename", "split", "subclass", "date field",
             "GT value (one span per row)", "status", "stated year", "stated month", "stated day",
             "flag", "Corrected value (edit)", "Notes (edit)"]
    for i, (h, w) in enumerate(zip(heads, (16, 22, 34, 7, 18, 18, 40, 16, 9, 9, 9, 22, 26, 30)), start=1):
        _header(dates, i, h, HEAD_VAL if i >= 13 else HEAD_ID)
        dates.column_dimensions[get_column_letter(i)].width = w
    for r, rec in enumerate(date_rows, start=2):
        for i, v in enumerate(rec, start=1):
            dates.cell(row=r, column=i, value=v).font = FONT
        if rec[11] in ("MISSING LABEL", "NO YEAR", "NO DAY/MONTH", "UNPARSEABLE"):
            for i in (13, 14):
                dates.cell(row=r, column=i).fill = TODO
    dates.freeze_panes = "C2"
    dates.auto_filter.ref = f"A1:N{max(len(date_rows), 1) + 1}"
    dates.cell(row=1, column=9).comment = Comment(
        f"Stated components come from {parts_source} at build time; a blank means the label "
        "does not state it. The clock is never read.", "gt-label-workbook")

    summary["A1"] = "Ground-truth label coverage (live formulas over the class sheets)"
    summary["A1"].font = TITLE
    hdr = ["Class sheet", "Documents", "Field cells", "populated", "UNLABELED", "pending_annotation",
           "schema_documented_absence", "not_applicable", "Missing labels", "Missing share"]
    for i, h in enumerate(hdr, start=1):
        c = summary.cell(row=3, column=i, value=h)
        c.font, c.fill = HEAD, HEAD_ID
        summary.column_dimensions[get_column_letter(i)].width = 22 if i == 1 else 16
    for r, (title, n, c0, c1) in enumerate(sheets, start=4):
        q = f"'{title}'"
        rng = f"{q}!{c0}2:{c1}{n + 1}"
        summary.cell(row=r, column=1, value=title).font = FONT
        summary.cell(row=r, column=2, value=f"=COUNTA({q}!A2:A{n + 1})").font = FONT
        summary.cell(row=r, column=3, value=f"=SUM(D{r}:H{r})").font = FONT
        for i, s in enumerate(STATUSES[:1] + ("UNLABELED", "pending_annotation", "schema_documented_absence",
                                               "not_applicable"), start=4):
            summary.cell(row=r, column=i, value=f'=COUNTIF({rng},"{s}")').font = FONT
        summary.cell(row=r, column=9, value=f"=E{r}+F{r}").font = FONT
        c = summary.cell(row=r, column=10, value=f"=IF(C{r}=0,0,I{r}/C{r})")
        c.font, c.number_format = FONT, "0.0%"
    tot = 4 + len(sheets)
    summary.cell(row=tot, column=1, value="Total").font = BOLD
    for i in range(2, 10):
        col = get_column_letter(i)
        summary.cell(row=tot, column=i, value=f"=SUM({col}4:{col}{tot - 1})").font = BOLD
    c = summary.cell(row=tot, column=10, value=f"=IF(C{tot}=0,0,I{tot}/C{tot})")
    c.font, c.number_format = BOLD, "0.0%"
    summary.cell(row=tot + 2, column=1, value="Date Audit flags").font = BOLD
    for k, flag in enumerate(DATE_FLAGS, start=tot + 3):
        summary.cell(row=k, column=1, value=flag).font = FONT
        summary.cell(row=k, column=2, value=f"=COUNTIF('Date Audit'!L2:L{max(len(date_rows), 1) + 1},A{k})").font = FONT

    pin = f"{revision}" + (f" = commit {resolved}" if resolved else "")
    lines = [
        ("Mailroom ground-truth label workbook", TITLE),
        ("", FONT),
        (f"Source: Hugging Face dataset {dataset}, config ground_truth (train + test), revision {pin}. "
         f"{len(rows)} documents. Built {_dt.date.today().isoformat()} by tools/gt-label-workbook/build_gt_workbook.py.", FONT),
        ("Columns: a value column and a status column for EVERY field the Dojo scores for that class "
         "(extraction_schema; corporate records add each subclass's entity fields; contract dates come from "
         "the CUAD clause spans, e.g. 'Agreement Date' -> agreement_date).", FONT),
        ("", FONT),
        ("HOW TO EDIT", BOLD),
        ("1. Yellow cells are missing labels (status UNLABELED or pending_annotation). Type the value as the "
         "document states it, then set the status cell to 'populated' (dropdown).", FONT),
        ("2. Dates: write the date exactly as the document states it, INCLUDING THE YEAR when the document has "
         "one (e.g. 'August 18, 2014'). The scorer never fills a missing year from the clock, so a label "
         "without a year cannot check the model's year.", FONT),
        ("3. Lists (parties, keywords, signatories, ...): a JSON array, e.g. [\"Acme Corp\", \"Beta LLC\"]. "
         "CUAD spans: separate several spans with ' || '. Spans show their text only; the source keeps each "
         "span's character offset.", FONT),
        ("4. A field the document genuinely does not contain: leave the value blank and set status "
         "'schema_documented_absence'. A field that cannot apply to this subclass: 'not_applicable'.", FONT),
        ("5. Grey columns (document_id ... annotation_method) identify the row: do not edit them. 'Hub viewer' "
         "opens the dataset viewer searched for the document_id, to read the text.", FONT),
        ("6. 'Date Audit' lists every date label, one span per row, with the components it states; fill "
         "'Corrected value' for rows flagged MISSING LABEL, NO YEAR, NO DAY/MONTH or UNPARSEABLE.", FONT),
        ("", FONT),
        ("STATUS CODES", BOLD),
        ("populated - a value is labelled and will be scored.", FONT),
        ("pending_annotation - known gap in the dataset; not scored until labelled.", FONT),
        ("schema_documented_absence - the document does not contain this field; a blank prediction is correct.", FONT),
        ("not_applicable - the field does not apply to this class/subclass.", FONT),
        ("UNLABELED - the Dojo scores this field but the dataset carries no label for it (not scored today).", FONT),
        ("", FONT),
        ("EXAMPLE OF A FINISHED EDIT (Correspondence sheet)", BOLD),
        ("communication_date: 'March 3, 2001'   |   status: populated", FONT),
        ("", FONT),
        ("GETTING EDITS BACK INTO THE DATASET", BOLD),
        ("This workbook is a working copy. Edited labels reach the Hub only through the dataset's own reviewed "
         "publish step, run by the dataset owner; nothing here writes to Hugging Face.", FONT),
        (f"Values longer than an Excel cell holds are cut and marked [TRUNCATED]; edit those on the Hub. "
         f"Truncated cells in this build: {len(truncated)}.", FONT),
    ]
    for i, (text, font) in enumerate(lines, start=1):
        c = readme.cell(row=i, column=1, value=text)
        c.font = font
        c.alignment = Alignment(wrap_text=True, vertical="top")
    readme.column_dimensions["A"].width = 140

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"out": str(out), "rows": len(rows), "date_rows": len(date_rows), "truncated": len(truncated),
            "date_parts": parts_source, "sheets": [s[0] for s in sheets]}


# ---------------------------------------------------------------------------
# CLI


def load_rows(parquet_dir: Path | None, revision: str) -> tuple[pd.DataFrame, str | None, str]:
    from pipeline.hf_corpora import FULL_CORPUS_ID

    if parquet_dir is not None:
        frames = [pd.read_parquet(parquet_dir / f"gt_{split}.parquet") for split in ("train", "test")]
        return pd.concat(frames, ignore_index=True), None, FULL_CORPUS_ID
    from pipeline.hf_corpus_loader import load_config_frame

    frames, resolved = [], None
    for split in ("train", "test"):
        frame, provenance = load_config_frame(FULL_CORPUS_ID, "ground_truth", split=split, revision=revision)
        resolved = provenance.get("revision_resolved") or resolved
        if "split" not in frame.columns:
            frame = frame.assign(split=split)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), resolved, FULL_CORPUS_ID


def main(argv: list[str] | None = None) -> int:
    from pipeline.hf_corpora import FULL_CORPUS_REVISION

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="workbook path (.xlsx)")
    ap.add_argument("--revision", default=FULL_CORPUS_REVISION,
                    help=f"corpus revision (default: the pipeline pin, {FULL_CORPUS_REVISION})")
    ap.add_argument("--parquet-dir", type=Path, default=None,
                    help="offline: a directory with gt_train.parquet and gt_test.parquet")
    args = ap.parse_args(argv)
    rows, resolved, dataset = load_rows(args.parquet_dir, args.revision)
    print(json.dumps(build_workbook(rows, args.out, dataset=dataset, revision=args.revision, resolved=resolved)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
