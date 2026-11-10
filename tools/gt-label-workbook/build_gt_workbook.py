"""Build an editable ground-truth label workbook (.xlsx) from a pinned corpus revision.

Every field the Dojo scores gets a value column and a status column, per document
class, whether or not the dataset labels it, so missing labels are visible and can be
filled in. A "Date Audit" sheet lists every date label with the components it
actually states (year / month / day); a "Summary" sheet counts coverage with live
formulas. Status, subclass and every closed-vocabulary field are drop-downs (Excel list
validation, shown as chips by Google Sheets) fed from a hidden "Lists" sheet; a label a
person adds turns green.

Usage (from the repository root):

    PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py \
        --revision v9.3 --out ~/gt_label_workbook.xlsx   # outside the repo; *.xlsx is git-ignored

``--revision`` (a tag, branch or commit; default ``FULL_CORPUS_REVISION``) is resolved to a
commit sha through the Hub API (read-only) and the parquet files are downloaded from
``resolve/<sha>/parquet/<config>/<split>/``, so the workbook is built from exactly that
commit. A revision that does not resolve is an error. The sha is recorded on the README
sheet. Pass ``--parquet-dir DIR`` holding ``gt_train.parquet`` / ``gt_test.parquet`` to
build offline (the commit is then not resolved). Nothing is written to Hugging Face.

``--to-label`` (default: insurance_claim, corporate_record, correspondence) adds, per
class, a "To Label" sheet (one row per document with a missing label, linked to its
original and to its full text) and a "Text" sheet holding the full document text from
the ``default`` config, split over rows of at most 32,000 characters so nothing is cut.
Offline, ``--parquet-dir`` then also needs ``default_train.parquet`` / ``default_test.parquet``.

Column lists and vocabularies come from the Dojo (``extraction_schema``, its enums, the
corporate-record subclass entity fields, the CUAD category -> field map), so the workbook
follows the scorer when the Dojo pin moves.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple
from urllib.parse import quote

import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.hyperlink import Hyperlink

from llm_dojo_scoring.schemas.corporate_record_schema import (
    CORE_FIELDS,
    DEFAULT_FIELD_TYPES,
    ENTITY_CORE_FIELDS,
    RECORD_TYPES,
    entity_field_types,
    subclass_schema,
)
from llm_dojo_scoring.schemas.corpus import cms_source_table, normalize_corpus_subclass
from llm_dojo_scoring.schemas.cuad_schema import CUAD_CATEGORIES
from llm_dojo_scoring.schemas.extraction_schemas import extraction_schema, extraction_subtypes

CLASSES: tuple[tuple[str, str], ...] = (
    ("contract", "Contracts"),
    ("merger_agreement", "Merger Agreements"),
    ("corporate_record", "Corporate Records"),
    ("correspondence", "Correspondence"),
    ("insurance_claim", "Insurance Claims"),
)
# "verified" is the one status that is not a dataset code: a person sets it after checking or
# correcting a value (the label then counts as populated). The others are the dataset's codes.
STATUSES = ("populated", "verified", "pending_annotation", "schema_documented_absence", "not_applicable", "UNLABELED")
MISSING = ("UNLABELED", "pending_annotation")
CLOSING = ("verified", "schema_documented_absence", "not_applicable")  # a person sets these to close a gap
DATE_FLAGS = ("MISSING LABEL", "NO YEAR", "NO DAY/MONTH", "UNPARSEABLE", "OK")
CELL_MAX = 32000  # Excel holds 32,767 characters per cell
EXCEL_CELL_MAX = 32767
TEXT_CHUNK = 32000  # document text is split across rows of at most this many characters
DATE_PROBLEMS = ("NO YEAR", "NO DAY/MONTH", "UNPARSEABLE")
TO_LABEL_DEFAULT: tuple[str, ...] = ("insurance_claim", "corporate_record", "correspondence")
MAX_WORKBOOK_MB = 150.0  # above this, document texts go to .txt files beside the workbook
SEC_BASE = "https://www.sec.gov"
HUB_BASE = "https://huggingface.co"
ROW_HEIGHT = 30  # data rows: two lines of wrapped text; click a cell to read the rest
SLACK = 200  # spare rows included in summary ranges, so rows appended below the data still count

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
HEAD = Font(name="Arial", size=10, bold=True, color="FFFFFF")
TITLE = Font(name="Arial", size=14, bold=True)
SECTION = Font(name="Arial", size=11, bold=True, color="1F3864")
LINK = Font(name="Arial", size=10, color="0563C1", underline="single")
GREY = Font(name="Arial", size=9, color="808080")
HEAD_TXT = PatternFill("solid", fgColor="D9E1F2")
HEAD_ID = PatternFill("solid", fgColor="44546A")
HEAD_LINK = PatternFill("solid", fgColor="2E75B6")
HEAD_VAL = PatternFill("solid", fgColor="2F5597")
HEAD_ST = PatternFill("solid", fgColor="7F7F7F")
HEAD_HELP = PatternFill("solid", fgColor="A6A6A6")
TODO = PatternFill("solid", fgColor="FFE699")  # a label is missing
DONE_RGB = "A9D08E"  # a label a person added or verified
DONE = PatternFill("solid", fgColor=DONE_RGB)
DONE_CF = PatternFill(start_color=DONE_RGB, end_color=DONE_RGB, fill_type="solid")
IDFILL = PatternFill("solid", fgColor="F2F2F2")
_THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
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


def _derived_fields() -> dict[str, str]:
    """doc type -> the scored field the Dojo derives from the subclass (so it is never hand-labelled)."""
    try:
        from llm_dojo_scoring.schemas.extraction_schemas import _SUBTYPE_DERIVED

        derived = dict(_SUBTYPE_DERIVED)
    except ImportError:  # pragma: no cover - the Dojo renamed its private table
        derived = {"contract": "cuad_family", "merger_agreement": "merger_consideration",
                   "correspondence": "communication_type"}
    derived.pop("corporate_record", None)  # record_type is labelled in the data, with its own status
    return derived


_DERIVED = _derived_fields()


def carried_fields(sub: pd.DataFrame) -> set[str]:
    """Fields the dataset's ground truth carries for these rows: a non-empty value or a gt_presence key."""
    carried: set[str] = set()
    for raw in sub.gt_fields:
        gt = json.loads(raw) if isinstance(raw, str) else dict(raw)
        carried.update(k for k, v in gt.items() if not is_empty(v))
        carried.update((_json(gt.get("gt_presence")) or {}).keys())
    return carried


#: Schema fields left out of a class's columns because the pinned dataset never carries them
#: (filled in by ``class_columns``; reported in the README and the build summary).
DROPPED_FIELDS: dict[str, list[str]] = {}


