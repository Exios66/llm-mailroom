"""Build an editable ground-truth label workbook (.xlsx) from the pinned corpus.

Every field the Dojo scores gets a value column and a status column, per document
class, whether or not the dataset labels it, so missing labels are visible and can be
filled in. A "Date Audit" sheet lists every date label with the components it
actually states (year / month / day); a "Summary" sheet counts coverage with live
formulas.

Usage (from the repository root):

    PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py \
        --out ~/gt_label_workbook.xlsx   # outside the repo; *.xlsx is git-ignored

Reads ``config=ground_truth`` (train + test) of the pipeline's full corpus at
``FULL_CORPUS_REVISION`` through ``pipeline.hf_corpus_loader.load_config_frame``
(read-only Hub access, cached under ``<MAILROOM_BASE_DIR>/hf_cache``). Pass
``--parquet-dir DIR`` holding ``gt_train.parquet`` / ``gt_test.parquet`` to build
offline. Nothing is written to Hugging Face.

``--to-label`` (default: insurance_claim, corporate_record, correspondence) adds, per
class, a "To Label" sheet (one row per document with a missing label, linked to its
original and to its full text) and a "Text" sheet holding the full document text from
the ``default`` config, split over rows of at most 32,000 characters so nothing is cut.
Offline, ``--parquet-dir`` then also needs ``default_train.parquet`` / ``default_test.parquet``.

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
from typing import Any, Callable, Iterable, NamedTuple
from urllib.parse import quote

import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.hyperlink import Hyperlink

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
EXCEL_CELL_MAX = 32767
TEXT_CHUNK = 32000  # document text is split across rows of at most this many characters
DATE_PROBLEMS = ("NO YEAR", "NO DAY/MONTH", "UNPARSEABLE")
TO_LABEL_DEFAULT: tuple[str, ...] = ("insurance_claim", "corporate_record", "correspondence")
MAX_WORKBOOK_MB = 150.0  # above this, document texts go to .txt files beside the workbook
SEC_BASE = "https://www.sec.gov"

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
HEAD = Font(name="Arial", size=10, bold=True, color="FFFFFF")
TITLE = Font(name="Arial", size=14, bold=True)
LINK = Font(name="Arial", size=10, color="0563C1", underline="single")
GREY = Font(name="Arial", size=9, color="808080")
HEAD_TXT = PatternFill("solid", fgColor="D9E1F2")
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
# Document text and links (To Label / Text sheets)

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_text(text: str) -> tuple[str, int]:
    """``text`` made safe for an XLSX cell, and how many characters were replaced.

    XML 1.0 cannot hold most control characters, so each one becomes its Unicode
    "control picture" (U+2400 + code point, e.g. U+0001 -> U+2401). The replacement is
    one-for-one: the length is unchanged and nothing is dropped or cut.
    """
    return _CONTROL.subn(lambda m: chr(0x2400 + ord(m.group())), text)


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _cut_point(window: str) -> int:
    """Length of the head of ``window`` to keep: after a blank line, line break, or space."""
    half = len(window) // 2
    for sep in ("\n\n", "\n"):
        i = window.rfind(sep, half)
        if i >= 0:
            return i + len(sep)
    i = max(window.rfind(" ", half), window.rfind("\t", half))
    return i + 1 if i >= 0 else len(window)


def chunk_text(text: str, limit: int = TEXT_CHUNK) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters; ``"".join(pieces) == text``.

    Cuts fall after the last blank line, else line break, else space or tab in the second
    half of the window; a text with none of those is cut at the limit. Nothing is dropped.
    Each piece also stays inside Excel's 32,767-UTF-16-unit cell cap.
    """
    if limit < 2 or limit > EXCEL_CELL_MAX:
        raise ValueError(f"limit must be between 2 and {EXCEL_CELL_MAX}")
    pieces: list[str] = []
    pos, n = 0, len(text)
    while True:
        lim = limit
        while True:  # astral characters count twice towards Excel's cap
            window = text[pos:pos + lim]
            over = _utf16_len(window) - EXCEL_CELL_MAX
            if over <= 0 or lim <= 2:
                break
            lim = max(2, lim - over)
        if pos + len(window) >= n:
            pieces.append(window)
            return pieces
        cut = _cut_point(window)
        pieces.append(window[:cut])
        pos += cut


