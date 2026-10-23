"""Build a real wheel and load its runtime taxonomy without checkout fallbacks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


def test_built_wheel_contains_and_loads_its_runtime_prompt_catalog(tmp_path):
    """An isolated wheel must resolve its taxonomy and all 18 frozen templates."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("wheel smoke test requires the documented uv build tool")
    root = Path(__file__).resolve().parents[2]
    dist = tmp_path / "dist"
    built = subprocess.run(
        [uv, "build", "--no-sources", "--offline", "--wheel", "--out-dir", str(dist), str(root)],
        text=True, capture_output=True, timeout=90,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheel = next(dist.glob("*.whl"))
    unpacked = tmp_path / "installed-wheel"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "config/taxonomy.yaml" in names
        assert archive.read("config/taxonomy.yaml") == (root / "src/config/taxonomy.yaml").read_bytes()
        assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in names)
        archive.extractall(unpacked)

    import llm_dojo_scoring

    dojo_root = Path(llm_dojo_scoring.__file__).resolve().parent.parent
    sites = [path for path in sys.path if Path(path).name in {"site-packages", "dist-packages"}]
    code = '''import json, pathlib, sys
wheel, dojo, sites = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path[:0] = [wheel, dojo, *sites]
def offline(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise RuntimeError("external I/O forbidden in wheel smoke")
sys.addaudithook(offline)
from pipeline import config
from llm.prompts import prompt_templates
assert config.CONFIG_PATH.resolve().is_relative_to(pathlib.Path(wheel).resolve())
templates = prompt_templates()
assert len(templates) == 18
assert "corporate_records_specialist" in templates
assert set(config.get_all_doc_types()) == {"contract", "corporate_record", "correspondence", "insurance_claim", "merger_agreement"}
assert not any("editable" in repr(finder).lower() for finder in sys.meta_path)
print(json.dumps({"templates": len(templates), "config": str(config.CONFIG_PATH)}))
'''
    environment = {**os.environ, "OBSERVABILITY_PROVIDER": "none", "LLM_DOJO_UPDATE_CHECK": "0"}
    python = getattr(sys, "_base_executable", sys.executable)
    result = subprocess.run(
        [python, "-I", "-S", "-B", "-c", code, str(unpacked), str(dojo_root), json.dumps(sites)],
        cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["templates"] == 18
