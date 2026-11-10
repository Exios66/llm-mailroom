"""tools/gt-label-workbook: the editable ground-truth workbook builder (no network)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
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
    # communication_date has a gt_presence key but no value: a labelling target, still unlabelled.
    mail = {"intent": "request", "subject_matter": "Meeting", "keywords": '["meeting"]',
            "gt_presence": json.dumps({"intent": "populated", "communication_date": "populated"})}
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
    # communication_date is a dataset field (gt_presence key) with no label yet.
    assert "communication_date" in headers and "communication_date | status" in headers
    # sender is a pipeline field the dataset never carries for these rows: no column.
    assert "sender" not in headers and "sender | status" not in headers
    assert "sender" in stats["dropped_fields"]["correspondence"]
    col = headers.index("communication_date") + 1
    assert wb["Correspondence"].cell(row=2, column=col + 1).value == "UNLABELED"
    assert wb["Correspondence"].cell(row=2, column=col).fill.fgColor.rgb == tool.TODO.fgColor.rgb


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
    formula = ws.cell(row=by_id["DOC-1"], column=headers.index("Labels missing") + 1).value
    # status cells only (value cells that happen to read "UNLABELED" are not counted)
    assert formula.startswith("=SUMPRODUCT(") and "pending_annotation" in formula and "MOD(COLUMN(" in formula


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
    # identity, links, subclass, then a value + status pair per field, then the counters and the helpers;
    # no original link exists for these rows, so there is no 'Open original' column
    assert headers == ["document_id", "filename", "split", "Open text", "subclass",
                       "communication_date", "communication_date | status", "still missing (live)", "date flags",
                       "fields needing labels (as built)", "dataset subclass (helper)", "needs mask (helper)"]
    assert ws.cell(row=2, column=6).fill.fgColor.rgb == tool.TODO.fgColor.rgb
    assert ws.cell(row=2, column=7).value == "UNLABELED"
    assert ws.cell(row=2, column=10).value == 1
    assert ws.freeze_panes == "B2"
    assert ws.auto_filter.ref == f"A1:I{ws.max_row}"
    assert ws.cell(row=2, column=6).alignment.wrap_text
    live = ws.cell(row=2, column=8).value
    assert live.startswith("=SUMPRODUCT((MOD(COLUMN(F2:G2)-COLUMN(F2),2)=1)") and "pending_annotation" in live
    for letter in ("J", "K", "L"):  # helpers are grouped and hidden
        assert ws.column_dimensions["J"].hidden and ws.column_dimensions["J"].outline_level == 1
    assert ws.column_dimensions["J"].min == 10 and ws.column_dimensions["J"].max == 12


def test_a_date_without_a_year_needs_a_label_even_when_populated(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Insurance Claims"]
    headers = [c.value for c in ws[1]]
    assert "Open original" not in headers  # 'files/x.txt' is not in the Hub listing
    col = headers.index("date_of_loss") + 1
    assert ws.cell(row=2, column=col).value == "August 18"
    assert ws.cell(row=2, column=col + 1).value == "populated"
    assert ws.cell(row=2, column=col).fill.fgColor.rgb == tool.TODO.fgColor.rgb
    assert ws.cell(row=2, column=headers.index("date flags") + 1).value == "date_of_loss: NO YEAR"
    assert ws.cell(row=2, column=_col(ws, "fields needing labels (as built)")).value == 1
    assert ws.cell(row=2, column=_col(ws, "needs mask (helper)")).value == "2"  # a date problem, not a plain gap


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
    assert [c.value for c in ws[1]][3] == "Open original"  # identity columns, then the links
    assert ws.cell(row=2, column=4).hyperlink.target == "https://www.sec.gov/Archives/edgar/data/1/0001/ex3.htm"
    _, _, wb = _build(tool, label_rows, tmp_path, hub_files=["files/x.txt"])
    link = wb["To Label – Insurance Claims"].cell(row=2, column=4).hyperlink
    assert link.target == f"https://huggingface.co/datasets/org/ds/blob/{COMMIT}/files/x.txt"


def test_open_text_and_back_links_point_at_each_other(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws, tx = wb["To Label – Correspondence"], wb["Text – Correspondence"]
    c_open = _col(ws, "Open text")
    for r in (2, 3):
        loc = ws.cell(row=r, column=c_open).hyperlink.location
        sheet, ref = loc.rsplit("!", 1)
        assert sheet == "'Text – Correspondence'"
        head = int(ref[1:])
        assert tx.cell(row=head, column=1).value == ws.cell(row=r, column=1).value
        assert tx.cell(row=head, column=2).hyperlink.location == f"'To Label – Correspondence'!A{r}"
    # DOC-A: header row 2, one chunk on row 3; DOC-B's header follows on row 4
    assert ws.cell(row=3, column=c_open).hyperlink.location == "'Text – Correspondence'!A4"
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
    assert ws.cell(row=2, column=_col(ws, "Open text")).hyperlink.target == "gt_texts/correspondence/DOC-A.txt"
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
    assert labelled and labelled[0][1].startswith("=COUNTA('To Label – Correspondence'!A:A)-1")
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


# ---------------------------------------------------------------------------
# Fixes from review: revision pin, filing_number, derived fields, sanitising, text files


def _headers(ws):
    return [c.value for c in ws[1]]


def _col(ws, header):
    return _headers(ws).index(header) + 1


def _readme_text(wb):
    return "\n".join(str(c.value) for row in wb["README"].iter_rows() for c in row if c.value)


SHA = "c" * 40
STALE = "d" * 40


@pytest.fixture()
def fake_hub(tool, label_rows, tmp_path, monkeypatch):
    """A fake Hub: the API resolves only v9.3; ``resolve/<sha>`` serves the pinned parquet files and
    the viewer's ``/parquet`` listing (refs/convert/parquet) serves different, stale rows."""
    import io

    from pipeline import hf_corpus_loader as loader

    gt = label_rows.drop(columns=["doc_text", "metadata"])
    blind = label_rows[["filename", "doc_text", "metadata"]].assign(metadata=lambda d: d.metadata.map(json.dumps))
    stale_gt = gt.assign(expected_subclass="STALE")

    def parquet(frame):
        buf = io.BytesIO()
        frame.to_parquet(buf)
        return buf.getvalue()

    served = {}
    files = []
    for split in ("train", "test"):
        for config, frame in (("ground_truth", gt[gt.split == split]), ("default", blind[gt.split.values == split])):
            path = f"parquet/{config}/{split}/{split}-00000-of-00001.parquet"
            files.append(path)
            served[f"https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset/resolve/{SHA}/{path}"] = parquet(frame)
            served[f"https://stale/{config}/{split}.parquet"] = parquet(stale_gt[stale_gt.split == split])
    calls = []

    def get_json(url, **_):
        calls.append(url)
        if url.endswith("/api/datasets/Lucius-Morningstar/mailroom-dataset/revision/v9.3"):
            return {"sha": SHA, "siblings": [{"rfilename": f} for f in files + ["README.md"]]}
        if "/revision/" in url:
            raise RuntimeError("404 Client Error: Revision Not Found")
        if url.startswith("https://datasets-server.huggingface.co/parquet"):  # the stale refs/convert/parquet listing
            return {"parquet_files": [{"config": c, "split": s, "url": f"https://stale/{c}/{s}.parquet"}
                                      for c in ("ground_truth", "default") for s in ("train", "test")]}
        raise AssertionError(f"unexpected GET {url}")

    def get_bytes(url, **_):
        calls.append(url)
        return served[url]

    monkeypatch.setattr(loader, "_http_get_json", get_json)
    monkeypatch.setattr(loader, "_http_get_bytes", get_bytes)
    monkeypatch.setenv("MAILROOM_HF_CACHE_DIR", str(tmp_path / "hf_cache"))
    return calls


