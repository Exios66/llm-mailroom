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


# ---------------------------------------------------------------------------
# To Label / Text sheets

DATASET = "org/ds"
COMMIT = "a" * 40
_SAMPLE = {"name": "Acme Corp", "label": "request", "free_text": "x", "id": "ID-1", "money": "$5",
           "date": "March 3, 2001"}


def _full_gt(tool, cls, **override):
    """Every Dojo-scored field of ``cls`` populated (and dates with a year)."""
    from llm_dojo_scoring.schemas.extraction_schemas import extraction_schema

    gt, presence = {}, {}
    for name, ftype in extraction_schema(cls).field_types.items():
        if name == "cms_source_table":
            continue
        base = ftype.split(":")[0]
        gt[name] = json.dumps(["a"]) if base == "entity_list" else _SAMPLE.get(base, "x")
        presence[name] = "populated"
    gt.update(override)
    gt["gt_presence"] = json.dumps(presence)
    return gt


def _doc(tool, doc_id, cls, subclass, text, gt, *, meta=None, split="train", filename=None):
    row = _row(doc_id, cls, subclass, gt, split=split, filename=filename)
    row["doc_text"] = text
    row["metadata"] = meta if meta is not None else {}
    return row


@pytest.fixture()
def label_rows(tool):
    sec_meta = {"exhibit_url": "/Archives/edgar/data/1/0001/ex3.htm"}
    return pd.DataFrame([
        # needs labels: communication_date is never labelled for correspondence
        _doc(tool, "DOC-B", "correspondence", "memo", "Second memo.\n\nBody.",
             _full_gt(tool, "correspondence", communication_date="")),
        _doc(tool, "DOC-A", "correspondence", "email", "=SUM(1,2) is not a formula",
             _full_gt(tool, "correspondence", communication_date="")),
        _doc(tool, "DOC-C", "correspondence", "email", "Fully labelled email.",
             _full_gt(tool, "correspondence")),
        # populated but the date has no year
        _doc(tool, "DOC-D", "insurance_claim", "carrier", "Claim text \x01 with a control char.",
             _full_gt(tool, "insurance_claim", date_of_loss="August 18"), meta={"original_file": "files/x.txt"}),
        _doc(tool, "DOC-E", "insurance_claim", "carrier", "All good.", _full_gt(tool, "insurance_claim")),
        _doc(tool, "DOC-S", "corporate_record", "bylaws", "Bylaws text.",
             {"gt_presence": "{}"}, meta=sec_meta, split="test"),
    ])


def _build(tool, rows, tmp_path, **kw):
    out = tmp_path / "gt.xlsx"
    kw.setdefault("to_label", ("correspondence", "insurance_claim", "corporate_record"))
    stats = tool.build_workbook(rows, out, dataset=DATASET, revision="v0", resolved=COMMIT, **kw)
    return out, stats, load_workbook(out)


def test_to_label_lists_only_documents_with_a_missing_label(tool, label_rows, tmp_path):
    _, stats, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Correspondence"]
    ids = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
    assert ids == ["DOC-A", "DOC-B"]  # sorted by subclass (email, memo); DOC-C is fully labelled
    assert stats["to_label"]["To Label – Correspondence"] == 2
    ins = wb["To Label – Insurance Claims"]
    assert [ins.cell(row=r, column=1).value for r in range(2, ins.max_row + 1)] == ["DOC-D"]