def class_columns(cls: str, sub: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(column key, header, field type) for every field the Dojo scores for ``cls`` that the dataset labels.

    A pipeline extraction field the dataset's ground truth never carries for this class (no
    value and no gt_presence key on any row, e.g. insurance ``claim_checklist``) is not a
    labelling target and gets no column. Corporate records keep the Dojo's per-subclass entity
    contract (``unlabeled_fields``); fields derived from CUAD, MAUD or the subclass are kept.
    """
    types = dict(extraction_schema(cls).field_types)
    if cls != "corporate_record":
        carried = carried_fields(sub)
        keep_always = {_DERIVED.get(cls), "cms_source_table", "cuad_clauses", "maud_clauses",
                       *(_CUAD_FIELD if cls == "contract" else ())}
        dropped = [n for n in types if n not in carried and n not in keep_always]
        DROPPED_FIELDS[cls] = dropped
        types = {n: t for n, t in types.items() if n not in dropped}
    if cls == "corporate_record":
        subclasses = sorted(sub.expected_subclass.astype(str).unique())
        # filing_number is a core entity field the base schema omits: list it when any subclass
        # present carries it, or when any row has a status or value for it.
        applicable = any("filing_number" in subclass_schema(s).core_applicable for s in subclasses)
        if not applicable:
            for raw in sub.gt_fields:
                gt = json.loads(raw) if isinstance(raw, str) else dict(raw)
                status = (_json(gt.get("gt_presence")) or {}).get("filing_number")
                if not is_empty(gt.get("filing_number")) or status not in (None, "not_applicable"):
                    applicable = True
                    break
        if applicable:
            types["filing_number"] = DEFAULT_FIELD_TYPES["corporate_record"]["filing_number"]
        types = {**{f: types[f] for f in CORE_FIELDS if f in types}, **{k: v for k, v in types.items() if k not in CORE_FIELDS}}
        for subclass in subclasses:
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
        keys = sorted({k for v in sub.gt_fields
                       for k in (_json((_json(v) or {}).get("maud_clause_labels")) or {})})
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
        if key not in entity_field_types(subclass) and key not in ENTITY_CORE_FIELDS:
            return "", "not_applicable"
    if cls == "insurance_claim" and key == "cms_source_table":
        table = cms_source_table(cls, subclass)
        return (table, "populated") if table else ("", "not_applicable")
    if _DERIVED.get(cls) == key and is_empty(gt.get(key)) and presence.get(key) not in (
            "not_applicable", "schema_documented_absence", "pending_annotation"):
        # the Dojo derives this field from the subclass: it is never a labelling gap
        return normalize_corpus_subclass(cls, subclass), "populated"
    value, status = gt.get(key), presence.get(key)
    if status in ("not_applicable", "schema_documented_absence", "pending_annotation"):
        return render(value), status
    return ("", "UNLABELED") if is_empty(value) else (render(value), "populated")


def field_vocab(cls: str) -> dict[str, tuple[str, ...]]:
    """field -> its closed vocabulary, read from the Dojo's JSON schema enums (null excluded)."""
    props = extraction_schema(cls).json_schema()["properties"]
    vocab = {name: tuple(v for v in spec["enum"] if v is not None) for name, spec in props.items() if spec.get("enum")}
    if cls == "corporate_record":
        vocab.setdefault("record_type", tuple(RECORD_TYPES))
    return vocab


def subclass_choices(cls: str, present: Iterable[str]) -> tuple[str, ...]:
    """The Dojo's subclasses for ``cls``, then any other value the dataset carries (so every cell is valid)."""
    return tuple(dict.fromkeys([*extraction_subtypes(cls), *sorted({str(p) for p in present})]))


# ---------------------------------------------------------------------------
# Document text and links (To Label / Text sheets)

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NONCHAR = re.compile("[\ud800-\udfff￾￿]")  # lone surrogates and the two XML non-characters
_SURROGATE = re.compile("[\ud800-\udfff]")


def sanitize_text(text: str) -> tuple[str, int]:
    """``text`` made safe for an XLSX cell, and how many characters were replaced.

    XML 1.0 cannot hold most control characters, so each one becomes its Unicode
    "control picture" (U+2400 + code point, e.g. U+0001 -> U+2401). Lone surrogates and the
    non-characters U+FFFE / U+FFFF become U+FFFD. The replacement is one-for-one: the length
    is unchanged and nothing is dropped or cut.
    """
    text, controls = _CONTROL.subn(lambda m: chr(0x2400 + ord(m.group())), text)
    text, others = _NONCHAR.subn("�", text)
    return text, controls + others


def text_file_name(doc_id: str) -> str:
    """A file name for ``doc_id`` that cannot contain a path separator (``quote(..., safe="")``)."""
    return (quote(str(doc_id), safe="") or "_") + ".txt"


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
    cell.value = sanitize_text(value)[0]
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
# Lists, drop-downs, highlights, formulas


