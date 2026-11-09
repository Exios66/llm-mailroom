"""Live manifest shape/selection contracts (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.live_manifest import (
    CLASSES,
    MANIFEST_COLUMNS,
    load_manifest,
    parse_release_manifest,
    select_smoke_rows,
    write_manifest_csv,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_manifest_sample.csv"


def test_committed_live_manifest_fixture_is_valid():
    rows = load_manifest(FIXTURE)
    assert len(rows) == 15
    assert {row["expected_doc_class"] for row in rows} == set(CLASSES)
    for row in rows:
        assert row["hub_revision"] == "v9.3"
        assert len(row["hub_sha"]) == 40
        assert len(row["content_sha256"]) == 64
        assert row["expected_stage"] in ("archived", "review", "failed")
        assert json.loads(row["expected_fields"]) == json.loads(row["expected_fields"])


def test_selection_is_deterministic_and_stratified():
    rows = load_manifest(FIXTURE)
    first = select_smoke_rows(rows, per_class=1, max_chars=250_000)
    second = select_smoke_rows(rows, per_class=1, max_chars=250_000)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert {row["expected_doc_class"] for row in first} == set(CLASSES)
    for row in first:
        assert int(row["chars"]) >= 200
        assert row["expected_stage"] == "archived"


def test_smallest_document_wins_within_a_class():
    rows = load_manifest(FIXTURE)
    contracts = select_smoke_rows(
        [row for row in rows if row["expected_doc_class"] == "contract"],
        per_class=1,
        classes=("contract",),
        max_chars=250_000,
    )
    assert contracts[0]["id"] == "DOC-a325abdfb361337c"


def test_manifest_writer_round_trips_rows(tmp_path):
    rows = load_manifest(FIXTURE)[:2]
    out = tmp_path / "manifest.csv"
    payload = write_manifest_csv(rows, out, {"dataset": "test", "revision": {"tag": "v9.3"}})
    assert payload["rows"] == 2
    assert load_manifest(out) == rows
    assert out.with_suffix(".provenance.json").is_file()
    assert set(rows[0]) == set(MANIFEST_COLUMNS)


def test_manifest_rejects_unknown_class(tmp_path):
    rows = load_manifest(FIXTURE)
    rows[0]["expected_doc_class"] = "court_opinion"
    out = tmp_path / "bad.csv"
    write_manifest_csv(rows, out, {})
    with pytest.raises(ValueError, match="unknown class"):
        load_manifest(out)


def test_release_manifest_parser_reads_path_sha_pairs():
    parsed = parse_release_manifest(
        "header\n  parquet/default/train/x.parquet  " + "a" * 64 + "\n"
    )
    assert parsed["parquet/default/train/x.parquet"] == "a" * 64
    with pytest.raises(RuntimeError):
        parse_release_manifest("no checksums here")
