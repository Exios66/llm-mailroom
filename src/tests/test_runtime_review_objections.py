"""Behavioral regressions for the final runtime PR review objections."""

from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("contents", ["{", '{"invalid": true}', '[]', 'null', 'true', '"invalid"'])
def test_unreadable_manifest_does_not_expose_ambiguous_legacy_bytes(temp_base_dir, contents):
    """Unverifiable legacy ownership fails closed without an unrelated parse error."""
    from pipeline.bins import manifests_dir, review_dir, review_file_path
    from schemas.manifest import DocumentManifest, PipelineStage

    manifest = DocumentManifest(doc_id="OWNED", matter_id="M", original_filename="same.txt",
                                stage=PipelineStage.REVIEW)
    legacy = review_dir() / "same.txt"
    legacy.write_text("unverified legacy bytes")
    (manifests_dir() / "broken.json").write_text(contents)
    owned = review_dir() / "same--OWNED.txt"
    assert review_file_path(manifest) == owned
    assert not owned.exists()
    assert legacy.read_text() == "unverified legacy bytes"
    owned.write_text("owned bytes")
    assert review_file_path(manifest).read_text() == "owned bytes"


@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and umask semantics")
def test_replacement_contents_remain_private_before_publication(tmp_path, mode):
    """A real populated temporary cannot expose private rows before rename."""
    import pyarrow

    source = Path(__file__).resolve().parents[1]
    dependencies = Path(pyarrow.__file__).resolve().parents[1]
    program = '''
import os, pathlib, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
os.umask(0o022)
from storage import warehouse
assert pathlib.Path(warehouse.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]))
target = pathlib.Path(sys.argv[3]) / "documents.parquet"
mode = int(sys.argv[4])
warehouse.write_parquet_rows(target, [{"doc_id": "private-A"}], merge_key="doc_id")
target.chmod(mode)
_, pq = warehouse._require_pyarrow()
original = pq.write_table
observed = []
def write_and_observe(table, where, *args, **kwargs):
    """Observe real populated Arrow output before publication."""
    result = original(table, where, *args, **kwargs)
    path = pathlib.Path(where)
    observed.append((path.stat().st_mode & 0o777, warehouse._read_parquet_rows(path)))
    return result
pq.write_table = write_and_observe
warehouse.write_parquet_rows(target, [{"doc_id": "private-B"}], merge_key="doc_id")
assert observed and observed[0][0] & ~mode == 0, observed
assert {row["doc_id"] for row in observed[0][1]} == {"private-A", "private-B"}
assert target.stat().st_mode & 0o777 == mode
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(source), str(dependencies),
         str(tmp_path), str(mode)], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mask", [0o022, 0o077])
@pytest.mark.parametrize("existing_mode", [None, 0o640])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and umask semantics")
def test_atomic_publication_preserves_umask_and_existing_modes(tmp_path, mask, existing_mode):
    """Real Parquet and manifest files retain default or existing reader access."""
    import pyarrow

    source = Path(__file__).resolve().parents[1]
    dependencies = Path(pyarrow.__file__).resolve().parents[1]
    program = '''
import json, os, pathlib, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
os.environ["MAILROOM_BASE_DIR"] = sys.argv[3]
os.umask(int(sys.argv[4]))
from storage import warehouse
assert pathlib.Path(warehouse.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]))
data = pathlib.Path(sys.argv[3]) / "warehouse" / "documents.parquet"
manifest = warehouse.manifest_file()
warehouse.write_parquet_rows(data, [{"doc_id": "A"}], merge_key="doc_id")
warehouse.save_warehouse_manifest({"schema_version": "1"})
expected = 0o666 & ~int(sys.argv[4])
existing = json.loads(sys.argv[5])
if existing is not None:
    for path in (data, manifest): path.chmod(existing)
    warehouse.write_parquet_rows(data, [{"doc_id": "B"}], merge_key="doc_id")
    warehouse.save_warehouse_manifest({"schema_version": "1", "test": True})
    expected = existing
for path in (data, manifest):
    assert path.stat().st_mode & 0o777 == expected, (path, oct(path.stat().st_mode & 0o777), oct(expected))
assert not list(data.parent.glob("*.tmp"))
'''
    import json

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(source), str(dependencies),
         str(tmp_path), str(mask), json.dumps(existing_mode)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.parametrize("fail_publication", [False, True])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and umask semantics")
def test_preopened_public_temporary_never_receives_private_rows(tmp_path, mode, fail_publication):
    """An early read descriptor may observe only an always-empty mode probe."""
    import pyarrow

    source = Path(__file__).resolve().parents[1]
    dependencies = Path(pyarrow.__file__).resolve().parents[1]
    program = r''' 
import os, pathlib, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
os.umask(0o022)
from storage import warehouse
assert pathlib.Path(warehouse.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]))
target = pathlib.Path(sys.argv[3]) / "documents.parquet"
mode, fail = int(sys.argv[4]), sys.argv[5] == "True"
warehouse.write_parquet_rows(target, [{"doc_id": "A", "text": "OLD PRIVATE SECRET"}], merge_key="doc_id")
target.chmod(mode)
before = target.read_bytes()
original_open = os.open
_, pq = warehouse._require_pyarrow()
original_write = pq.write_table
retained, creation_modes = [], []
def open_and_retain(path, flags, *args, **kwargs):
    """Schedule a real reader immediately after exclusive inode creation."""
    fd = original_open(path, flags, *args, **kwargs)
    if flags & os.O_EXCL:
        permissions = os.fstat(fd).st_mode & 0o777
        creation_modes.append(permissions)
        if permissions & 0o044:
            retained.append(pathlib.Path(path).open("rb"))
    return fd
def write_and_inspect(table, where, *args, **kwargs):
    """Inspect every retained descriptor after real Arrow serialization."""
    result = original_write(table, where, *args, **kwargs)
    assert pathlib.Path(where).stat().st_mode & 0o777 == 0o600
    for handle in retained:
        handle.seek(0)
        assert handle.read() == b"", "an inode opened while public later received private rows"
    if fail:
        raise OSError("injected publication failure")
    return result
os.open, pq.write_table = open_and_retain, write_and_inspect
try:
    try:
        warehouse.write_parquet_rows(target, [{"doc_id": "B", "text": "NEW PRIVATE SECRET"}], merge_key="doc_id")
    except OSError as exc:
        assert fail and str(exc) == "injected publication failure"
    else:
        assert not fail
finally:
    os.open, pq.write_table = original_open, original_write
    for handle in retained: handle.close()
assert creation_modes and 0o600 in creation_modes, creation_modes
assert target.stat().st_mode & 0o777 == mode
if fail:
    assert target.read_bytes() == before
else:
    assert {r["doc_id"] for r in warehouse._read_parquet_rows(target)} == {"A", "B"}
assert not list(target.parent.glob(".*.tmp"))
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(source), str(dependencies),
         str(tmp_path), str(mode), str(fail_publication)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