def _meta(value: Any) -> dict:
    value = _json(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def original_link(meta: Any, dataset: str, commit: str | None,
                  hub_files: Iterable[str] | None) -> tuple[str, str] | None:
    """(URL, label) of the original document, or ``None`` when the dataset links none.

    A file the dataset repo itself holds at ``commit`` (its ``original_file`` path is in
    ``hub_files``) links to the pinned Hub blob. Failing that, an SEC EDGAR exhibit
    (``exhibit_url``) links to sec.gov. Nothing is guessed.
    """
    meta = _meta(meta)
    path = str(meta.get("original_file") or "").strip()
    if path and commit and hub_files is not None and path in set(hub_files):
        return f"https://huggingface.co/datasets/{dataset}/blob/{commit}/{quote(path)}", "Hub file"
    url = str(meta.get("exhibit_url") or "").strip()
    if url.startswith("/Archives/"):
        url = SEC_BASE + url
    if url.startswith("https://www.sec.gov/Archives/"):
        return url, "SEC EDGAR"
    return None


def _set_str(cell, value: str) -> None:
    """Store ``value`` as text even when it starts with '=' (openpyxl would make a formula)."""
    cell.value = value
    cell.data_type = "s"


def _goto(cell, sheet: str, ref: str, text: str) -> None:
    cell.value = text
    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!{ref}", display=text)
    cell.font = LINK


def _url(cell, url: str, text: str) -> None:
    cell.value = text
    cell.hyperlink = url
    cell.font = LINK


class _Doc(NamedTuple):
    row: Any
    subclass: str
    cells: list  # (key, header, ftype, value, status, flags)
    need: list[int]
    link: tuple[str, str] | None


# ---------------------------------------------------------------------------
# Workbook


def _header(ws, col: int, text: str, fill, note: str | None = None) -> None:
    cell = ws.cell(row=1, column=col, value=text)
    cell.font, cell.fill = HEAD, fill
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    if note:
        cell.comment = Comment(note, "gt-label-workbook")


def _to_label_readme(tl: dict[str, Any], *, truncated_texts: int) -> list[tuple[str, Font]]:
    if not tl["sheets"]:
        return []
    names = ", ".join(f"'{n}'" for n, *_ in tl["sheets"])
    where = ("the 'Text – ...' sheets" if tl["text_mode"] == "sheet"
             else f"the .txt files in the '{tl['text_dir']}' folder beside this workbook")
    lines = [
        ("TO LABEL SHEETS", BOLD),
        (f"{names} list only the documents that still need a label, one document per row, sorted by "
         "subclass then document_id. A document is listed when a field is UNLABELED or pending_annotation, "
         "or a date label is flagged NO YEAR, NO DAY/MONTH or UNPARSEABLE (see 'Date Audit'). Only fields "
         "that need a label on at least one row get a column pair (value, then status).", FONT),
        ("Yellow value cells need a label. Fill the value, then set the status cell to 'populated'. "
         "'fields needing labels' is the count when the workbook was built; 'still missing (live)' is a "
         "formula that falls as you set statuses.", FONT),
        ("DATES MUST INCLUDE THE YEAR whenever the document states one, e.g. 'August 18, 2014', never "
         "'18th day of August' or 'August 18'. 'date flags' names the fields with a date problem.", FONT),
    ]
    if tl["has_original"]:
        lines.append(("'Open original' opens the source document: an SEC EDGAR exhibit for corporate records "
                      "(the link comes from the dataset's exhibit_url; sec.gov may ask a browser to confirm "
                      "it is not automated). The dataset repo itself holds no original files, so there is "
                      "no Hub file link.", FONT))
    else:
        lines.append(("The dataset repo holds no original files and its metadata gives no source URL for these "
                      "classes, so there is no 'Open original' column; use 'Open text'.", FONT))
    lines.append((f"'Open text' jumps to the document's full text in {where}. The text is never cut: texts "
                  f"longer than {TEXT_CHUNK:,} characters continue on the following rows (parts 1/n, 2/n, ...), "
                  "split at blank lines or line breaks where possible. Control characters that XLSX cannot "
                  "store are shown as their symbol (e.g. U+2401). In the text sheet, 'back to To Label' "
                  "returns to the document's row. "
                  f"Texts cut in this build: {truncated_texts}.", FONT))
    lines.append(("", FONT))
    return lines


def _add_to_label(wb: Workbook, rows: pd.DataFrame, class_cols: dict, to_label: tuple[str, ...], *,
                  parts_fn: Callable[[Any], Any], dataset: str, commit: str | None,
                  hub_files: Iterable[str] | None, out: Path, max_mb: float,
                  clip: Callable[[str, str], str]) -> dict[str, Any]:
    """Add a "To Label" sheet (documents with a missing label) and a "Text" sheet per class."""
    tl: dict[str, Any] = {"sheets": [], "text_mode": "sheet", "text_docs": 0, "text_chars": 0, "text_chunks": 0,
                          "text_truncated": 0, "controls_replaced": 0, "empty_texts": 0, "has_original": False,
                          "text_dir": ""}
    hub_files = None if hub_files is None else set(hub_files)
    plan = []
    for cls, title in CLASSES:
        if cls not in to_label or cls not in class_cols:
            continue
        docs: list[_Doc] = []
        for _, row in rows[rows.expected == cls].iterrows():
            gt = json.loads(row.gt_fields) if isinstance(row.gt_fields, str) else dict(row.gt_fields)
            presence = _json(gt.get("gt_presence")) or {}
            subclass = str(row.expected_subclass)
            cells = []
            for key, header, ftype in class_cols[cls]:
                value, status = cell_for(cls, key, gt, presence, subclass)
                flags = ()
                if ftype.startswith("date"):
                    flags = tuple(date_flag(sp, status, parts_fn)[0] for sp in (value.split(" || ") if value else [""]))
                cells.append((key, header, ftype, value, status, flags))
            need = [i for i, c in enumerate(cells) if c[4] in MISSING or any(f in DATE_PROBLEMS for f in c[5])]
            if need:
                meta = row.metadata if "metadata" in row.index else None
                docs.append(_Doc(row, subclass, cells, need, original_link(meta, dataset, commit, hub_files)))
        docs.sort(key=lambda d: (d.subclass, str(d.row.document_id)))
        if docs:
            plan.append((cls, title, docs))
    if not plan:
        return tl

    def text_of(doc: _Doc) -> str:
        t = doc.row.doc_text
        return t if isinstance(t, str) else ""

    est = sum(len(text_of(d).encode("utf-8")) for _, _, docs in plan for d in docs)
    external = est > max_mb * 1_000_000
    if external:
        tl["text_mode"] = "files"
        tl["text_dir"] = f"{out.stem}_texts"
    tl["text_docs"] = sum(len(docs) for _, _, docs in plan)

    for cls, title, docs in plan:
        tl_name, tx_name = f"To Label – {title}", f"Text – {title}"
        chunked = []
        text_row = 2
        for d in docs:
            raw = text_of(d)
            if not raw:
                tl["empty_texts"] += 1
            safe, replaced = sanitize_text(raw)
            chunks = chunk_text(safe)
            if "".join(chunks) != safe or any(_utf16_len(c) > EXCEL_CELL_MAX for c in chunks):
                tl["text_truncated"] += 1
            tl["text_chars"] += len(raw)
            tl["text_chunks"] += len(chunks)
            tl["controls_replaced"] += replaced
            chunked.append((raw, chunks, text_row))
            text_row += 1 + len(chunks)

        used = sorted({i for d in docs for i in d.need})
        has_original = any(d.link for d in docs)
        tl["has_original"] = tl["has_original"] or has_original
        ws = wb.create_sheet(tl_name)
        ws.sheet_properties.tabColor = "FFC000"
        heads = [("document_id", 22), ("split", 7), ("subclass", 20), ("filename", 32)]
        if has_original:
            heads.append(("Open original", 14))
        heads += [("Open text", 11), ("fields needing labels", 12)]
        for i, (name, width) in enumerate(heads, start=1):
            _header(ws, i, name, HEAD_ID)
            ws.column_dimensions[get_column_letter(i)].width = width
        count_col = len(heads)
        c_open = count_col - 1
        first = count_col + 1
        cols = class_cols[cls]
        for j, idx in enumerate(used):
            vc = first + 2 * j
            _, header, ftype = cols[idx]
            _header(ws, vc, header, HEAD_VAL, f"Scored field type: {ftype}. Edit the value here.")
            _header(ws, vc + 1, f"{header} | status", HEAD_ST, "Set it to 'populated' when you add a value.")
            ws.column_dimensions[get_column_letter(vc)].width = 36
            ws.column_dimensions[get_column_letter(vc + 1)].width = 16
        last = first + 2 * len(used) - 1
        live, flag_col = last + 1, last + 2
        _header(ws, live, "still missing (live)", HEAD_ID,
                "Formula: this row's fields whose status is UNLABELED or pending_annotation.")
        _header(ws, flag_col, "date flags", HEAD_ID, "Date labels that lack a year, a day/month, or do not parse.")
        ws.column_dimensions[get_column_letter(live)].width = 12
        ws.column_dimensions[get_column_letter(flag_col)].width = 40
        if has_original:
            ws.cell(row=1, column=5).comment = Comment(
                "Source document. Corporate records link to the SEC EDGAR exhibit named in the dataset "
                "metadata (exhibit_url).", "gt-label-workbook")
        dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=False)
        ws.add_data_validation(dv)
        for r, (d, (raw, _, trow)) in enumerate(zip(docs, chunked), start=2):
            for i, v in enumerate([d.row.document_id, d.row.split, d.subclass, d.row.filename], start=1):
                c = ws.cell(row=r, column=i)
                _set_str(c, str(v))
                c.font, c.fill = FONT, IDFILL
            if d.link:
                _url(ws.cell(row=r, column=5), *d.link)
            c = ws.cell(row=r, column=c_open)
            if external:
                rel = f"{tl['text_dir']}/{cls}/{quote(str(d.row.document_id))}.txt"
                c.value = "open text"
                c.hyperlink = rel
                c.font = LINK
            else:
                _goto(c, tx_name, f"A{trow}", "open text")
            ws.cell(row=r, column=count_col, value=len(d.need)).font = BOLD
            for j, idx in enumerate(used):
                _, header, _, value, status, _ = d.cells[idx]
                vcell = ws.cell(row=r, column=first + 2 * j)
                _set_str(vcell, clip(value, f"{tl_name}:{d.row.document_id}:{header}"))
                scell = ws.cell(row=r, column=first + 2 * j + 1, value=status)
                vcell.font = scell.font = FONT
                vcell.alignment = scell.alignment = Alignment(wrap_text=True, vertical="top")
                if idx in d.need:
                    vcell.fill = TODO
            rng = f"{get_column_letter(first)}{r}:{get_column_letter(last)}{r}"
            ws.cell(row=r, column=live,
                    value=f'=COUNTIF({rng},"UNLABELED")+COUNTIF({rng},"pending_annotation")').font = BOLD
            problems = dict.fromkeys(f"{d.cells[i][1]}: {f}" for i in d.need for f in d.cells[i][5] if f in DATE_PROBLEMS)
            fc = ws.cell(row=r, column=flag_col)
            _set_str(fc, "; ".join(problems))
            fc.font, fc.alignment = FONT, Alignment(wrap_text=True, vertical="top")
        for j in range(len(used)):
            col = get_column_letter(first + 2 * j + 1)
            dv.add(f"{col}2:{col}{len(docs) + 1}")
        ws.freeze_panes = ws.cell(row=2, column=5)
        ws.auto_filter.ref = f"A1:{get_column_letter(flag_col)}{len(docs) + 1}"
        ws.row_dimensions[1].height = 45
        tl["sheets"].append((tl_name, len(docs), get_column_letter(count_col), get_column_letter(live)))

        if external:
            folder = out.parent / tl["text_dir"] / cls
            folder.mkdir(parents=True, exist_ok=True)
            for d, (raw, _, _) in zip(docs, chunked):
                (folder / f"{d.row.document_id}.txt").write_bytes(raw.encode("utf-8"))
            continue
        tx = wb.create_sheet(tx_name)
        tx.sheet_properties.tabColor = "A6A6A6"
        for i, (name, width) in enumerate((("document_id", 22), ("part", 18), ("text", 130)), start=1):
            _header(tx, i, name, HEAD_ID)
            tx.column_dimensions[get_column_letter(i)].width = width
        wrap = Alignment(wrap_text=True, vertical="top")
        for k, (d, (raw, chunks, trow)) in enumerate(zip(docs, chunked)):
            doc_id = str(d.row.document_id)
            a = tx.cell(row=trow, column=1)
            _set_str(a, doc_id)
            _goto(tx.cell(row=trow, column=2), tl_name, f"A{k + 2}", "back to To Label")
            info = tx.cell(row=trow, column=3)
            _set_str(info, f"{d.row.filename}  |  {d.subclass}  |  {d.row.split}  |  {len(raw):,} characters"
                           f"  |  {len(chunks)} part{'s' if len(chunks) != 1 else ''}")
            a.font = info.font = BOLD
            for c in (a, tx.cell(row=trow, column=2), info):
                c.fill = HEAD_TXT
            tx.cell(row=trow, column=2).font = LINK
            for p, chunk in enumerate(chunks, start=1):
                g = tx.cell(row=trow + p, column=1)
                _set_str(g, doc_id)
                g.font = GREY
                n = tx.cell(row=trow + p, column=2, value=f"{p}/{len(chunks)}")
                n.font = GREY
                n.alignment = wrap
                t = tx.cell(row=trow + p, column=3)
                _set_str(t, chunk)
                t.font, t.alignment = FONT, wrap
        tx.freeze_panes = "A2"
        tx.auto_filter.ref = f"A1:C{trow + len(chunks) + 1}"

    for pos, (name, *_rest) in enumerate(tl["sheets"], start=2):
        wb.move_sheet(name, offset=pos - wb.sheetnames.index(name))
    return tl


