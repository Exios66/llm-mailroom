"""Package layout and the legacy flat import paths.

Until v0.26.0 every module lived directly under ``llm_dojo_scoring``. The modules
are now grouped into subpackages (see ``llm_dojo_scoring/README.md``). The old
flat paths stay importable, so ``import llm_dojo_scoring.field_scoring`` and
``mock.patch("llm_dojo_scoring.field_scoring._get_embedding")`` keep working.
Each old path resolves to the **same module object** as its new path, so
monkeypatching either one is visible through both.

New code should import from the subpackage path.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType

#: Legacy flat module name -> its path below ``llm_dojo_scoring``.
LEGACY_MODULES: dict[str, str] = {
    "_paired": "scoring._paired",
    "agent_rubrics": "scoring.agent_rubrics",
    "archive": "pipeline.archive",
    "asr": "scoring.asr",
    "bootstrap": "analysis.bootstrap",
    "bundles": "metrics.bundles",
    "claims_consistency": "scoring.claims_consistency",
    "classification": "scoring.classification",
    "content_scoring": "scoring.content_scoring",
    "corporate_record_relabel": "schemas.corporate_record_relabel",
    "corporate_record_schema": "schemas.corporate_record_schema",
    "corpus": "schemas.corpus",
    "cost": "analysis.cost",
    "cuad_schema": "schemas.cuad_schema",
    "diagnostics": "analysis.diagnostics",
    "doc_bundles": "metrics.doc_bundles",
    "emitter": "metrics.emitter",
    "equivalences": "schemas.equivalences",
    "experiment": "analysis.experiment",
    "extraction_metrics": "scoring.extraction_metrics",
    "failure_modes": "analysis.failure_modes",
    "field_scoring": "scoring.field_scoring",
    "field_support": "scoring.field_support",
    "gt_metadata": "schemas.gt_metadata",
    "intake": "pipeline.intake",
    "intents": "schemas.intents",
    "legalbench_scoring": "scoring.legalbench_scoring",
    "mailroom": "pipeline.mailroom",
    "maud": "schemas.maud",
    "maud_schema": "schemas.maud_schema",
    "metric_meta": "metrics.metric_meta",
    "pilot_labels": "pipeline.pilot_labels",
    "pilot_summary": "pipeline.pilot_summary",
    "pipeline_scoring": "pipeline.pipeline_scoring",
    "pipeline_verdict": "pipeline.pipeline_verdict",
    "profiles": "metrics.profiles",
    "pruning": "metrics.pruning",
    "registry": "metrics.registry",
    "sandbox_schema_adherence": "pipeline.sandbox_schema_adherence",
    "sandbox_scoring": "pipeline.sandbox_scoring",
    "schema_contract": "schemas.schema_contract",
    "scorecard_honesty": "metrics.scorecard_honesty",
    "serving": "analysis.serving",
    "sorter_guide": "schemas.sorter_guide",
    "suites": "scoring.suites",
    "tasks": "scoring.tasks",
    "trace_knobs": "scoring.trace_knobs",
    "update_awareness": "integrations.update_awareness",
}

_PKG = __name__.rpartition(".")[0]


class _AliasLoader(importlib.abc.Loader):
    """Load a legacy name as the module that now lives at its new path."""

    def __init__(self, target: str) -> None:
        """Remember the dotted path of the real module."""
        self.target = target

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        """Import the real module and hand it to the import system."""
        module = importlib.import_module(self.target)
        self._spec = module.__spec__
        self._loader = getattr(module, "__loader__", None)
        return module

    def exec_module(self, module: ModuleType) -> None:
        """Restore the real module's spec and loader, which module_from_spec overwrote."""
        module.__spec__ = self._spec
        module.__loader__ = self._loader


class _LegacyFinder(importlib.abc.MetaPathFinder):
    """Resolve ``llm_dojo_scoring.<old name>`` to its subpackage module."""

    def find_spec(self, fullname: str, path=None, target=None):
        """Return an alias spec for a legacy flat module name, else ``None``."""
        pkg, _, name = fullname.rpartition(".")
        if pkg != _PKG or name not in LEGACY_MODULES:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(f"{_PKG}.{LEGACY_MODULES[name]}"))


def install_legacy_aliases() -> None:
    """Install the legacy-path finder once (idempotent)."""
    if not any(isinstance(f, _LegacyFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _LegacyFinder())


__all__ = ["LEGACY_MODULES", "install_legacy_aliases"]