class _Lists:
    """Drop-down vocabularies: one column each on a hidden "Lists" sheet, one named range each."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, ...]] = {}

    @staticmethod
    def name(*parts: str) -> str:
        return "lst_" + re.sub(r"[^A-Za-z0-9_]", "_", "_".join(parts))

    def add(self, name: str, values: Iterable[str]) -> str:
        values = tuple(dict.fromkeys(str(v) for v in values))
        if not values:
            raise ValueError(f"list {name} is empty")
        if self.items.setdefault(name, values) != values:
            raise ValueError(f"list {name} defined twice with different values")
        return name

    def write(self, wb: Workbook) -> None:
        if not self.items:
            return
        ws = wb.create_sheet("Lists")
        for i, (name, values) in enumerate(self.items.items(), start=1):
            letter = get_column_letter(i)
            ws.cell(row=1, column=i, value=name).font = BOLD
            ws.column_dimensions[letter].width = max(16, len(name) + 2)
            for r, v in enumerate(values, start=2):
                ws.cell(row=r, column=i, value=v).font = FONT
            wb.defined_names[name] = DefinedName(name, attr_text=f"Lists!${letter}$2:${letter}${len(values) + 1}")
        ws.sheet_state = "hidden"


def _validation(list_name: str, *, blank: bool, title: str, message: str) -> DataValidation:
    return DataValidation(type="list", formula1=list_name, allow_blank=blank, showErrorMessage=True,
                          errorStyle="stop", errorTitle=title, error=message)


def _add_ranges(ws, dv: DataValidation, ranges: list[str]) -> None:
    """Attach ``dv`` to ``ranges`` (long sqref lists are split over several validations)."""
    for i in range(0, len(ranges), 20):
        part = ranges[i:i + 20]
        piece = dv if i == 0 else _validation(dv.formula1, blank=bool(dv.allow_blank), title=dv.errorTitle,
                                              message=dv.error)
        for rng in part:
            piece.add(rng)
        ws.add_data_validation(piece)


def _green_formula(j: int, v: str, s: str, m: str) -> str:
    """CF formula (first data row 2) for field ``j`` (1-based) whose value is column ``v``, status ``s``, mask ``m``.

    Green when the status is 'verified', or when the dataset had a gap (mask '1' = missing,
    '2' = date problem) and a person has closed it: a documented absence / not applicable, or
    a populated value for a plain gap. A date problem needs 'verified', a documented absence or
    not applicable, because 'populated' was already the status.
    """
    ch = f"MID(${m}2,{j},1)"
    return (f'OR(${s}2="verified",AND(OR({ch}="1",{ch}="2"),OR(${s}2="schema_documented_absence",'
            f'${s}2="not_applicable",AND({ch}="1",${s}2="populated",LEN(${v}2)>0))))')


def _status_cells(first: str, last: str, r: int) -> str:
    """Array of status cells in row ``r`` of a value/status block ``first``..``last``: a SUMPRODUCT mask term."""
    return f"(MOD(COLUMN({first}{r}:{last}{r})-COLUMN({first}{r}),2)=1)"


def _missing_formula(first: str, last: str, r: int, date_status_cols: Iterable[str] = ()) -> str:
    """Live count of this row's fields still missing: status UNLABELED / pending_annotation, plus a date
    problem found at build time whose status is still 'populated' (set it to 'verified' after fixing)."""
    blk = f"{first}{r}:{last}{r}"
    f = f'=SUMPRODUCT({_status_cells(first, last, r)}*(({blk}="UNLABELED")+({blk}="pending_annotation")))'
    for col in date_status_cols:
        f += f'+IF({col}{r}="populated",1,0)'
    return f


def _decorate(ws, lists: _Lists, *, cls: str, fields: list[tuple[str, str, str]], first: int, n_rows: int,
              sub_col: int, orig_col: int, mask_col: int, sub_values: tuple[str, ...]) -> None:
    """Drop-downs (status, subclass, closed vocabularies) and the green highlight for human labels."""
    last_row = n_rows + 1
    if n_rows < 1:
        return
    vocab = field_vocab(cls)
    status_ranges: list[str] = []
    enum_ranges: dict[str, list[str]] = {}
    m = get_column_letter(mask_col)
    for j, (key, _, _) in enumerate(fields):
        v, s = get_column_letter(first + 2 * j), get_column_letter(first + 2 * j + 1)
        status_ranges.append(f"{s}2:{s}{last_row}")
        if key in vocab:
            enum_ranges.setdefault(key, []).append(f"{v}2:{v}{last_row}")
        ws.conditional_formatting.add(
            f"{v}2:{s}{last_row}", FormulaRule(formula=[_green_formula(j + 1, v, s, m)], fill=DONE_CF))
    _add_ranges(ws, _validation(lists.add("lst_status", STATUSES), blank=False, title="Status",
                                message="Pick a status from the list."), status_ranges)
    for key, ranges in enum_ranges.items():
        name = lists.add(_Lists.name("enum", cls, key), vocab[key])
        _add_ranges(ws, _validation(name, blank=True, title=key, message=f"Pick a {key} from the list."), ranges)
    sc, oc = get_column_letter(sub_col), get_column_letter(orig_col)
    name = lists.add(_Lists.name("subclass", cls), sub_values)
    _add_ranges(ws, _validation(name, blank=False, title="Subclass", message="Pick a subclass from the list."),
                [f"{sc}2:{sc}{last_row}"])
    ws.conditional_formatting.add(f"{sc}2:{sc}{last_row}", FormulaRule(formula=[f"${sc}2<>${oc}2"], fill=DONE_CF))


def _group_hidden(ws, first: int, last: int) -> None:
    """Group (outline) and hide helper columns; the + above the sheet's right edge shows them."""
    ws.column_dimensions.group(get_column_letter(first), get_column_letter(last), outline_level=1, hidden=True)


# ---------------------------------------------------------------------------
# Workbook


def _header(ws, col: int, text: str, fill, note: str | None = None, width: float | None = None) -> None:
    cell = ws.cell(row=1, column=col, value=text)
    cell.font, cell.fill = HEAD, fill
    cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    cell.border = BORDER
    if note:
        cell.comment = Comment(note, "gt-label-workbook")
    if width is not None:
        ws.column_dimensions[get_column_letter(col)].width = width


def _value_width(ftype: str) -> int:
    base = ftype.split(":")[0].split(" ")[0]
    return {"date": 17, "id": 20, "money": 14, "label": 22, "name": 28, "free_text": 44,
            "entity_list": 36}.get(base, 30)


def _data_cell(ws, row: int, col: int, value: Any = None, *, fill=None, font=FONT, wrap: bool = True):
    c = ws.cell(row=row, column=col, value=value)
    c.font, c.border = font, BORDER
    c.alignment = WRAP_TOP if wrap else Alignment(vertical="top")
    if fill is not None:
        c.fill = fill
    return c


