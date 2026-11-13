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


def _probe(tmp_path: Path, extra_path: str | None, names: tuple[str, ...] = (), **env: str) -> dict:
    """Import the Dojo in a fresh interpreter with the real release hidden, ``extra_path`` first.

    ``names`` are then imported with ``from llm_dojo_scoring import <name>``, as the API does.
    """
    import json

    path = [str(SRC)] + ([extra_path] if extra_path else [])
    code = (_HIDE_INSTALLED + f"sys.path[:0] = {path!r}\n"
            "import dojo_source, llm_dojo_scoring, json\n"
            + "".join(f"from llm_dojo_scoring import {n}\n" for n in names) +
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


def _lazy_release(tmp_path: Path, field_scoring: str, *, missing: str | None = None) -> str:
    """A fake installed Dojo with a PEP 562 root that defers every required name to ``scoring.field_scoring``."""
    names = [n for n in dojo_source.REQUIRED_ROOT_NAMES if n not in ("__version__", missing)]
    site = _fake_release(
        tmp_path,
        '__version__ = "9.9.9-lazy"\n'
        f"_LAZY = {{n: ('scoring.field_scoring', n) for n in {names!r}}}\n"
        "def __getattr__(name):\n"
        "    import importlib\n"
        "    if name in _LAZY:\n"
        "        mod, attr = _LAZY[name]\n"
        "        return getattr(importlib.import_module(f'{__name__}.{mod}'), attr)\n"
        "    raise AttributeError(name)\n",
    )
    scoring = Path(site) / "llm_dojo_scoring" / "scoring"
    scoring.mkdir()
    (scoring / "__init__.py").write_text("", encoding="utf-8")
    if field_scoring == "healthy":
        field_scoring = "".join(f"{n} = None\n" for n in names)
    (scoring / "field_scoring.py").write_text(field_scoring, encoding="utf-8")
    return site


def test_lazy_root_with_broken_submodule_falls_back(tmp_path):
    """A lazy root imports cleanly even when a required name's module is broken; the hook must still fall back."""
    site = _lazy_release(tmp_path, 'raise ImportError("broken field_scoring")\n')
    status = _probe(tmp_path, site, names=("warm_embedding_model",))
    assert status["source"] == "vendored" and "broken field_scoring" in status["reason"]
    assert status["file"].startswith(str(ROOT / "vendor"))


def test_lazy_root_missing_a_required_name_falls_back(tmp_path):
    """A lazy release that no longer exports a name mailroom imports is rejected at import, not at use."""
    site = _lazy_release(tmp_path, "healthy", missing="warm_embedding_model")
    status = _probe(tmp_path, site, names=("warm_embedding_model",))
    assert status["source"] == "vendored" and "warm_embedding_model" in status["reason"]


def test_lazy_root_with_healthy_submodules_stays_installed(tmp_path):
    """The same lazy release with every required name intact is kept as the installed Dojo."""
    site = _lazy_release(tmp_path, "healthy")
    status = _probe(tmp_path, site, names=("warm_embedding_model",))
    assert status["source"] == "installed" and status["version"] == "9.9.9-lazy"


def test_required_root_names_cover_every_root_import():
    """REQUIRED_ROOT_NAMES must list every ``from llm_dojo_scoring import X`` made by src/ and tools/."""
    import ast

    found: set[str] = set()
    for base in (ROOT / "src", ROOT / "tools"):
        for path in base.rglob("*.py"):
            if {"vendor", ".venv", "node_modules"} & set(path.relative_to(ROOT).parts):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == dojo_source.PACKAGE:
                    found.update(alias.name for alias in node.names)
    assert found, "the AST scan found no root imports"
    missing = found - set(dojo_source.REQUIRED_ROOT_NAMES)
    assert not missing, f"add to dojo_source.REQUIRED_ROOT_NAMES: {sorted(missing)}"


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


#: Dojo names the per-type scoring nodes need (plan M1, gap G2).
_SCORING_NAMES = ("score_document", "get_doc_scorer", "score_archive_block", "assess_extraction_payload")


def test_scoring_imports_under_vendored_source(tmp_path):
    """The forced fallback snapshot carries every name the scoring nodes import."""
    code = (_HIDE_INSTALLED + f"sys.path[:0] = [{str(SRC)!r}]\n"
            "import dojo_source, llm_dojo_scoring as d, json\n"
            f"names = {_SCORING_NAMES!r}\n"
            "print(json.dumps({**dojo_source.status(), 'file': d.__file__, 'version': d.__version__,"
            " 'missing': [n for n in names if not callable(getattr(d, n, None))]}))\n")
    run_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run_env["MAILROOM_DOJO_SOURCE"] = "vendored"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=run_env,
                         cwd=tmp_path, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    import json

    status = json.loads(out.stdout.strip().splitlines()[-1])
    assert status["source"] == "vendored"
    assert status["file"].startswith(str(ROOT / "vendor"))
    assert status["missing"] == []