def build_workbook(rows: pd.DataFrame, out: Path, *, dataset: str, revision: str,
                   resolved: str | None = None, to_label: Iterable[str] = (),
                   hub_files: Iterable[str] | None = None,
                   max_workbook_mb: float = MAX_WORKBOOK_MB) -> dict[str, Any]:
    """Write the workbook for ``rows`` (the ground_truth config) to ``out``.

    ``to_label`` names the classes that also get a "To Label" and a "Text" sheet; ``rows``
    then needs a ``doc_text`` column (and may carry ``metadata`` for the original-document
    links). ``hub_files`` lists the files the dataset repo holds at ``resolved``.
    """
    parts_fn, parts_source = _date_parts_impl()
    rows = rows.reset_index(drop=True)
    to_label = tuple(to_label)
    unknown = sorted(set(to_label) - {c for c, _ in CLASSES})
    if unknown:
        raise ValueError(f"unknown class in to_label: {unknown}; choose from {[c for c, _ in CLASSES]}")
    if to_label and "doc_text" not in rows.columns:
        raise ValueError("to_label needs a doc_text column (the 'default' config joined on filename)")
    truncated: list[str] = []

    def clip(text: str, where: str) -> str:
        text = sanitize_text(text)[0]
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
    class_cols: dict[str, list[tuple[str, str, str]]] = {}

    for cls, title in CLASSES:
        sub = rows[rows.expected == cls].sort_values(["split", "expected_subclass", "filename"])
        if sub.empty:
            continue
        ws = wb.create_sheet(title)
        cols = class_cols[cls] = class_columns(cls, sub)
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
                vcell = ws.cell(row=r, column=first + 2 * j)
                _set_str(vcell, clip(value, f"{title}:{row.document_id}:{header}"))
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

    tl = _add_to_label(wb, rows, class_cols, to_label, parts_fn=parts_fn, dataset=dataset, commit=resolved,
                       hub_files=hub_files, out=out, max_mb=max_workbook_mb, clip=clip)

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
    if tl["sheets"]:
        k0 = tot + 4 + len(DATE_FLAGS)
        summary.cell(row=k0, column=1, value="To Label sheets").font = BOLD
        for i, h in enumerate(("Documents to label", "Fields needing labels (as built)", "Still missing (live)"), start=2):
            c = summary.cell(row=k0, column=i, value=h)
            c.font, c.fill = HEAD, HEAD_ID
        for k, (name, n, count_col, live_col) in enumerate(tl["sheets"], start=k0 + 1):
            q = f"'{name}'"
            summary.cell(row=k, column=1, value=name).font = FONT
            summary.cell(row=k, column=2, value=f"=COUNTA({q}!A2:A{n + 1})").font = FONT
            summary.cell(row=k, column=3, value=f"=SUM({q}!{count_col}2:{count_col}{n + 1})").font = FONT
            summary.cell(row=k, column=4, value=f"=SUM({q}!{live_col}2:{live_col}{n + 1})").font = FONT

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
        *_to_label_readme(tl, truncated_texts=tl["text_truncated"]),
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
            "date_parts": parts_source, "sheets": [s[0] for s in sheets],
            "to_label": {name: n for name, n, _, _ in tl["sheets"]},
            "text_mode": tl["text_mode"], "text_docs": tl["text_docs"], "text_chars": tl["text_chars"],
            "text_chunks": tl["text_chunks"], "text_truncated": tl["text_truncated"],
            "text_controls_replaced": tl["controls_replaced"], "empty_texts": tl["empty_texts"],
            "all_sheets": wb.sheetnames}