def _to_label_readme(tl: dict[str, Any], *, truncated_texts: int) -> list[tuple[str, str]]:
    if not tl["sheets"]:
        return []
    names = ", ".join(f"'{n}'" for n, *_ in tl["sheets"])
    lines = [
        ("TO LABEL SHEETS", "h"),
        (f"{names} list only the documents that still need a label, one document per row, sorted by "
         "subclass then document_id. A document is listed when a field is UNLABELED or pending_annotation, "
         "or a date label is flagged NO YEAR, NO DAY/MONTH or UNPARSEABLE (see 'Date Audit'). Only fields "
         "that need a label on at least one row get a column pair (value, then status). Column order: "
         "document_id, filename, split, links, subclass, then value + status per field.", "p"),
        ("DATES MUST INCLUDE THE YEAR whenever the document states one, e.g. 'August 18, 2014', never "
         "'18th day of August' or 'August 18'. 'date flags' names the fields with a date problem. After you "
         "correct a flagged date (or decide the field is not applicable), set its status to 'verified' (or "
         "'not_applicable'): a flagged date still counts as missing while its status is 'populated'.", "p"),
        ("'still missing (live)' counts this row's fields whose status is UNLABELED or pending_annotation, plus "
         "flagged dates still 'populated'. At build time it equals the hidden column 'fields needing labels "
         "(as built)'; it falls as you fill and verify. Click the + above the right edge of the sheet to show "
         "the hidden helper columns.", "p"),
    ]
    if tl["has_original"]:
        lines.append(("'Open original' opens the source document: an SEC EDGAR exhibit for corporate records "
                      "(the link comes from the dataset's exhibit_url; sec.gov may ask a browser to confirm "
                      "it is not automated). Insurance and correspondence documents have no external source: "
                      "their source is the document text embedded in the dataset, reached by 'Open text'. The "
                      "dataset repo itself holds no original files, so there is no Hub file link.", "p"))
    else:
        lines.append(("The dataset repo holds no original files and its metadata gives no source URL for these "
                      "classes, so there is no 'Open original' column; the source is the embedded document "
                      "text, reached by 'Open text'.", "p"))
    if tl["text_mode"] == "sheet":
        lines.append((f"'Open text' jumps to the document's full text in the 'Text – ...' sheets. The text is never "
                      f"cut: texts longer than {TEXT_CHUNK:,} characters continue on the following rows (parts 1/n, "
                      "2/n, ...), split at blank lines or line breaks where possible. Control characters and "
                      "other characters XLSX cannot store are shown as a symbol (control pictures such as U+2401; "
                      "U+FFFD for non-characters). In a text sheet, 'back to To Label' returns to the document's "
                      f"row. Texts cut in this build: {truncated_texts}. FILTER the To Label and Text sheets, "
                      "do not SORT them: the open text and back links point at fixed rows and break if rows move.",
                      "p"))
    else:
        lines.append((f"'Open text' opens the document's full text as a .txt file in the '{tl['text_dir']}' folder "
                      "beside this workbook (the texts exceeded the size limit for sheets, so there are no "
                      "'Text' sheets). The files hold the exact original text, uncut; keep the folder next to the "
                      "workbook or the links break. File names are the document_id, percent-encoded so they cannot "
                      f"leave the folder. Texts cut in this build: {truncated_texts}. Filter the To Label sheets, "
                      "do not sort them.", "p"))
    if tl["empty_texts"]:
        lines.append((f"{tl['empty_texts']} listed document(s) have no text in the dataset's 'default' config "
                      "(no row with the same filename), so their text cell is empty; label them from the "
                      "original document.", "p"))
    lines.append(("", "p"))
    return lines


def _add_to_label(wb: Workbook, rows: pd.DataFrame, class_cols: dict, to_label: tuple[str, ...], *,
                  parts_fn: Callable[[Any], Any], dataset: str, commit: str | None,
                  hub_files: Iterable[str] | None, out: Path, max_mb: float,
                  clip: Callable[[str, str], str], lists: _Lists) -> dict[str, Any]:
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
            need = [i for i, c in enumerate(cells) if c[4] in MISSING or _date_problem(c)]
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

    est = sum(len(text_of(d).encode("utf-8", errors="replace")) for _, _, docs in plan for d in docs)
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

        cols = class_cols[cls]
        used = sorted({i for d in docs for i in d.need})
        has_original = any(d.link for d in docs)
        tl["has_original"] = tl["has_original"] or has_original
        ws = wb.create_sheet(tl_name)
        ws.sheet_properties.tabColor = "FFC000"
        # identity columns, then links, then subclass, then a value + status pair per field
        heads = [("document_id", 22, HEAD_ID), ("filename", 30, HEAD_ID), ("split", 7, HEAD_ID)]
        if has_original:
            heads.append(("Open original", 14, HEAD_LINK))
        heads.append(("Open text", 11, HEAD_LINK))
        heads.append(("subclass", 20, HEAD_VAL))
        for i, (name, width, fill) in enumerate(heads, start=1):
            _header(ws, i, name, fill, width=width)
        c_open = len(heads) - 1
        c_sub = len(heads)
        first = len(heads) + 1
        for j, idx in enumerate(used):
            vc = first + 2 * j
            _, header, ftype = cols[idx]
            _header(ws, vc, header, HEAD_VAL, f"Scored field type: {ftype}. Edit the value here.", _value_width(ftype))
            _header(ws, vc + 1, f"{header} | status", HEAD_ST, "Set it to 'populated' when you add a value.", 18)
        last = first + 2 * len(used) - 1
        live, flag_col = last + 1, last + 2
        built, orig_col, mask_col = last + 3, last + 4, last + 5
        _header(ws, live, "still missing (live)", HEAD_ID,
                "Formula: this row's fields whose status is UNLABELED or pending_annotation, plus flagged dates "
                "still 'populated'.", 12)
        _header(ws, flag_col, "date flags", HEAD_ID, "Date labels that lack a year, a day/month, or do not parse.", 38)
        _header(ws, built, "fields needing labels (as built)", HEAD_HELP, "Helper: how many fields needed a label "
                "when the workbook was built.", 14)
        _header(ws, orig_col, "dataset subclass (helper)", HEAD_HELP, "Helper: the subclass the dataset carries; "
                "the subclass cell turns green when you change it.", 18)
        _header(ws, mask_col, "needs mask (helper)", HEAD_HELP, "Helper: one character per field pair, left to right: "
                "1 = the dataset had no label, 2 = date problem, 0 = nothing missing. Drives the green highlight.", 14)
        if has_original:
            ws.cell(row=1, column=4).comment = Comment(
                "Source document. Corporate records link to the SEC EDGAR exhibit named in the dataset "
                "metadata (exhibit_url).", "gt-label-workbook")
        n = len(docs)
        for r, (d, (raw, _, trow)) in enumerate(zip(docs, chunked), start=2):
            for i, v in enumerate([d.row.document_id, d.row.filename, d.row.split], start=1):
                _set_str(_data_cell(ws, r, i, fill=IDFILL, wrap=i == 2), str(v))
            if has_original:
                oc = _data_cell(ws, r, 4, wrap=False)
                if d.link:
                    _url(oc, *d.link)
            lc = _data_cell(ws, r, c_open, wrap=False)
            if external:
                disk = text_file_name(d.row.document_id)
                lc.value = "open text"
                lc.hyperlink = f"{tl['text_dir']}/{cls}/{quote(disk)}"
                lc.font = LINK
            else:
                _goto(lc, tx_name, f"A{trow}", "open text")
            _set_str(_data_cell(ws, r, c_sub, wrap=False), d.subclass)
            mask, date_cols = [], []
            for j, idx in enumerate(used):
                _, header, _, value, status, _ = d.cells[idx]
                vcell = _data_cell(ws, r, first + 2 * j)
                _set_str(vcell, clip(value, f"{tl_name}:{d.row.document_id}:{header}"))
                _data_cell(ws, r, first + 2 * j + 1, status)
                if _date_problem(d.cells[idx]):
                    mask.append("2")
                    date_cols.append(get_column_letter(first + 2 * j + 1))
                else:
                    mask.append("1" if status in MISSING else "0")
                if idx in d.need:
                    vcell.fill = TODO
            f, l_ = get_column_letter(first), get_column_letter(last)
            _data_cell(ws, r, live, _missing_formula(f, l_, r, date_cols), font=BOLD, wrap=False)
            problems = dict.fromkeys(f"{d.cells[i][1]}: {fl}" for i in d.need for fl in d.cells[i][5] if fl in DATE_PROBLEMS)
            _set_str(_data_cell(ws, r, flag_col), "; ".join(problems))
            _data_cell(ws, r, built, len(d.need), font=BOLD, wrap=False)
            _set_str(_data_cell(ws, r, orig_col, wrap=False), d.subclass)
            _set_str(_data_cell(ws, r, mask_col, wrap=False), "".join(mask))
            ws.row_dimensions[r].height = ROW_HEIGHT
        _decorate(ws, lists, cls=cls, fields=[cols[i] for i in used], first=first, n_rows=n, sub_col=c_sub,
                  orig_col=orig_col, mask_col=mask_col,
                  sub_values=subclass_choices(cls, rows.loc[rows.expected == cls, "expected_subclass"].astype(str)))
        _group_hidden(ws, built, mask_col)
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = f"A1:{get_column_letter(flag_col)}{n + 1}"
        ws.row_dimensions[1].height = 48
        tl["sheets"].append((tl_name, n, get_column_letter(live), get_column_letter(built)))

        if external:
            folder = out.parent / tl["text_dir"] / cls
            folder.mkdir(parents=True, exist_ok=True)
            for d, (raw, _, _) in zip(docs, chunked):
                target = folder / text_file_name(d.row.document_id)
                if target.resolve().parent != folder.resolve():  # cannot happen with quote(safe=""); fail loudly
                    raise ValueError(f"text file for {d.row.document_id!r} would leave {folder}")
                target.write_bytes(_SURROGATE.sub("�", raw).encode("utf-8"))
            continue
        tx = wb.create_sheet(tx_name)
        tx.sheet_properties.tabColor = "A6A6A6"
        for i, (name, width) in enumerate((("document_id", 22), ("part", 20), ("text", 130)), start=1):
            _header(tx, i, name, HEAD_ID, width=width)
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
                c.fill, c.border = HEAD_TXT, BORDER
            tx.cell(row=trow, column=2).font = LINK
            for p, chunk in enumerate(chunks, start=1):
                g = tx.cell(row=trow + p, column=1)
                _set_str(g, doc_id)
                g.font = GREY
                g.alignment = Alignment(vertical="top")
                n_ = tx.cell(row=trow + p, column=2, value=f"part {p}/{len(chunks)}")
                n_.font, n_.alignment = GREY, Alignment(vertical="top")
                t = tx.cell(row=trow + p, column=3)
                _set_str(t, chunk)
                t.font, t.alignment = FONT, WRAP_TOP
        tx.freeze_panes = "A2"
        tx.auto_filter.ref = f"A1:C{trow + len(chunks)}"
        tx.row_dimensions[1].height = 22

    for pos, (name, *_rest) in enumerate(tl["sheets"], start=2):
        wb.move_sheet(name, offset=pos - wb.sheetnames.index(name))
    return tl


