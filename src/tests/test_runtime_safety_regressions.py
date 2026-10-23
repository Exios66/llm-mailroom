"""Source-owned identity, provider and publication safety regressions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import os
import subprocess
import sys

import pytest


def test_same_named_review_documents_keep_their_own_bytes(temp_base_dir):
    """Parking another doc ID cannot turn a name collision into an upsert."""
    from pipeline.bins import park_for_review
    from pipeline.review_resolve import locate_document_file
    from schemas.manifest import DocumentManifest, PipelineStage

    parked = []
    for doc_id, text in [("FIRST", "FIRST LEGAL DOCUMENT"), ("SECOND", "SECOND LEGAL DOCUMENT")]:
        source = temp_base_dir / doc_id / "same.txt"
        source.parent.mkdir()
        source.write_text(text)
        manifest = DocumentManifest(doc_id=doc_id, matter_id="M", original_filename="same.txt",
                                    stage=PipelineStage.REVIEW)
        path, newly = park_for_review(source, manifest)
        assert newly and path.read_text() == text
        assert locate_document_file(manifest).read_text() == text
        replay, newly = park_for_review(source, manifest)
        assert not newly and replay == path
        parked.append((manifest, path, text))
    assert parked[0][1] != parked[1][1]
    for manifest, _, text in parked:
        assert locate_document_file(manifest).read_text() == text


@pytest.mark.parametrize("matter", ["../escape", "absolute", "a/b", "a\\b", "..", ".", "C:\\escape", "C:escape"])
def test_archive_refuses_path_bearing_matter_ids(temp_base_dir, matter):
    """Untrusted matter IDs never become filesystem paths outside the archive."""
    from pipeline.bins import move_to_archive

    source = temp_base_dir / "input.txt"
    source.write_text("legal document")
    if matter == "absolute":
        matter = str(temp_base_dir / "outside-archive")
    with pytest.raises(ValueError, match="matter_id"):
        move_to_archive(source, matter, "contract", "DOC")
    assert source.read_text() == "legal document"


@pytest.mark.no_langchain_mock
@pytest.mark.parametrize("provider,environment,expected", [
    ("vllm", "VLLM_API_KEY", "sentinel-provider"),
    ("litellm", "LITELLM_API_KEY", "sentinel-provider"),
    ("ollama", None, "not-needed"),
])
def test_langchain_credentials_follow_resolved_provider(monkeypatch, provider, environment, expected):
    """Only an explicit constructor override may replace a provider's credential."""
    from langchain_agents import base_agent
    from llm.providers import ProviderConfig
    from llm.client import ResolvedModel

    class Agent(base_agent.BaseAgent):
        agent_name = "sorter"

        def system_prompt(self):
            return "fixture"

    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-openrouter")
    if environment:
        monkeypatch.setenv(environment, "sentinel-provider")
    config = ProviderConfig(name=provider, base_url="http://audit.invalid/v1", api_key_env=environment,
                            default_model="fixture")
    # The provider resolver is the external configuration boundary here;
    # the real BaseAgent constructor/property select and forward the key.
    monkeypatch.setattr("llm.client.resolve_agent_model", lambda _: ResolvedModel(config, model="fixture", champion="fixture"))
    monkeypatch.setattr(base_agent, "ChatOpenAI", lambda **kwargs: kwargs)
    assert Agent().llm()["api_key"] == expected
    assert Agent(api_key="explicit-override").llm()["api_key"] == "explicit-override"


def test_overlapping_parquet_upserts_keep_both_documents(tmp_path, monkeypatch):
    """Force two legitimate old snapshots while keeping real Arrow writes valid."""
    from storage import warehouse

    path = tmp_path / "documents.parquet"
    read = warehouse._read_parquet_rows
    barrier = threading.Barrier(2)
    _, pq = warehouse._require_pyarrow()
    write = pq.write_table
    writer_lock = threading.Lock()

    def simultaneous_read(target):
        snapshot = read(target)
        try:
            barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
        return snapshot

    def serial_arrow_write(*args, **kwargs):
        with writer_lock:
            return write(*args, **kwargs)

    monkeypatch.setattr(warehouse, "_read_parquet_rows", simultaneous_read)
    monkeypatch.setattr(pq, "write_table", serial_arrow_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(warehouse.write_parquet_rows, path, [{"doc_id": value}], merge_key="doc_id")
                   for value in ("A", "B")]
        for future in futures:
            future.result(timeout=5)
    assert {row["doc_id"] for row in read(path)} == {"A", "B"}


