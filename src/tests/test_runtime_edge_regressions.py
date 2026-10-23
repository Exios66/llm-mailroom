"""Real client and database/Arrow regressions from the DMR-091 postfix audit."""

import asyncio
from datetime import date
import threading

import httpx
import pytest


@pytest.mark.no_langchain_mock
@pytest.mark.parametrize("provider_key", [None, ""])
@pytest.mark.parametrize("ambient_key", [None, "sentinel-unrelated-openai"])
@pytest.mark.parametrize("explicit_key", [None, "sentinel-explicit"])
def test_keyless_vllm_never_inherits_unrelated_sdk_key(monkeypatch, provider_key, ambient_key, explicit_key):
    """The actual resolver and SDK constructor send only the selected credential."""
    from langchain_agents.base_agent import BaseAgent
    from llm import providers

    class Agent(BaseAgent):
        """Concrete client-only fixture; provider HTTP is the sole fake boundary."""
        agent_name = "sorter"

        def system_prompt(self):
            return "fixture"

    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://fixture.invalid/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-unrelated-openrouter")
    for name, value in [("VLLM_API_KEY", provider_key), ("OPENAI_API_KEY", ambient_key)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(providers, "_providers_cache", None)
    llm = Agent(api_key=explicit_key).llm()
    wire = []

    def respond(request):
        wire.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        llm.root_client = llm.root_client.with_options(http_client=client)
        llm.client = llm.root_client.chat.completions
        assert llm.invoke("fixture").content == "ok"
    assert wire == [("http://fixture.invalid/v1/chat/completions", "Bearer " + (explicit_key or "not-needed"))]


def test_parquet_reader_keeps_one_inode_during_atomic_replacement(tmp_path, monkeypatch):
    """Metadata and data pages must use the same snapshot during a real upsert."""
    import pyarrow.parquet.core as core
    from storage import warehouse

    path = tmp_path / "documents.parquet"
    warehouse.write_parquet_rows(path, [{"doc_id": "seed"}], merge_key="doc_id")
    original = core.ParquetDataset.read
    started, published = threading.Event(), threading.Event()
    rows, errors = [], []

    def pause_after_metadata(dataset, *args, **kwargs):
        if threading.current_thread().name == "snapshot-reader":
            for fragment in dataset.fragments:
                _ = fragment.metadata
            started.set()
            assert published.wait(5)
        return original(dataset, *args, **kwargs)

    def read_snapshot():
        try:
            rows.extend(warehouse._read_parquet_rows(path))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(core.ParquetDataset, "read", pause_after_metadata)
    reader = threading.Thread(target=read_snapshot, name="snapshot-reader")
    reader.start()
    assert started.wait(5)
    try:
        warehouse.write_parquet_rows(path, [{"doc_id": "a much longer second document id"}], merge_key="doc_id")
    finally:
        published.set()
        reader.join(5)
    assert not reader.is_alive()
    assert errors == []
    assert rows == [{"doc_id": "seed"}]
    assert len(warehouse._read_parquet_rows(path)) == 2


def test_public_warehouse_export_retains_audit_document_join_key(temp_base_dir, monkeypatch):
    """Actual catalog and audit rows join after the public small-batch export."""
    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry
    from storage.catalog import write_document_record
    from storage import warehouse

    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "1")
    entry = build_audit_entry(doc_id="AUDIT-DOC", matter_id="M", event="archived",
                              actor="fixture", detail={}, prev_hash="")

    async def publish():
        await write_document_record({"doc_id": "AUDIT-DOC", "matter_id": "M",
            "original_filename": "a.txt", "stage": "archived", "doc_type": "contract"})
        await write_audit_entry(entry)
        return await warehouse.export_to_warehouse(doc_ids=["AUDIT-DOC"], stamp=date(2026, 10, 23))

    result = asyncio.run(publish())
    assert result["exported_audit_entries"] == 1
    docs = warehouse._read_parquet_rows(warehouse.daily_documents_path(date(2026, 10, 23)))
    audit = warehouse._read_parquet_rows(warehouse.daily_audit_path(date(2026, 10, 23)))
    assert docs[0]["doc_id"] == audit[0]["doc_id"] == "AUDIT-DOC"
    assert audit[0]["entry_id"] == str(entry.entry_id)