def _date_problem(cell: tuple) -> bool:
    """A date label that needs work: a flagged problem while it is labelled or missing (a documented
    absence / not applicable status makes the value moot)."""
    return cell[4] in (*MISSING, "populated") and any(f in DATE_PROBLEMS for f in cell[5])


def _readme_rows(*, dataset: str, revision: str, resolved: str | None, n_rows: int, tl: dict[str, Any],
                 truncated: int) -> list[tuple[str, str]]:
    if resolved:
        pin = f"revision {revision} = commit {resolved}"
    else:
        pin = f"revision label {revision} (built from local parquet files; the commit was not resolved)"
    lines: list[tuple[str, str]] = [
        ("Mailroom ground-truth label workbook", "title"),
        ("", "p"),
        (f"Source: Hugging Face dataset {dataset}, config ground_truth (train + test), {pin}. {n_rows} documents. "
         f"Built {_dt.date.today().isoformat()} by tools/gt-label-workbook/build_gt_workbook.py.", "p"),
        ("", "p"),
        ("LEGEND", "h"),
        ("Yellow cell: a label is missing (status UNLABELED or pending_annotation), or a date label has no year / "
         "does not parse. Fill it.", "yellow"),
        ("Green cell: a label a person added or verified (see HOW TO LABEL). Existing dataset values are not "
         "highlighted.", "green"),
        ("Drop-down cells (status, subclass, intent, claim_type, coverage_determination, record_type, urgency, "
         "communication_type, ...): pick from the list; Google Sheets shows them as chips. The lists are read "
         "from the Dojo and live on a hidden 'Lists' sheet.", "p"),
        ("Header colours: dark grey = identity, blue = links or values, mid grey = status. Light grey headers "
         "(hidden, right edge) are helper columns: click the + above them to show.", "p"),
        ("", "p"),
        ("HOW TO LABEL", "h"),
        ("1. Yellow cells are missing labels. Type the value as the document states it, then set the status cell "
         "to 'populated' (drop-down). Both cells turn green.", "p"),
        ("2. Dates: write the date exactly as the document states it, INCLUDING THE YEAR when the document has "
         "one (e.g. 'August 18, 2014'). The scorer never fills a missing year from the clock, so a label "
         "without a year cannot check the model's year.", "p"),
        ("3. Lists (parties, keywords, signatories, ...): a JSON array, e.g. [\"Acme Corp\", \"Beta LLC\"]. "
         "CUAD spans: separate several spans with ' || '. Spans show their text only; the source keeps each "
         "span's character offset.", "p"),
        ("4. A field the document genuinely does not contain: leave the value blank and set status "
         "'schema_documented_absence'. A field that cannot apply to this subclass: 'not_applicable'. "
         "Both turn green when they close a gap.", "p"),
        ("5. 'verified': set it after checking or correcting an existing value (it counts as populated). "
         "A verified field always turns green.", "p"),
        ("6. Subclass is a drop-down of the Dojo's subclasses, prefilled with the dataset's; it turns green when "
         "you change it. Changing it does not recompute which fields apply.", "p"),
        ("7. Identity columns (document_id, filename, split, ...) are for reading: do not edit them. On the class "
         "sheets, 'Hub viewer' opens the dataset viewer's text search for the filename in the 'default' config "
         "(the viewer's search index can take a minute to load).", "p"),
        ("8. 'Date Audit' lists every date label, one span per row, with the components it states; use "
         "'Corrected value' for rows flagged MISSING LABEL, NO YEAR, NO DAY/MONTH or UNPARSEABLE. Its counts "
         "do not feed the To Label counters: correct the label itself on the To Label sheet.", "p"),
        ("9. FILTER with the header arrows; do not SORT. Links between sheets point at fixed rows.", "p"),
        ("", "p"),
        *_to_label_readme(tl, truncated_texts=tl["text_truncated"]),
        ("STATUS CODES", "h"),
        ("populated - a value is labelled and will be scored.", "p"),
        ("verified - a person checked or corrected the value (not a dataset code; counts as populated).", "p"),
        ("pending_annotation - known gap in the dataset; not scored until labelled.", "p"),
        ("schema_documented_absence - the document does not contain this field; a blank prediction is correct.", "p"),
        ("not_applicable - the field does not apply to this class/subclass.", "p"),
        ("UNLABELED - the Dojo scores this field but the dataset carries no label for it (not scored today).", "p"),
        ("Derived fields (communication_type, cuad_family, merger_consideration, cms_source_table) are derived "
         "from the subclass and shown as populated; they are never gaps.", "p"),
        ("", "p"),
        ("COLUMNS AND COUNTS", "h"),
        ("Columns: a value column and a status column for each field the Dojo scores for that class "
         "(extraction_schema; corporate records add each subclass's entity fields and filing_number; contract "
         "dates come from the CUAD clause spans, e.g. 'Agreement Date' -> agreement_date). A pipeline field "
         "the dataset never labels for a class (no value and no gt_presence key on any row) is not a labelling "
         "target and has no column. Left out in this build: "
         + ("; ".join(f"{c}: {', '.join(v)}" for c, v in DROPPED_FIELDS.items() if v) or "none") + ".", "p"),
        ("Counts ('Labels missing', 'still missing (live)', the Summary sheet) are formulas that follow your edits; "
         "they count status cells only and use functions that work in Excel, LibreOffice and Google Sheets. "
         "'Labels missing' on a class sheet counts statuses only; the To Label counter also includes flagged dates.",
         "p"),
        ("", "p"),
        ("GETTING EDITS BACK INTO THE DATASET", "h"),
        ("This workbook is a working copy. Edited labels reach the Hub only through the dataset's own reviewed "
         "publish step, run by the dataset owner; nothing here writes to Hugging Face.", "p"),
        (f"Values longer than an Excel cell holds are cut and marked [TRUNCATED]; edit those on the Hub. "
         f"Truncated cells in this build: {truncated}.", "p"),
    ]
    return lines


