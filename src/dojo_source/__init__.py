"""Import ``llm_dojo_scoring`` from the installed release, with the vendored snapshot as backup.

llm-mailroom depends on the Dojo through its pyproject git pin, so the installed release is
always the primary source. ``vendor/llm-dojo-scoring/src`` in a checkout holds a tracked
snapshot of the same pinned commit; :func:`install` registers an import hook that serves the
snapshot only when the installed release is missing or fails while importing (a broken or
partial install, a dependency outage; a lazy root is also asked for :data:`REQUIRED_ROOT_NAMES`), so scoring keeps working when the release cannot be
loaded. Nothing is imported eagerly: the hook acts on the first ``import llm_dojo_scoring``.

``MAILROOM_DOJO_SOURCE``: ``auto`` (default, installed then vendored), ``installed`` (no
fallback) or ``vendored`` (always the snapshot). ``MAILROOM_DOJO_VENDOR`` points at another
snapshot root. :func:`status` reports which source served the import.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
from pathlib import Path
from typing import Any

PACKAGE = "llm_dojo_scoring"
_STATE: dict[str, Any] = {"source": None, "reason": None, "path": None}

#: Root names this repo imports with ``from llm_dojo_scoring import <name>`` (a test keeps it
#: complete). The Dojo root is lazy (PEP 562): importing it succeeds even when the module behind a
#: name is broken, so the auto fallback resolves these inside its try block instead of letting
#: the failure surface later at a call site outside the fallback boundary.
REQUIRED_ROOT_NAMES: tuple[str, ...] = (
    "EntityListScore",
    "ExtractionScoreResult",
    "__version__",
    "agent_rubrics",
    "bootstrap",
    "configure",
    "configure_from_taxonomy",
    "contract_empty_payload",
    "contract_violations",
    "diagnostics",
    "embed_texts",
    "field_scoring",
    "get_ambiguous_band",
    "get_doc_scorer",
    "get_field_types",
    "get_suite",
    "is_entity_list",
    "legalbench_scoring",
    "list_suites",
    "load_registry",
    "normalize_text",
    "pipeline_scoring",
    "score_category_presence",
    "score_extraction",
    "warm_embedding_model",
)


def vendored_root() -> Path | None:
    env = os.environ.get("MAILROOM_DOJO_VENDOR", "").strip()
    root = Path(env) if env else Path(__file__).resolve().parents[2] / "vendor" / "llm-dojo-scoring" / "src"
    return root if (root / PACKAGE / "__init__.py").is_file() else None


def _mode() -> str:
    mode = os.environ.get("MAILROOM_DOJO_SOURCE", "auto").strip().lower()
    return mode if mode in ("auto", "installed", "vendored") else "auto"


def _vendored_spec(root: Path):
    return importlib.machinery.PathFinder.find_spec(PACKAGE, [str(root)])


def _purge_submodules() -> None:
    for name in [m for m in sys.modules if m.startswith(PACKAGE + ".")]:
        del sys.modules[name]


def _record(source: str, reason: str, origin: str | None) -> None:
    _STATE.update({"source": source, "reason": reason, "path": origin})


class _FallbackLoader(importlib.abc.Loader):
    """Run the installed release's loader; on failure re-execute the module from the snapshot."""

    def __init__(self, primary: importlib.abc.Loader, root: Path | None):
        self.primary, self.root = primary, root

    def create_module(self, spec):
        return None

    @staticmethod
    def _resolve_required(module) -> None:
        """Touch every required root name a lazy root defers, so a broken module fails here."""
        lazy = getattr(module, "_LAZY", None)
        if lazy is None:
            return  # an eager root already imported everything
        deferred = {**getattr(module, "_SUBMODULES", {}), **lazy}
        for name in REQUIRED_ROOT_NAMES:
            if name in deferred:
                getattr(module, name)

    def exec_module(self, module) -> None:
        try:
            self.primary.exec_module(module)
            self._resolve_required(module)
            _record("installed", "installed release imported", getattr(module, "__file__", None))
            return
        except Exception as exc:  # noqa: BLE001 - any import failure falls back
            spec = _vendored_spec(self.root) if self.root is not None else None
            if spec is None:
                raise
            reason = f"installed release failed to import ({type(exc).__name__}: {exc})"
        _purge_submodules()
        for key in [k for k in module.__dict__ if k != "__name__"]:
            del module.__dict__[key]  # drop whatever the failed release left behind
        module.__spec__, module.__loader__ = spec, spec.loader
        module.__file__ = spec.origin
        module.__path__ = list(spec.submodule_search_locations or [])
        spec.loader.exec_module(module)
        _record("vendored", reason, spec.origin)


class DojoFallbackFinder(importlib.abc.MetaPathFinder):
    """Meta-path hook for the top-level ``llm_dojo_scoring`` package only."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != PACKAGE:
            return None
        mode, root = _mode(), vendored_root()
        if mode == "vendored" and root is not None:
            spec = _vendored_spec(root)
            _record("vendored", "MAILROOM_DOJO_SOURCE=vendored", spec.origin if spec else None)
            return spec
        primary = None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            primary = finder.find_spec(fullname, path, target)
            if primary is not None:
                break
        if primary is None:
            if root is None or mode == "installed":
                return None
            spec = _vendored_spec(root)
            _record("vendored", "llm-dojo-scoring is not installed", spec.origin if spec else None)
            return spec
        if mode == "auto" and primary.loader is not None and hasattr(primary.loader, "exec_module"):
            primary.loader = _FallbackLoader(primary.loader, root)
        else:
            _record("installed", "installed release (no fallback)", primary.origin)
        return primary


def install() -> None:
    """Register the hook once, ahead of the default finders (idempotent)."""
    if not any(isinstance(f, DojoFallbackFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, DojoFallbackFinder())


def status() -> dict[str, Any]:
    """Which source served ``llm_dojo_scoring`` (``source`` None until it is imported)."""
    root = vendored_root()
    return {**_STATE, "mode": _mode(), "vendored_root": str(root) if root else None}


install()
