"""Importable prompt catalog for llm-dojo-scoring.

Vendors live production templates, the latest docclass-merged family, and the
eval-environment frozen ``production_prompts`` v1 lineage (sha256-locked
specialist stems). Metric bundle and field-map live on the record metadata —
never as eval targets in the model-visible string.

Non-LLM roles (intake clerk, archivist, local-vs-API serving comparison,
proposed auditors) are catalogued with empty ``text`` and an honest ``kind``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Iterable

import yaml

from .lineage import (
    LINEAGE_SOURCE,
    PromptVersion,
    clear_lineage_cache,
    default_prompt_version,
    diff_prompt_versions,
    get_prompt_version,
    latest_prompt_version,
    list_prompt_versions,
    prompt_chain,
    prompt_roles,
)

__all__ = [
    "PromptRecord",
    "FAMILIES",
    "KINDS",
    "get_prompt",
    "list_prompts",
    "clear_prompt_cache",
    # eval-environment prompt lineage (llm_dojo_scoring.prompts.lineage)
    "PromptVersion",
    "LINEAGE_SOURCE",
    "list_prompt_versions",
    "get_prompt_version",
    "prompt_roles",
    "prompt_chain",
    "latest_prompt_version",
    "default_prompt_version",
    "diff_prompt_versions",
]

FAMILIES = ("production", "docclass", "production_prompts")
KINDS = ("llm", "deterministic", "procedural", "proposed")

_PROVENANCE_COMMENT = re.compile(r"^\s*<!--.*?-->\s*", re.DOTALL)


@dataclass(frozen=True)
class PromptRecord:
    """One catalog entry. ``text`` is model-visible for ``kind=llm`` only."""

    agent: str
    family: str
    version: str
    kind: str
    text: str
    metrics_bundle: str | None = None
    doc_bundle: str | None = None
    source_repo: str = ""
    source_key: str = ""
    priming: tuple[str, ...] = ()
    notes: str = ""
    template: str | None = None
    sha256: str = ""
    source_commit: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Return the agent and family pair identifying this catalog entry."""
        return (self.agent, self.family)


def _pkg_root():
    """Return the resource root for installed prompt data and templates."""
    return resources.files("llm_dojo_scoring.prompts")


def _strip_provenance(raw: str) -> str:
    """Remove one leading provenance comment and following blank lines."""
    return _PROVENANCE_COMMENT.sub("", raw, count=1).lstrip("\n")


def _load_template(name: str | None) -> str:
    """Read model-visible template text, or return empty text without a name."""
    if not name:
        return ""
    path = _pkg_root().joinpath("templates", name)
    return _strip_provenance(path.read_text(encoding="utf-8"))


def _record_from_row(row: dict) -> PromptRecord:
    """Validate a catalog row and load template text only for LLM roles."""
    kind = str(row.get("kind") or "llm")
    if kind not in KINDS:
        raise ValueError(f"unknown prompt kind {kind!r}")
    family = str(row.get("family") or "production")
    if family not in FAMILIES:
        raise ValueError(f"unknown prompt family {family!r}")
    template = row.get("template")
    priming = row.get("priming") or []
    if isinstance(priming, str):
        priming = [priming]
    text = _load_template(template) if kind == "llm" else ""
    return PromptRecord(
        agent=str(row["agent"]),
        family=family,
        version=str(row.get("version") or ""),
        kind=kind,
        text=text,
        metrics_bundle=row.get("metrics_bundle"),
        doc_bundle=row.get("doc_bundle"),
        source_repo=str(row.get("source_repo") or ""),
        source_key=str(row.get("source_key") or ""),
        priming=tuple(str(p) for p in priming),
        notes=str(row.get("notes") or ""),
        template=template,
        sha256=str(row.get("sha256") or ""),
        source_commit=str(row.get("source_commit") or ""),
    )


@lru_cache(maxsize=1)
def _catalog() -> tuple[PromptRecord, ...]:
    """Load and cache prompt records in their YAML catalog order."""
    raw = _pkg_root().joinpath("catalog.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    rows = data.get("prompts") or []
    return tuple(_record_from_row(row) for row in rows)


def clear_prompt_cache() -> None:
    """Drop the loaded catalog and eval-environment lineage (test isolation)."""
    _catalog.cache_clear()
    clear_lineage_cache()


def list_prompts(
    *,
    agent: str | None = None,
    family: str | None = None,
    kind: str | None = None,
) -> list[PromptRecord]:
    """Catalog entries, optionally filtered. Order matches ``catalog.yaml``."""
    out: list[PromptRecord] = []
    for rec in _catalog():
        if agent is not None and rec.agent != agent:
            continue
        if family is not None and rec.family != family:
            continue
        if kind is not None and rec.kind != kind:
            continue
        out.append(rec)
    return out


def get_prompt(agent: str, family: str = "production") -> PromptRecord:
    """Return the catalog entry for ``(agent, family)``.

    ``family`` defaults to ``"production"``. Docclass-merged arms use
    ``family="docclass"``; the eval-environment frozen v1 specialist stems
    use ``family="production_prompts"``. Raises ``KeyError`` when the pair
    is unknown.
    """
    for rec in _catalog():
        if rec.agent == agent and rec.family == family:
            return rec
    known = sorted({f"{r.agent}/{r.family}" for r in _catalog()})
    raise KeyError(
        f"unknown prompt {agent!r} family={family!r}; known: {known}"
    ) from None


def iter_prompts() -> Iterable[PromptRecord]:
    """Return an iterator over every cached prompt catalog entry."""
    return iter(_catalog())