def build_workbook(rows: pd.DataFrame, out: Path, *, dataset: str, revision: str,
                   resolved: str | None = None, to_label: Iterable[str] = (),
                   hub_files: Iterable[str] | None = None,
                   max_workbook_mb: float = MAX_WORKBOOK_MB) -> dict[str, Any]:
    """Write the workbook for ``rows`` (the ground_truth config) to ``out``.

    ``to_label`` names the classes that also get a "To Label" and a "Text" sheet; ``rows``
    then needs a ``doc_text`` column (and may carry ``metadata`` for the original-document
    links). ``hub_files`` lists the files the dataset repo holds at ``resolved``.
    """
    DROPPED_FIELDS.clear()
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
    lists = _Lists()
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
            fill = HEAD_VAL if name == "expected_subclass" else HEAD_LINK if name == "Hub viewer" else HEAD_ID
            _header(ws, i, name, fill, width=width)
        c_sub = 4
        first = len(ID_COLS) + 1
        for j, (_, header, ftype) in enumerate(cols):
            vc = first + 2 * j
            _header(ws, vc, header, HEAD_VAL, f"Scored field type: {ftype}. Edit the value here.", _value_width(ftype))
            _header(ws, vc + 1, f"{header} | status", HEAD_ST,
                    "Presence code. Set it to 'populated' when you add a value.", 18)
        last = first + 2 * len(cols) - 1
        miss, orig_col, mask_col = last + 1, last + 2, last + 3
        _header(ws, miss, "Labels missing", HEAD_ID,
                "Formula: this row's fields whose status is UNLABELED or pending_annotation.", 12)
        _header(ws, orig_col, "dataset subclass (helper)", HEAD_HELP, "Helper: the subclass the dataset carries.", 18)
        _header(ws, mask_col, "needs mask (helper)", HEAD_HELP, "Helper: one character per field pair; 1 = the "
                "dataset had no label. Drives the green highlight.", 14)
        f, l_ = get_column_letter(first), get_column_letter(last)
        for r, (_, row) in enumerate(sub.iterrows(), start=2):
            gt = json.loads(row.gt_fields) if isinstance(row.gt_fields, str) else dict(row.gt_fields)
            presence = _json(gt.get("gt_presence")) or {}
            subclass = str(row.expected_subclass)
            for i, v in enumerate([row.document_id, row.filename, row.split, subclass,
                                   row.source_corpus, row.annotation_method], start=1):
                _set_str(_data_cell(ws, r, i, fill=None if i == 4 else IDFILL, wrap=i == 2), str(v))
            link = _data_cell(ws, r, 7, wrap=False)
            _url(link, f"https://huggingface.co/datasets/{dataset}/viewer/default/{row.split}"
                       f"?q={quote(str(row.filename), safe='')}", "open")
            mask = []
            for j, (key, header, ftype) in enumerate(cols):
                value, status = cell_for(cls, key, gt, presence, subclass)
                vcell = _data_cell(ws, r, first + 2 * j)
                _set_str(vcell, clip(value, f"{title}:{row.document_id}:{header}"))
                _data_cell(ws, r, first + 2 * j + 1, status)
                mask.append("1" if status in MISSING else "0")
                if status in MISSING:
                    vcell.fill = TODO
                if ftype.startswith("date"):
                    for span in (value.split(" || ") if value else [""]):
                        flag, stated = date_flag(span, status, parts_fn)
                        date_rows.append([title, row.document_id, row.filename, row.split, subclass,
                                          header, span, status, *stated, flag])
            _data_cell(ws, r, miss, _missing_formula(f, l_, r), font=BOLD, wrap=False)
            _set_str(_data_cell(ws, r, orig_col, wrap=False), subclass)
            _set_str(_data_cell(ws, r, mask_col, wrap=False), "".join(mask))
            ws.row_dimensions[r].height = ROW_HEIGHT
        _decorate(ws, lists, cls=cls, fields=cols, first=first, n_rows=len(sub), sub_col=c_sub, orig_col=orig_col,
                  mask_col=mask_col, sub_values=subclass_choices(cls, sub.expected_subclass.astype(str)))
        _group_hidden(ws, orig_col, mask_col)
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = f"A1:{get_column_letter(miss)}{len(sub) + 1}"
        ws.row_dimensions[1].height = 48
        sheets.append((title, len(sub), f, l_))

    tl = _add_to_label(wb, rows, class_cols, to_label, parts_fn=parts_fn, dataset=dataset, commit=resolved,
                       hub_files=hub_files, out=out, max_mb=max_workbook_mb, clip=clip, lists=lists)

    _date_audit(dates, date_rows, parts_source)
    _summary(summary, sheets, tl, len(date_rows))

    for i, (text, kind) in enumerate(_readme_rows(dataset=dataset, revision=revision, resolved=resolved,
                                                 n_rows=len(rows), tl=tl, truncated=len(truncated)), start=1):
        c = readme.cell(row=i, column=1, value=text)
        c.font = {"title": TITLE, "h": SECTION}.get(kind, FONT)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if kind == "yellow":
            c.fill = TODO
        elif kind == "green":
            c.fill = DONE
    readme.column_dimensions["A"].width = 150
    readme.sheet_view.showGridLines = False

    lists.write(wb)
    wb.active = 0
    wb.properties.title = "Mailroom ground-truth label workbook"
    wb.properties.description = f"{dataset} {revision}" + (f" = {resolved}" if resolved else "")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"out": str(out), "rows": len(rows), "date_rows": len(date_rows), "truncated": len(truncated),
            "revision": revision, "resolved": resolved,
            "date_parts": parts_source, "sheets": [s[0] for s in sheets],
            "to_label": {name: n for name, n, _, _ in tl["sheets"]},
            "text_mode": tl["text_mode"], "text_docs": tl["text_docs"], "text_chars": tl["text_chars"],
            "text_chunks": tl["text_chunks"], "text_truncated": tl["text_truncated"],
            "text_controls_replaced": tl["controls_replaced"], "empty_texts": tl["empty_texts"],
            "lists": sorted(lists.items), "all_sheets": wb.sheetnames,
            "dropped_fields": {c: list(v) for c, v in DROPPED_FIELDS.items() if v}}