def test_to_label_columns_follow_the_spec_and_skip_complete_fields(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Correspondence"]
    headers = [c.value for c in ws[1]]
    # no original link exists for these rows, so there is no 'Open original' column
    assert headers[:6] == ["document_id", "split", "subclass", "filename", "Open text", "fields needing labels"]
    assert headers[6:] == ["communication_date", "communication_date | status", "still missing (live)", "date flags"]
    assert ws.cell(row=2, column=7).fill.fgColor.rgb.endswith("FFFF00")
    assert ws.cell(row=2, column=8).value == "UNLABELED"
    assert ws.cell(row=2, column=6).value == 1
    assert ws.freeze_panes == "E2"
    assert ws.auto_filter.ref == f"A1:J{ws.max_row}"
    assert ws.cell(row=2, column=7).alignment.wrap_text
    live = ws.cell(row=2, column=9).value
    assert live.startswith("=COUNTIF(G2:H2") and "pending_annotation" in live


def test_a_date_without_a_year_needs_a_label_even_when_populated(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Insurance Claims"]
    headers = [c.value for c in ws[1]]
    assert headers[4:6] == ["Open text", "fields needing labels"]  # 'files/x.txt' is not in the Hub listing
    col = headers.index("date_of_loss") + 1
    assert ws.cell(row=2, column=col).value == "August 18"
    assert ws.cell(row=2, column=col + 1).value == "populated"
    assert ws.cell(row=2, column=col).fill.fgColor.rgb.endswith("FFFF00")
    assert ws.cell(row=2, column=headers.index("date flags") + 1).value == "date_of_loss: NO YEAR"
    assert ws.cell(row=2, column=6).value == 1


def test_original_link_hub_blob_is_pinned_and_sec_is_absolute(tool, label_rows, tmp_path):
    assert tool.original_link({"original_file": "files/a b.txt"}, DATASET, COMMIT, {"files/a b.txt"}) == (
        f"https://huggingface.co/datasets/org/ds/blob/{COMMIT}/files/a%20b.txt", "Hub file")
    assert tool.original_link({"original_file": "files/a.txt"}, DATASET, COMMIT, set()) is None
    assert tool.original_link({"original_file": "files/a.txt"}, DATASET, None, {"files/a.txt"}) is None
    assert tool.original_link({"exhibit_url": "/Archives/edgar/data/1/2/x.htm"}, DATASET, COMMIT, None) == (
        "https://www.sec.gov/Archives/edgar/data/1/2/x.htm", "SEC EDGAR")
    assert tool.original_link('{"exhibit_url": "https://www.sec.gov/Archives/edgar/data/1/y.htm"}',
                              DATASET, COMMIT, None)[0] == "https://www.sec.gov/Archives/edgar/data/1/y.htm"
    assert tool.original_link({"exhibit_url": "https://evil.example/Archives/x"}, DATASET, COMMIT, None) is None
    assert tool.original_link(None, DATASET, COMMIT, None) is None

    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Corporate Records"]
    assert [c.value for c in ws[1]][4] == "Open original"
    assert ws.cell(row=2, column=5).hyperlink.target == "https://www.sec.gov/Archives/edgar/data/1/0001/ex3.htm"
    _, _, wb = _build(tool, label_rows, tmp_path, hub_files=["files/x.txt"])
    link = wb["To Label – Insurance Claims"].cell(row=2, column=5).hyperlink
    assert link.target == f"https://huggingface.co/datasets/org/ds/blob/{COMMIT}/files/x.txt"


def test_open_text_and_back_links_point_at_each_other(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws, tx = wb["To Label – Correspondence"], wb["Text – Correspondence"]
    for r in (2, 3):
        loc = ws.cell(row=r, column=5).hyperlink.location
        sheet, ref = loc.rsplit("!", 1)
        assert sheet == "'Text – Correspondence'"
        head = int(ref[1:])
        assert tx.cell(row=head, column=1).value == ws.cell(row=r, column=1).value
        assert tx.cell(row=head, column=2).hyperlink.location == f"'To Label – Correspondence'!A{r}"
    # DOC-A: header row 2, one chunk on row 3; DOC-B's header follows on row 4
    assert ws.cell(row=3, column=5).hyperlink.location == "'Text – Correspondence'!A4"
    assert wb.sheetnames[2:5] == ["To Label – Corporate Records", "To Label – Correspondence",
                                  "To Label – Insurance Claims"]


def test_text_that_looks_like_a_formula_stays_text_and_controls_are_visible(tool, label_rows, tmp_path):
    out, stats, wb = _build(tool, label_rows, tmp_path)
    tx = wb["Text – Correspondence"]
    assert tx.cell(row=3, column=3).value == "=SUM(1,2) is not a formula"
    assert tx.cell(row=3, column=3).data_type == "s"
    ins = wb["Text – Insurance Claims"]
    assert ins.cell(row=3, column=3).value == "Claim text ␁ with a control char."
    assert stats["text_controls_replaced"] == 1 and stats["text_truncated"] == 0


def test_chunk_text_round_trips_a_100k_text_exactly(tool):
    text = "".join(f"Paragraph {i}. " + "word " * 120 + "\n\n" for i in range(170))[:100_000]
    assert len(text) == 100_000
    chunks = tool.chunk_text(text)
    assert len(chunks) >= 4 and "".join(chunks) == text
    assert all(0 < len(c) <= 32_000 for c in chunks)
    assert all(c.endswith("\n\n") for c in chunks[:-1])  # cut on paragraph boundaries


@pytest.mark.parametrize(
    "text",
    ["", "short", "x" * 100_000, " " * 70_000, "line\n" * 30_000, "ab\r\n" * 40_000, "\U0001F600" * 40_000,
     "a" * 31_999 + "\U0001F600" * 3_000, "tail \n\n", "no break " * 9_000],
)
def test_chunk_text_never_drops_a_character(tool, text):
    chunks = tool.chunk_text(text)
    assert "".join(chunks) == text
    assert all(tool._utf16_len(c) <= 32_767 for c in chunks)


def test_chunk_text_rejects_a_limit_beyond_excel(tool):
    with pytest.raises(ValueError):
        tool.chunk_text("x", limit=40_000)


def test_long_text_is_split_across_rows_and_reads_back_exactly(tool, label_rows, tmp_path):
    long_text = ("Sentence number one. " * 40 + "\n\n") * 130  # ~110k characters
    assert len(long_text) > 100_000
    rows = label_rows.copy()
    rows.loc[rows.document_id == "DOC-B", "doc_text"] = long_text
    out, stats, wb = _build(tool, rows, tmp_path)
    tx = wb["Text – Correspondence"]
    parts, in_doc = [], False
    for r in range(2, tx.max_row + 1):
        a, b, c = (tx.cell(row=r, column=i).value for i in (1, 2, 3))
        if b == "back to To Label":
            in_doc = a == "DOC-B"
            header = c
        elif in_doc:
            assert len(c) <= 32_000
            parts.append(c)
    assert len(parts) >= 4 and "".join(parts) == long_text
    assert f"{len(long_text):,} characters" in header and f"{len(parts)} parts" in header
    assert stats["text_truncated"] == 0 and stats["text_mode"] == "sheet"
    assert stats["text_chars"] >= len(long_text)


def test_oversized_text_goes_to_files_with_relative_links(tool, label_rows, tmp_path):
    out, stats, wb = _build(tool, label_rows, tmp_path, max_workbook_mb=0.00001)
    assert stats["text_mode"] == "files"
    assert not [n for n in wb.sheetnames if n.startswith("Text")]
    ws = wb["To Label – Correspondence"]
    assert ws.cell(row=2, column=5).hyperlink.target == "gt_texts/correspondence/DOC-A.txt"
    assert (tmp_path / "gt_texts" / "correspondence" / "DOC-A.txt").read_bytes() == b"=SUM(1,2) is not a formula"
    raw = (tmp_path / "gt_texts" / "insurance_claim" / "DOC-D.txt").read_text(encoding="utf-8")
    assert raw == "Claim text \x01 with a control char."  # files keep the exact original


def test_to_label_is_optional_and_validated(tool, rows, label_rows, tmp_path):
    out, stats, wb = _build(tool, rows, tmp_path, to_label=())
    assert stats["to_label"] == {} and not [n for n in wb.sheetnames if n.startswith(("To Label", "Text"))]
    with pytest.raises(ValueError, match="doc_text"):
        tool.build_workbook(rows, tmp_path / "x.xlsx", dataset=DATASET, revision="v0", to_label=("correspondence",))
    with pytest.raises(ValueError, match="unknown class"):
        tool.build_workbook(label_rows, tmp_path / "x.xlsx", dataset=DATASET, revision="v0", to_label=("court",))


def test_to_label_flag_parsing(tool):
    import argparse

    assert tool.parse_classes("insurance_claim, correspondence") == ("insurance_claim", "correspondence")
    assert tool.parse_classes("none") == ()
    with pytest.raises(argparse.ArgumentTypeError):
        tool.parse_classes("court")
    assert tool.TO_LABEL_DEFAULT == ("insurance_claim", "corporate_record", "correspondence")


def test_summary_and_readme_describe_the_to_label_sheets(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    summary = [[c.value for c in row] for row in wb["Summary"].iter_rows()]
    labelled = [r for r in summary if r[0] == "To Label – Correspondence"]
    assert labelled and labelled[0][1].startswith("=COUNTA('To Label – Correspondence'!A2:A3)")
    readme = "\n".join(str(c.value) for c in wb["README"]["A"] if c.value)
    assert "TO LABEL SHEETS" in readme and "MUST INCLUDE THE YEAR" in readme and "Texts cut in this build: 0" in readme


def test_cli_builds_offline_from_parquet_with_text(tool, label_rows, tmp_path, capsys):
    pytest.importorskip("pyarrow")
    gt = label_rows.drop(columns=["doc_text", "metadata"])
    for split in ("train", "test"):
        part = gt[gt.split == split]
        part.to_parquet(tmp_path / f"gt_{split}.parquet")
        blind = label_rows.loc[part.index, ["filename", "doc_text", "metadata"]]
        blind.to_parquet(tmp_path / f"default_{split}.parquet")
    out = tmp_path / "cli.xlsx"
    assert tool.main(["--out", str(out), "--parquet-dir", str(tmp_path), "--to-label",
                      "correspondence,corporate_record"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["to_label"] == {"To Label – Corporate Records": 1, "To Label – Correspondence": 2}
    assert load_workbook(out)["Text – Correspondence"].max_row == 5  # header + 2 documents x (header + 1 part)
    assert tool.main(["--out", str(tmp_path / "plain.xlsx"), "--parquet-dir", str(tmp_path), "--to-label", "none"]) == 0
    assert json.loads(capsys.readouterr().out)["to_label"] == {}