# ---------------------------------------------------------------------------
# CLI


def hub_listing(dataset: str, revision: str) -> tuple[str | None, list[str] | None]:
    """(commit sha, repo file paths) of ``dataset`` at ``revision``; (None, None) if unreachable."""
    from pipeline.hf_corpus_loader import HUB_BASE, _http_get_json

    try:
        info = _http_get_json(f"{HUB_BASE}/api/datasets/{dataset}/revision/{quote(revision, safe='')}")
        return info.get("sha"), [x["rfilename"] for x in info.get("siblings", [])]
    except Exception as exc:  # read-only lookup; the workbook is still built without it
        print(f"warning: could not list {dataset}@{revision}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None, None


def load_rows(parquet_dir: Path | None, revision: str, *, with_text: bool = False
              ) -> tuple[pd.DataFrame, str | None, str]:
    """The ground_truth rows (train + test); ``with_text`` adds ``doc_text`` and ``metadata``
    from the ``default`` config, joined on ``filename``."""
    from pipeline.hf_corpora import FULL_CORPUS_ID

    def text_frame(split: str, ground_truth: pd.DataFrame) -> pd.DataFrame:
        if parquet_dir is not None:
            path = parquet_dir / f"default_{split}.parquet"
            if not path.exists():
                raise SystemExit(f"{path} not found: --to-label needs the default config's text; "
                                 "add default_train.parquet / default_test.parquet or pass --to-label none")
            blind = pd.read_parquet(path)
        else:
            from pipeline.hf_corpus_loader import load_config_frame

            blind, _ = load_config_frame(FULL_CORPUS_ID, "default", split=split, revision=revision)
        keep = [c for c in ("filename", "doc_text", "metadata") if c in blind.columns]
        return ground_truth.merge(blind[keep], on="filename", how="left", validate="one_to_one")

    frames, resolved = [], None
    for split in ("train", "test"):
        if parquet_dir is not None:
            frame = pd.read_parquet(parquet_dir / f"gt_{split}.parquet")
        else:
            from pipeline.hf_corpus_loader import load_config_frame

            frame, provenance = load_config_frame(FULL_CORPUS_ID, "ground_truth", split=split, revision=revision)
            resolved = provenance.get("revision_resolved") or resolved
            if "split" not in frame.columns:
                frame = frame.assign(split=split)
        frames.append(text_frame(split, frame) if with_text else frame)
    return pd.concat(frames, ignore_index=True), resolved, FULL_CORPUS_ID


def parse_classes(value: str) -> tuple[str, ...]:
    known = [c for c, _ in CLASSES]
    wanted = [] if value.strip().lower() in ("", "none") else [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in wanted if v not in known]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown class {bad}; choose from {known} or 'none'")
    return tuple(dict.fromkeys(wanted))


def main(argv: list[str] | None = None) -> int:
    from pipeline.hf_corpora import FULL_CORPUS_REVISION

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="workbook path (.xlsx)")
    ap.add_argument("--revision", default=FULL_CORPUS_REVISION,
                    help=f"corpus revision (default: the pipeline pin, {FULL_CORPUS_REVISION})")
    ap.add_argument("--parquet-dir", type=Path, default=None,
                    help="offline: a directory with gt_train.parquet and gt_test.parquet "
                         "(plus default_train.parquet and default_test.parquet for --to-label)")
    ap.add_argument("--to-label", type=parse_classes, default=TO_LABEL_DEFAULT, metavar="CLASSES",
                    help="comma-separated classes that get a 'To Label' and a 'Text' sheet, or 'none' "
                         f"(default: {','.join(TO_LABEL_DEFAULT)})")
    ap.add_argument("--max-workbook-mb", type=float, default=MAX_WORKBOOK_MB,
                    help="if the document texts exceed this, write them as .txt files beside the workbook "
                         f"instead of 'Text' sheets (default: {MAX_WORKBOOK_MB:g})")
    args = ap.parse_args(argv)
    rows, resolved, dataset = load_rows(args.parquet_dir, args.revision, with_text=bool(args.to_label))
    hub_files = None
    if args.to_label and args.parquet_dir is None:
        sha, hub_files = hub_listing(dataset, args.revision)
        resolved = resolved or sha
    stats = build_workbook(rows, args.out, dataset=dataset, revision=args.revision, resolved=resolved,
                           to_label=args.to_label, hub_files=hub_files, max_workbook_mb=args.max_workbook_mb)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