def _date_audit(dates, date_rows: list[list], parts_source: str) -> None:
    heads = ["Sheet", "document_id", "filename", "split", "subclass", "date field",
             "GT value (one span per row)", "status", "stated year", "stated month", "stated day",
             "flag", "Corrected value (edit)", "Notes (edit)"]
    for i, (h, w) in enumerate(zip(heads, (18, 22, 34, 7, 18, 18, 40, 18, 9, 9, 9, 22, 28, 30)), start=1):
        _header(dates, i, h, HEAD_VAL if i >= 13 else HEAD_ID, width=w)
    for r, rec in enumerate(date_rows, start=2):
        for i, v in enumerate(rec, start=1):
            c = dates.cell(row=r, column=i)
            if isinstance(v, str):
                _set_str(c, v)  # a span such as "=..." stays text, never a formula
            else:
                c.value = v
            c.font, c.border = FONT, BORDER
        if rec[11] in ("MISSING LABEL", *DATE_PROBLEMS):
            for i in (13, 14):
                dates.cell(row=r, column=i).fill = TODO
    last = max(len(date_rows), 1) + 1
    dates.conditional_formatting.add(
        f"M2:M{last}", FormulaRule(formula=['LEN($M2)>0'], fill=DONE_CF))  # a correction a person typed
    dates.freeze_panes = "C2"
    dates.auto_filter.ref = f"A1:N{last}"
    dates.row_dimensions[1].height = 36
    dates.cell(row=1, column=9).comment = Comment(
        f"Stated components come from {parts_source} at build time; a blank means the label "
        "does not state it. The clock is never read.", "gt-label-workbook")


def _summary(summary, sheets: list[tuple[str, int, str, str]], tl: dict[str, Any], n_dates: int) -> None:
    summary["A1"] = "Ground-truth label coverage (live formulas over the class sheets)"
    summary["A1"].font = TITLE
    summary["A2"] = ("Counts follow your edits on the class sheets (status cells only). The To Label table at "
                     "the bottom follows your edits on the To Label sheets.")
    summary["A2"].font = GREY
    hdr = ["Class sheet", "Documents", "Field cells", *STATUSES, "Missing labels", "Missing share"]
    for i, h in enumerate(hdr, start=1):
        c = summary.cell(row=4, column=i, value=h)
        c.font, c.fill, c.border = HEAD, HEAD_ID, BORDER
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        summary.column_dimensions[get_column_letter(i)].width = 24 if i == 1 else 16
    summary.row_dimensions[4].height = 34
    n_st = len(STATUSES)
    c_stat0 = 4  # first status column (D)
    c_miss = c_stat0 + n_st
    L = get_column_letter
    for r, (title, n, c0, c1) in enumerate(sheets, start=5):
        q = f"'{title}'"
        end = n + 1 + SLACK
        blk = f"{q}!{c0}2:{c1}{end}"
        summary.cell(row=r, column=1, value=title).font = FONT
        summary.cell(row=r, column=2, value=f"=COUNTA({q}!A:A)-1").font = FONT
        summary.cell(row=r, column=3, value=f"=SUM({L(c_stat0)}{r}:{L(c_stat0 + n_st - 1)}{r})").font = FONT
        for i, s in enumerate(STATUSES, start=c_stat0):
            summary.cell(row=r, column=i, value=(
                f'=SUMPRODUCT((MOD(COLUMN({blk})-COLUMN({q}!{c0}2),2)=1)*({blk}="{s}"))')).font = FONT
        ui, pi = c_stat0 + STATUSES.index("UNLABELED"), c_stat0 + STATUSES.index("pending_annotation")
        summary.cell(row=r, column=c_miss, value=f"={L(ui)}{r}+{L(pi)}{r}").font = FONT
        c = summary.cell(row=r, column=c_miss + 1, value=f"=IF(C{r}=0,0,{L(c_miss)}{r}/C{r})")
        c.font, c.number_format = FONT, "0.0%"
    tot = 5 + len(sheets)
    summary.cell(row=tot, column=1, value="Total").font = BOLD
    for i in range(2, c_miss + 1):
        col = L(i)
        summary.cell(row=tot, column=i, value=f"=SUM({col}5:{col}{tot - 1})").font = BOLD
    c = summary.cell(row=tot, column=c_miss + 1, value=f"=IF(C{tot}=0,0,{L(c_miss)}{tot}/C{tot})")
    c.font, c.number_format = BOLD, "0.0%"
    summary.cell(row=tot + 2, column=1, value="Date Audit flags").font = SECTION
    for k, flag in enumerate(DATE_FLAGS, start=tot + 3):
        summary.cell(row=k, column=1, value=flag).font = FONT
        summary.cell(row=k, column=2, value=f"=COUNTIF('Date Audit'!L:L,A{k})").font = FONT
    if tl["sheets"]:
        k0 = tot + 4 + len(DATE_FLAGS)
        summary.cell(row=k0 - 1, column=1, value="To Label sheets").font = SECTION
        for i, h in enumerate(("Sheet", "Documents to label", "Fields needing labels (as built)", "Still missing (live)",
                               "Resolved", "Documents with nothing left"), start=1):
            c = summary.cell(row=k0, column=i, value=h)
            c.font, c.fill, c.border = HEAD, HEAD_ID, BORDER
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        summary.row_dimensions[k0].height = 34
        for k, (name, n, live_col, built_col) in enumerate(tl["sheets"], start=k0 + 1):
            q = f"'{name}'"
            summary.cell(row=k, column=1, value=name).font = FONT
            summary.cell(row=k, column=2, value=f"=COUNTA({q}!A:A)-1").font = FONT
            summary.cell(row=k, column=3, value=f"=SUM({q}!{built_col}:{built_col})").font = FONT
            summary.cell(row=k, column=4, value=f"=SUM({q}!{live_col}:{live_col})").font = FONT
            summary.cell(row=k, column=5, value=f"=MAX(0,C{k}-D{k})").font = FONT
            summary.cell(row=k, column=6, value=f"=COUNTIF({q}!{live_col}2:{live_col}{n + 1 + SLACK},0)").font = FONT
    summary.freeze_panes = "B5"


