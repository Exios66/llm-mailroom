"""Behavioral regressions for the final runtime PR review objections."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("contents", ["{", '{"invalid": true}'])
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


@pytest.mark.parametrize("mask", [0o022, 0o077])
@pytest.mark.parametrize("existing_mode", [None, 0o640])
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
