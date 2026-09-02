"""Parquet warehouse export — daily files + manifest watermark."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def warehouse_env(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "1")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    for mod in ("storage.db", "storage.catalog", "storage.audit_log", "storage.warehouse"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    pyarrow = pytest.importorskip("pyarrow")
    return pyarrow


def _seed_terminal_doc(temp_base_dir, *, doc_id="wh-doc-1", stage="archived"):
    import asyncio

    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry
    from storage.catalog import write_document_record
    from storage.db import ensure_schema

    ensure_schema()
    asyncio.run(
        write_document_record(
            {
                "doc_id": doc_id,
                "matter_id": "M-WH",
                "original_filename": "sample.txt",
                "stage": stage,
                "doc_type": "contract",
                "classification_confidence": 0.91,
                "extraction_confidence": 0.88,
                "extracted_data": {"parties": ["Acme"]},
                "scores": {"success_rate": 1},
                "trace_id": "trace-wh",
            }
        )
    )
    entry = build_audit_entry(
        doc_id=doc_id,
        matter_id="M-WH",
        event="archived",
        actor="pipeline",
        detail={"stage": stage},
        prev_hash="",
    )
    asyncio.run(write_audit_entry(entry))


def test_export_creates_daily_parquet_and_manifest(warehouse_env, temp_base_dir):
    import asyncio

    from storage.warehouse import (
        daily_audit_path,
        daily_documents_path,
        export_to_warehouse,
        load_warehouse_manifest,
        manifest_file,
    )

    _seed_terminal_doc(temp_base_dir)
    stamp = date(2026, 8, 27)
    result = asyncio.run(export_to_warehouse(stamp=stamp, full=True))
    assert result["status"] == "ok"
    assert result["exported_documents"] == 1
    assert result["exported_audit_entries"] >= 1

    doc_path = daily_documents_path(stamp)
    audit_path = daily_audit_path(stamp)
    assert doc_path.is_file()
    assert audit_path.is_file()
    assert manifest_file().is_file()

    manifest = load_warehouse_manifest()
    assert manifest["schema_version"] == "1"
    assert manifest["last_export_at"]
    assert "2026-08-27" in manifest["daily_files"]
    assert manifest["daily_files"]["2026-08-27"]["document_count"] == 1

    docs = warehouse_env.parquet.read_table(doc_path).to_pylist()
    assert docs[0]["doc_id"] == "wh-doc-1"
    assert docs[0]["stage"] == "archived"
    assert json.loads(docs[0]["extracted_data_json"])["parties"] == ["Acme"]


def test_export_merges_same_day_batches(warehouse_env, temp_base_dir):
    import asyncio

    from storage.warehouse import daily_documents_path, export_to_warehouse

    _seed_terminal_doc(temp_base_dir, doc_id="wh-doc-a")
    stamp = date(2026, 8, 28)
    asyncio.run(export_to_warehouse(doc_ids=["wh-doc-a"], stamp=stamp))

    _seed_terminal_doc(temp_base_dir, doc_id="wh-doc-b")
    asyncio.run(export_to_warehouse(doc_ids=["wh-doc-b"], stamp=stamp))

    rows = warehouse_env.parquet.read_table(daily_documents_path(stamp)).to_pylist()
    ids = {r["doc_id"] for r in rows}
    assert ids == {"wh-doc-a", "wh-doc-b"}


def test_routine_export_document_sync(warehouse_env, temp_base_dir):
    from storage.warehouse import daily_documents_path, export_document_to_warehouse

    _seed_terminal_doc(temp_base_dir, doc_id="wh-routine")
    assert export_document_to_warehouse("wh-routine") is True
    # The routine export stamps by UTC date (V-6/V-7 convention) — a local
    # date.today() lags UTC on west-of-UTC machines in the evening.
    stamp = datetime.now(timezone.utc).date()
    path = daily_documents_path(stamp)
    assert path.is_file()
    rows = warehouse_env.parquet.read_table(path).to_pylist()
    assert any(r["doc_id"] == "wh-routine" for r in rows)


def test_export_cli_json(warehouse_env, temp_base_dir, capsys):
    _seed_terminal_doc(temp_base_dir, doc_id="wh-cli")
    from scripts.export_warehouse import main

    sys.argv = ["export_warehouse.py", "--full", "--json"]
    assert main() == 0
    out = capsys.readouterr().out.strip()
    # Prefer the last JSON object on stdout (structlog noise may precede it
    # when LOG_LEVEL was already configured by an earlier test).
    start = out.rfind("\n{")
    if start >= 0:
        start += 1
    else:
        start = out.find("{")
    assert start >= 0, out
    payload = json.loads(out[start:])
    assert payload["exported_documents"] >= 1