# ---------------------------------------------------------------------------
# Hub access (read-only)


class HubError(RuntimeError):
    """The revision does not resolve, or a pinned file cannot be fetched."""


def _fetch_json(url: str) -> Any:
    from pipeline import hf_corpus_loader as loader

    return loader._http_get_json(url)


def _fetch_bytes(url: str, label: str) -> bytes:
    from pipeline import hf_corpus_loader as loader

    return loader._cached_get_bytes(url, label=label)


def resolve_revision(dataset: str, revision: str) -> tuple[str, list[str]]:
    """(commit sha, repo file paths) of ``dataset`` at ``revision``; raises :class:`HubError` if it does not resolve."""
    url = f"{HUB_BASE}/api/datasets/{dataset}/revision/{quote(revision, safe='')}"
    try:
        info = _fetch_json(url)
    except Exception as exc:
        raise HubError(f"cannot resolve {dataset}@{revision}: {type(exc).__name__}: {exc}") from exc
    sha = str(info.get("sha") or "") if isinstance(info, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise HubError(f"{dataset}@{revision} did not resolve to a commit sha (got {sha!r})")
    return sha, [str(s["rfilename"]) for s in info.get("siblings") or [] if "rfilename" in s]


def hub_frame(dataset: str, sha: str, files: Iterable[str], config: str, split: str) -> pd.DataFrame:
    """One config's split from the parquet files the repo holds at commit ``sha`` (not the viewer's
    refs/convert/parquet branch, which can lag the revision)."""
    prefix = f"parquet/{config}/{split}/"
    paths = sorted(f for f in files if f.startswith(prefix) and f.endswith(".parquet"))
    if not paths:
        raise HubError(f"{dataset}@{sha[:12]} holds no parquet files under {prefix}")
    frames = []
    for path in paths:
        url = f"{HUB_BASE}/datasets/{dataset}/resolve/{sha}/{quote(path)}"
        try:
            raw = _fetch_bytes(url, label=f"{dataset.replace('/', '__')}_{config}_{split}_{sha[:12]}")
        except Exception as exc:
            raise HubError(f"cannot download {path} at {sha[:12]}: {type(exc).__name__}: {exc}") from exc
        frames.append(pd.read_parquet(io.BytesIO(raw)))
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI


def load_rows(parquet_dir: Path | None, dataset: str, sha: str | None, files: list[str] | None, *,
              with_text: bool = False) -> pd.DataFrame:
    """The ground_truth rows (train + test); ``with_text`` adds ``doc_text`` and ``metadata``
    from the ``default`` config, joined on ``filename``. Hub rows come from commit ``sha``."""

    def frame_of(config: str, split: str, local: str) -> pd.DataFrame:
        if parquet_dir is not None:
            path = parquet_dir / f"{local}_{split}.parquet"
            if not path.exists():
                if config == "default":
                    raise SystemExit(f"{path} not found: --to-label needs the default config's text; "
                                     "add default_train.parquet / default_test.parquet or pass --to-label none")
                raise SystemExit(f"{path} not found")
            return pd.read_parquet(path)
        return hub_frame(dataset, sha or "", files or [], config, split)

    frames = []
    for split in ("train", "test"):
        frame = frame_of("ground_truth", split, "gt")
        if "split" not in frame.columns:
            frame = frame.assign(split=split)
        if with_text:
            blind = frame_of("default", split, "default")
            keep = [c for c in ("filename", "doc_text", "metadata") if c in blind.columns]
            frame = frame.merge(blind[keep], on="filename", how="left", validate="one_to_one")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def parse_classes(value: str) -> tuple[str, ...]:
    known = [c for c, _ in CLASSES]
    wanted = [] if value.strip().lower() in ("", "none") else [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in wanted if v not in known]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown class {bad}; choose from {known} or 'none'")
    return tuple(dict.fromkeys(wanted))


def main(argv: list[str] | None = None) -> int:
    from pipeline.hf_corpora import FULL_CORPUS_ID, FULL_CORPUS_REVISION

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="workbook path (.xlsx)")
    ap.add_argument("--revision", default=FULL_CORPUS_REVISION,
                    help="corpus tag, branch or commit; resolved to a commit sha, and an unknown one is an error "
                         f"(default: the pipeline pin, {FULL_CORPUS_REVISION})")
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
    sha, hub_files = None, None
    try:
        if args.parquet_dir is None:
            sha, hub_files = resolve_revision(FULL_CORPUS_ID, args.revision)
        rows = load_rows(args.parquet_dir, FULL_CORPUS_ID, sha, hub_files, with_text=bool(args.to_label))
    except HubError as exc:
        raise SystemExit(f"error: {exc}") from exc
    stats = build_workbook(rows, args.out, dataset=FULL_CORPUS_ID, revision=args.revision, resolved=sha,
                           to_label=args.to_label, hub_files=hub_files, max_workbook_mb=args.max_workbook_mb)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
