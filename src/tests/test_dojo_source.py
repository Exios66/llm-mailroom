"""Installed Dojo first, vendored snapshot as fallback (src/dojo_source)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import dojo_source

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _pin() -> str:
    found = re.search(r"llm-dojo-scoring\.git@([0-9a-f]{40})", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert found, "pyproject must pin llm-dojo-scoring to a commit"
    return found.group(1)


def test_snapshot_matches_pyproject_pin():
    vendor_md = (ROOT / "vendor" / "llm-dojo-scoring" / "VENDOR.md").read_text(encoding="utf-8")
    assert f"Exact source commit: `{_pin()}`" in vendor_md
    init = (ROOT / "vendor" / "llm-dojo-scoring" / "src" / "llm_dojo_scoring" / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M).group(1)
    assert f"Source package version: `{version}`" in vendor_md


def test_hook_registered_by_package_imports():
    import pipeline  # noqa: F401

    assert any(isinstance(f, dojo_source.DojoFallbackFinder) for f in sys.meta_path)
    assert dojo_source.vendored_root() == ROOT / "vendor" / "llm-dojo-scoring" / "src"


# Hide the real release without dropping its sys.path entry: a regular install lives in
# site-packages, and removing that entry would also hide the Dojo's own dependencies
# (yaml, ...), so the vendored snapshot could not import either. Only the top-level
# ``llm_dojo_scoring`` lookup skips the directories that held the real release.
_HIDE_INSTALLED = (
    "import sys, os, importlib.machinery as _m\n"
    "sys.meta_path[:] = [f for f in sys.meta_path if 'llm_dojo_scoring' not in str(getattr(f, '__module__', '')) + type(f).__module__]\n"
    "_hidden = {p for p in sys.path if os.path.isdir(os.path.join(p or '.', 'llm_dojo_scoring'))}\n"
    "class _HidingPathFinder(_m.PathFinder):\n"
    "    @classmethod\n"
    "    def find_spec(cls, name, path=None, target=None):\n"
    "        if name == 'llm_dojo_scoring' and path is None:\n"
    "            path = [p for p in sys.path if p not in _hidden]\n"
    "        return super().find_spec(name, path, target)\n"
    "sys.meta_path[:] = [_HidingPathFinder if f is _m.PathFinder else f for f in sys.meta_path]\n"
)


def _probe(tmp_path: Path, extra_path: str | None, **env: str) -> dict:
    """Import the Dojo in a fresh interpreter with the real release hidden, ``extra_path`` first."""
    import json

    path = [str(SRC)] + ([extra_path] if extra_path else [])
    code = (_HIDE_INSTALLED + f"sys.path[:0] = {path!r}\n"
            "import dojo_source, llm_dojo_scoring, json\n"
            "print(json.dumps({**dojo_source.status(), 'file': llm_dojo_scoring.__file__,"
            " 'version': getattr(llm_dojo_scoring, '__version__', None)}))\n")
    run_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run_env.update(env)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=run_env,
                         cwd=tmp_path, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _fake_release(tmp_path: Path, body: str) -> str:
    pkg = tmp_path / "site" / "llm_dojo_scoring"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return str(tmp_path / "site")


def test_missing_release_uses_snapshot(tmp_path):
    status = _probe(tmp_path, None)
    assert status["source"] == "vendored" and "not installed" in status["reason"]
    assert status["file"].startswith(str(ROOT / "vendor"))


def test_installed_release_wins(tmp_path):
    site = _fake_release(tmp_path, '__version__ = "9.9.9-installed"\n')
    status = _probe(tmp_path, site)
    assert status["source"] == "installed" and status["version"] == "9.9.9-installed"


def test_broken_release_falls_back(tmp_path):
    site = _fake_release(tmp_path, 'raise ImportError("simulated outage")\n')
    status = _probe(tmp_path, site)
    assert status["source"] == "vendored" and "simulated outage" in status["reason"]
    assert status["file"].startswith(str(ROOT / "vendor"))


def test_modes(tmp_path):
    site = _fake_release(tmp_path, '__version__ = "9.9.9-installed"\n')
    forced = _probe(tmp_path, site, MAILROOM_DOJO_SOURCE="vendored")
    assert forced["source"] == "vendored"
    broken = _fake_release(tmp_path / "b", 'raise ImportError("down")\n')
    code = _HIDE_INSTALLED + ("sys.path[:0] = [%r, %r]; import dojo_source\n"
                              "try:\n import llm_dojo_scoring\nexcept ImportError:\n print('raised')\n") % (str(SRC), broken)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "MAILROOM_DOJO_SOURCE": "installed"})
    assert "raised" in out.stdout