def test_failed_parquet_publication_preserves_previous_file(tmp_path, monkeypatch):
    """A failed replacement cannot truncate the last complete Parquet snapshot."""
    from storage import warehouse

    path = tmp_path / "documents.parquet"
    warehouse.write_parquet_rows(path, [{"doc_id": "A"}], merge_key="doc_id")
    _, pq = warehouse._require_pyarrow()

    def fail_after_partial_write(table, target, **kwargs):
        Path(target).write_bytes(b"torn replacement")
        raise OSError("fixture disk failure")

    monkeypatch.setattr(pq, "write_table", fail_after_partial_write)
    with pytest.raises(OSError, match="fixture disk failure"):
        warehouse.write_parquet_rows(path, [{"doc_id": "B"}], merge_key="doc_id")
    assert warehouse._read_parquet_rows(path) == [{"doc_id": "A"}]


def test_parquet_upserts_serialize_independent_processes(tmp_path):
    """A process-local lock alone cannot protect the shared warehouse."""
    from storage import warehouse

    code = """import sys,time
from pathlib import Path
from storage.warehouse import write_parquet_rows
target,ready,prefix = Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
deadline=time.monotonic()+10
while not ready.exists():
    assert time.monotonic()<deadline
    time.sleep(.005)
for i in range(10):
    write_parquet_rows(target,[{'doc_id':prefix+str(i)}],merge_key='doc_id')
"""
    target, ready = tmp_path / "documents.parquet", tmp_path / "ready"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(target), str(ready), prefix],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for prefix in ("A", "B")]
    try:
        ready.touch()
        for process in processes:
            _, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert {row["doc_id"] for row in warehouse._read_parquet_rows(target)} == {
        prefix + str(i) for prefix in ("A", "B") for i in range(10)
    }


def test_export_publication_keeps_other_days_and_advancing_watermark(temp_base_dir, monkeypatch):
    """Overlapping export commits merge fresh manifests and never regress time."""
    from datetime import date, datetime, timezone
    import asyncio
    from storage import warehouse
    from storage.catalog import DocumentRecord

    newer = datetime(2026, 10, 23, tzinfo=timezone.utc)
    older = datetime(2026, 10, 22, tzinfo=timezone.utc)
    records = {f"D{day}": DocumentRecord(doc_id=f"D{day}", matter_id="M", original_filename="x.txt",
                                         stage="archived", updated_at=stamp)
               for day, stamp in ((23, newer), (22, older))}
    barrier = threading.Barrier(2)

    async def fetch_documents(*, doc_ids=None, since=None):
        # Both exporters have already loaded the old manifest when their
        # independently completing DB reads reach this barrier.
        barrier.wait(timeout=3)
        return [records[doc_id] for doc_id in doc_ids]

    async def fetch_audit(doc_ids):
        return [{"entry_id": "E" + doc_id, "doc_id": doc_id, "event": "archived"}
                for doc_id in doc_ids]

    monkeypatch.setattr(warehouse, "fetch_terminal_documents", fetch_documents)
    monkeypatch.setattr(warehouse, "fetch_audit_rows_for_doc_ids", fetch_audit)
    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "1")

    def export(day):
        return asyncio.run(warehouse.export_to_warehouse(doc_ids=[f"D{day}"], stamp=date(2026, 10, day)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(export, day) for day in (23, 22)]
        for future in futures:
            future.result(timeout=5)
    result = warehouse.load_warehouse_manifest()
    assert set(result["daily_files"]) == {"2026-10-22", "2026-10-23"}
    assert result["last_doc_updated_at"] == newer.isoformat()