def test_revision_is_resolved_and_files_come_from_that_commit(tool, fake_hub, tmp_path, capsys):
    out = tmp_path / "hub.xlsx"
    assert tool.main(["--out", str(out), "--revision", "v9.3", "--to-label", "correspondence"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["resolved"] == SHA and stats["revision"] == "v9.3"
    wb = load_workbook(out)
    assert f"commit {SHA}" in _readme_text(wb)
    assert not any("datasets-server" in u or "stale" in u for u in fake_hub)
    assert any(f"/resolve/{SHA}/parquet/ground_truth/train/" in u for u in fake_hub)
    ws = wb["Correspondence"]
    assert {ws.cell(row=r, column=_col(ws, "expected_subclass")).value for r in range(2, ws.max_row + 1)} <= {
        "email", "memo"}  # the pinned rows, not the STALE ones
    assert stats["to_label"]["To Label – Correspondence"] == 2


def test_an_unknown_revision_fails_instead_of_building(tool, fake_hub, tmp_path, capsys):
    out = tmp_path / "bad.xlsx"
    with pytest.raises(SystemExit) as exc:
        tool.main(["--out", str(out), "--revision", "v99.99", "--to-label", "none"])
    assert exc.value.code not in (0, None)
    assert not out.exists()
    assert not any("resolve/" in u for u in fake_hub)  # nothing was downloaded


def test_corporate_filing_number_follows_the_dojo_unlabeled_fields(tool, tmp_path):
    from llm_dojo_scoring.schemas.corporate_record_schema import ENTITY_CORE_FIELDS, subclass_schema, unlabeled_fields

    base = _full_gt(tool, "corporate_record")
    cases = {
        "ART-1": ("articles_of_incorporation", {**base, "gt_presence": "{}"}),
        "ART-2": ("articles_of_incorporation", {**base, "filing_number": "12345", "gt_presence": "{}"}),
        "ART-3": ("articles_of_incorporation", {**base, "gt_presence": json.dumps({"filing_number": "schema_documented_absence"})}),
        "BYL-1": ("bylaws", {**base, "gt_presence": json.dumps({"filing_number": "not_applicable"})}),
        "BYL-2": ("bylaws", {**base, "gt_presence": json.dumps({"filing_number": "pending_annotation"})}),
        "OTH-1": ("other", {"gt_presence": "{}"}),
    }
    rows = pd.DataFrame([_doc(tool, i, "corporate_record", sc, "text", gt) for i, (sc, gt) in cases.items()])
    _, _, wb = _build(tool, rows, tmp_path, to_label=("corporate_record",))
    cols = tool.class_columns("corporate_record", rows)
    assert dict((k, t) for k, _, t in cols)["filing_number"] == "id"
    ws = wb["Corporate Records"]
    headers = _headers(ws)
    assert headers.index("jurisdiction") + 2 == headers.index("filing_number") < headers.index("intent")
    by_id = {ws.cell(row=r, column=1).value: r for r in range(2, ws.max_row + 1)}
    status = lambda doc: ws.cell(row=by_id[doc], column=_col(ws, "filing_number") + 1).value  # noqa: E731
    assert status("ART-1") == "UNLABELED" and status("ART-2") == "populated"
    assert status("ART-3") == "schema_documented_absence" and status("BYL-1") == "not_applicable"
    assert status("BYL-2") == "pending_annotation"
    # the workbook's missing set equals the Dojo's unlabeled_fields, row by row (core entity fields + entities)
    scored = set(ENTITY_CORE_FIELDS) | {e.key for sc in {c[0] for c in cases.values()} for e in subclass_schema(sc).entities}
    for doc, (sc, gt) in cases.items():
        presence = json.loads(gt["gt_presence"])
        mine = {k for k, _, _ in cols if k in scored
                and tool.cell_for("corporate_record", k, gt, presence, sc)[1] in tool.MISSING}
        assert mine == set(unlabeled_fields(sc, gt)) & {k for k, _, _ in cols}, doc
    to_label = wb["To Label – Corporate Records"]
    assert "filing_number" in _headers(to_label)


def test_communication_type_is_derived_from_the_subclass_not_listed_as_missing(tool, tmp_path):
    gt = _full_gt(tool, "correspondence")
    del gt["communication_type"]
    gt["gt_presence"] = json.dumps({k: "populated" for k in gt if k != "gt_presence"})
    gt["communication_date"] = "March 3, 2001"
    rows = pd.DataFrame([_doc(tool, "DOC-M", "correspondence", "memo", "text", gt)])
    assert tool.cell_for("correspondence", "communication_type", gt, json.loads(gt["gt_presence"]), "memo") == (
        "memo", "populated")
    _, stats, wb = _build(tool, rows, tmp_path, to_label=("correspondence",))
    assert stats["to_label"] == {}  # nothing else is missing, so no row is listed
    ws = wb["Correspondence"]
    assert ws.cell(row=2, column=_col(ws, "communication_type")).value == "memo"
    assert ws.cell(row=2, column=_col(ws, "communication_type") + 1).value == "populated"
    # a dataset value, when there is one, wins over the derived one
    assert tool.cell_for("correspondence", "communication_type", {"communication_type": "letter"}, {}, "memo") == (
        "letter", "populated")


def test_sanitize_text_replaces_every_character_xml_cannot_hold(tool, label_rows, tmp_path):
    text = "a￾b￿c\ud800d\udfffe\x00f\U0001F600"
    safe, replaced = tool.sanitize_text(text)
    assert replaced == 5 and len(safe) == len(text)
    assert safe == "a�b�c�d�e␀f\U0001F600"
    rows = label_rows.astype({"doc_text": object})  # arrow-backed strings cannot hold a lone surrogate
    rows.loc[rows.document_id == "DOC-A", "doc_text"] = text
    _, stats, wb = _build(tool, rows, tmp_path)  # must save and re-open
    assert stats["text_controls_replaced"] >= 5
    assert wb["Text – Correspondence"].cell(row=3, column=3).value == safe


def test_external_text_files_never_leave_the_texts_folder(tool, label_rows, tmp_path):
    from urllib.parse import unquote

    rows = label_rows.copy()
    rows.loc[rows.document_id == "DOC-A", "document_id"] = "../../escape"
    rows.loc[rows.document_id == "DOC-B", "document_id"] = "a/b\\c: d"
    out, stats, wb = _build(tool, rows, tmp_path, max_workbook_mb=0.00001)
    folder = tmp_path / "gt_texts"
    written = {p for p in tmp_path.rglob("*") if p.is_file()} - {out}
    assert written and all(folder in p.parents for p in written), sorted(map(str, written))
    ws = wb["To Label – Correspondence"]
    links = [ws.cell(row=r, column=_col(ws, "Open text")).hyperlink.target for r in range(2, ws.max_row + 1)]
    for target in links:
        assert (tmp_path / unquote(target)).is_file() and folder in (tmp_path / unquote(target)).resolve().parents
    readme = _readme_text(wb)
    assert "gt_texts" in readme and "'Text – ...' sheets" not in readme and "parts 1/n" not in readme
    assert "back to To Label" not in readme


# ---------------------------------------------------------------------------
# Drop-downs, named ranges, green highlight, live formulas


def _list_values(wb, name):
    """The values a named range points at (the hidden 'Lists' sheet)."""
    sheet, rng = wb.defined_names[name].attr_text.split("!")
    return [c.value for row in wb[sheet][rng.replace("$", "")] for c in row]


def _validations(ws):
    """formula1 -> the cells (as 'A2:A9' strings) of every data validation on ``ws``."""
    out = {}
    for dv in ws.data_validations.dataValidation:
        out.setdefault(dv.formula1, []).extend(str(r) for r in dv.sqref.ranges)
    return out


def _dv_covers(ranges, ws, column, rows):
    letter = ws.cell(row=1, column=column).column_letter
    return (f"{letter}2:{letter}{rows + 1}" if rows > 1 else f"{letter}2") in ranges


def test_lists_are_a_hidden_sheet_with_named_ranges_from_the_dojo(tool, label_rows, tmp_path):
    from llm_dojo_scoring.schemas.corporate_record_schema import RECORD_TYPES
    from llm_dojo_scoring.schemas.extraction_schemas import extraction_schema, extraction_subtypes

    _, stats, wb = _build(tool, label_rows, tmp_path)
    assert wb["Lists"].sheet_state == "hidden" and wb.sheetnames[-1] == "Lists"
    assert _list_values(wb, "lst_status") == list(tool.STATUSES) and "verified" in tool.STATUSES
    for cls in ("correspondence", "insurance_claim", "corporate_record"):
        enums = {n: [v for v in s["enum"] if v is not None] for n, s in
                 extraction_schema(cls).json_schema()["properties"].items() if s.get("enum")}
        assert enums
        for field, values in enums.items():
            assert _list_values(wb, f"lst_enum_{cls}_{field}") == values  # never typed by hand
        sub = _list_values(wb, f"lst_subclass_{cls}")
        assert sub[:len(extraction_subtypes(cls))] == list(extraction_subtypes(cls))
    assert _list_values(wb, "lst_enum_corporate_record_record_type") == list(RECORD_TYPES)
    assert set(stats["lists"]) == {n for n in wb.defined_names}


def test_every_status_and_closed_vocabulary_field_has_a_dropdown(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["Correspondence"]
    n = ws.max_row - 1
    dvs = _validations(ws)
    headers = _headers(ws)
    statuses = [i + 1 for i, h in enumerate(headers) if h.endswith(" | status")]
    assert statuses and all(_dv_covers(dvs["lst_status"], ws, c, n) for c in statuses)
    for field in ("communication_type", "urgency", "intent"):
        assert _dv_covers(dvs[f"lst_enum_correspondence_{field}"], ws, _col(ws, field), n), field
    assert _dv_covers(dvs["lst_subclass_correspondence"], ws, _col(ws, "expected_subclass"), n)
    assert all(dv.type == "list" and dv.showErrorMessage for dv in ws.data_validations.dataValidation)
    ins = wb["Insurance Claims"]
    dvs = _validations(ins)
    for field in ("claim_type", "coverage_determination", "intent", "cms_source_table"):
        assert _dv_covers(dvs[f"lst_enum_insurance_claim_{field}"], ins, _col(ins, field), ins.max_row - 1), field
    corp = wb["Corporate Records"]
    assert _dv_covers(_validations(corp)["lst_enum_corporate_record_record_type"], corp,
                      _col(corp, "record_type"), corp.max_row - 1)


def test_to_label_sheets_have_status_subclass_and_field_dropdowns_prefilled(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["To Label – Correspondence"]
    n = ws.max_row - 1
    dvs = _validations(ws)
    assert _dv_covers(dvs["lst_status"], ws, _col(ws, "communication_date | status"), n)
    assert _dv_covers(dvs["lst_subclass_correspondence"], ws, _col(ws, "subclass"), n)
    assert [ws.cell(row=r, column=_col(ws, "subclass")).value for r in (2, 3)] == ["email", "memo"]
    assert "email" in _list_values(wb, "lst_subclass_correspondence")
    assert _validations(wb["To Label – Insurance Claims"]).keys() >= {"lst_status", "lst_subclass_insurance_claim"}


def test_labels_a_person_adds_turn_green_and_dataset_values_do_not(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    for name, fields in (("To Label – Correspondence", ["communication_date"]), ("Correspondence", None)):
        ws = wb[name]
        rules = [(str(rng.sqref), r) for rng in ws.conditional_formatting for r in rng.rules]
        assert rules and all(r.type == "expression" and r.dxf.fill.bgColor.rgb.endswith(tool.DONE_RGB) for _, r in rules)
        pair = f"{ws.cell(row=2, column=_col(ws, 'communication_date')).column_letter}2:"
        assert any(sq.startswith(pair) for sq, _ in rules), "a rule covers the communication_date value+status pair"
        formulas = [r.formula[0] for _, r in rules]
        assert any('="verified"' in f and "MID($" in f and "LEN($" in f for f in formulas)
        sub = [r for sq, r in rules if sq.startswith(ws.cell(row=2, column=_col(ws, "subclass" if fields else "expected_subclass")).column_letter + "2:")]
        assert sub and "<>" in sub[0].formula[0]  # the subclass cell turns green when it differs from the dataset's
    # the mask records what the dataset lacked: only those cells can turn green
    ws = wb["To Label – Correspondence"]
    mask = ws.cell(row=2, column=_col(ws, "needs mask (helper)")).value
    assert mask == "1" and ws.cell(row=2, column=_col(ws, "dataset subclass (helper)")).value == "email"
    ins = wb["To Label – Insurance Claims"]
    assert ins.cell(row=2, column=_col(ins, "needs mask (helper)")).value == "2"  # populated, but a yearless date
    assert tool._green_formula(3, "F", "G", "L").count("MID($L2,3,1)") == 3


def test_live_counts_agree_with_the_built_counts_and_use_only_portable_functions(tool, label_rows, tmp_path):
    import re

    _, _, wb = _build(tool, label_rows, tmp_path)
    for name in ("To Label – Correspondence", "To Label – Insurance Claims"):
        ws = wb[name]
        built, live, mask = (_col(ws, h) for h in ("fields needing labels (as built)", "still missing (live)",
                                                  "needs mask (helper)"))
        for r in range(2, ws.max_row + 1):
            assert ws.cell(row=r, column=built).value == sum(ch != "0" for ch in ws.cell(row=r, column=mask).value)
            f = ws.cell(row=r, column=live).value
            assert f.startswith("=SUMPRODUCT(") and f"UNLABELED" in f
    # a flagged date still 'populated' is counted until a person verifies it
    ins = wb["To Label – Insurance Claims"]
    live_f = ins.cell(row=2, column=_col(ins, "still missing (live)")).value
    status_letter = ins.cell(row=2, column=_col(ins, "date_of_loss | status")).column_letter
    assert f'IF({status_letter}2="populated",1,0)' in live_f
    everything = " ".join(str(c.value) for w in wb for row in w.iter_rows() for c in row
                          if isinstance(c.value, str) and c.value.startswith("="))
    used = set(re.findall(r"([A-Z]+)\(", everything))
    assert used <= {"SUMPRODUCT", "MOD", "COLUMN", "IF", "COUNTIF", "COUNTA", "SUM", "MAX"}, used
    assert "INDIRECT" not in everything and "OFFSET" not in everything  # no volatile or fragile references


def test_summary_counts_status_cells_and_to_label_progress(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    cells = {c.coordinate: c.value for row in wb["Summary"].iter_rows() for c in row if c.value is not None}
    header = [cells[f"{get_col}4"] for get_col in "ABCDEFGHIJK"]
    assert header[3:9] == list(tool.STATUSES)
    assert cells["A5"] == "Contracts" or cells["B5"].startswith("=COUNTA('")
    docs = [v for k, v in cells.items() if k.startswith("B") and str(v).startswith("=COUNTA(")]
    assert docs and all(v.endswith("!A:A)-1") for v in docs)  # whole-column count, no hard-wired row extent
    assert all("SUMPRODUCT(" in v for k, v in cells.items()
               if k[0] in "DEFGHI" and k[1:].isdigit() and 5 <= int(k[1:]) <= 7)
    assert any(v == "Documents with nothing left" for v in cells.values())


def test_readme_records_the_resolved_commit_and_the_legend(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    readme = _readme_text(wb)
    assert f"revision v0 = commit {COMMIT}" in readme
    fills = {str(c.fill.fgColor.rgb) for c in wb["README"]["A"] if c.fill and c.fill.fill_type}
    assert {tool.TODO.fgColor.rgb, tool.DONE.fgColor.rgb} <= fills  # yellow and green legend rows
    assert "FILTER" in readme and "do not SORT" in readme.replace("do not sort", "do not SORT")
    assert "verified" in readme and "Open original" in readme
    out = tmp_path / "offline.xlsx"
    tool.build_workbook(label_rows, out, dataset=DATASET, revision="v0", to_label=("correspondence",))
    assert "commit was not resolved" in _readme_text(load_workbook(out))


def test_text_sheet_has_part_labels_and_a_filter_that_ends_at_the_data(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    tx = wb["Text – Correspondence"]
    assert tx.auto_filter.ref == f"A1:C{tx.max_row}"
    assert tx.cell(row=3, column=2).value == "part 1/1"
    assert tx.column_dimensions["C"].width >= 100 and tx.cell(row=3, column=3).alignment.wrap_text


def test_viewer_link_searches_the_filename(tool, label_rows, tmp_path):
    _, _, wb = _build(tool, label_rows, tmp_path)
    ws = wb["Insurance Claims"]
    target = ws.cell(row=2, column=7).hyperlink.target
    assert target == f"https://huggingface.co/datasets/{DATASET}/viewer/default/train?q=DOC-D.txt"
    assert "document_id" not in _readme_text(wb).split("'Hub viewer'")[1][:200]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_libreoffice_recalculates_the_counts_after_edits_with_no_errors(tool, label_rows, tmp_path):
    out, _, _ = _build(tool, label_rows, tmp_path)
    wb = load_workbook(out)
    ws = wb["To Label – Correspondence"]
    cd = _col(ws, "communication_date")
    ws.cell(row=2, column=cd).value = "March 3, 2001"
    ws.cell(row=2, column=cd + 1).value = "populated"          # DOC-A: gap filled
    ws.cell(row=3, column=cd + 1).value = "schema_documented_absence"  # DOC-B: documented absence
    ins = wb["To Label – Insurance Claims"]
    ins.cell(row=2, column=_col(ins, "date_of_loss | status")).value = "verified"  # flagged date checked
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)

    def recalculated(path, name):
        outdir = tmp_path / name
        subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp_path}/lo-{name}", "--headless",
                        "--convert-to", "xlsx", "--outdir", str(outdir), str(path)],
                       check=True, capture_output=True, timeout=240)
        return load_workbook(outdir / path.name, data_only=True)

    before, after = recalculated(out, "before"), recalculated(edited, "after")
    for book in (before, after):
        errors = [(w.title, c.coordinate, c.value) for w in book for row in w.iter_rows() for c in row
                  if isinstance(c.value, str) and (c.value.startswith("#") or c.value.startswith("Err:"))]
        assert errors == []

    def live(book, name):
        w = book[name]
        col = _col(w, "still missing (live)")
        return [w.cell(row=r, column=col).value for r in range(2, w.max_row + 1)]

    assert live(before, "To Label – Correspondence") == [1, 1]
    assert live(after, "To Label – Correspondence") == [0, 0]
    assert live(before, "To Label – Insurance Claims") == [1]   # the yearless date counts while 'populated'
    assert live(after, "To Label – Insurance Claims") == [0]
    summary = [[c.value for c in r] for r in after["Summary"].iter_rows()]
    corr = next(r for r in summary if r[0] == "To Label – Correspondence")
    assert corr[1:6] == [2, 2, 0, 2, 2]  # documents, as built, still missing, resolved, nothing left


def test_insurance_claim_checklist_is_not_a_labelling_target(tool, tmp_path):
    """claim_checklist is a pipeline output the dataset never labels: no column on any sheet,
    so it never puts every claim on the To Label sheet."""
    gt = _full_gt(tool, "insurance_claim")
    gt.pop("claim_checklist", None)
    presence = json.loads(gt["gt_presence"])
    presence.pop("claim_checklist", None)
    gt["gt_presence"] = json.dumps(presence)
    rows = pd.DataFrame([_doc(tool, "DOC-I", "insurance_claim", "carrier", "Claim text.", gt)])
    _, stats, wb = _build(tool, rows, tmp_path, to_label=("insurance_claim",))
    assert "claim_checklist" in stats["dropped_fields"]["insurance_claim"]
    for name in wb.sheetnames:
        assert "claim_checklist" not in _headers(wb[name]), name
    assert stats["to_label"].get("To Label – Insurance Claims", 0) == 0
    assert "claim_checklist" in _readme_text(wb)
