"""eval-environment prompt lineage: every archived, frozen and mutated version.

Read-only catalog of the ``mailroom-dataset-v1`` lineage vendored by
``scripts/sync_eval_environment_lineage.py`` into
``eval_environment_lineage.json``: archived ``<role>_v0`` production prompts,
the frozen ``<role>_v1`` snapshot, and the GEPA / manual mutations
(``<role>_v2`` ...), each with its memo, provenance and a change summary
against its predecessor. The texts are historical eval-environment bytes; the
anti-priming rule that governs dojo-authored prompts is not applied to them.

``sha256`` is eval-environment's convention: ``sha256(text.encode("utf-8"))``
over the raw text. (The ``production_prompts`` catalog hashes the text with a
trailing newline ensured; the two agree for the specialist stems, whose texts
end in ``"\\n"``.)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from types import MappingProxyType

__all__ = [
    "PromptVersion",
    "LINEAGE_SOURCE",
    "LINEAGES",
    "list_prompt_versions",
    "get_prompt_version",
    "prompt_roles",
    "prompt_chain",
    "latest_prompt_version",
    "default_prompt_version",
    "diff_prompt_versions",
    "clear_lineage_cache",
]

LINEAGES = ("archived", "frozen", "mutation")
_DATA_FILE = "eval_environment_lineage.json"
_KEY_RE = re.compile(r"^(?P<role>.+)_v(?P<version>\d+)$")


@dataclass(frozen=True)
class PromptVersion:
    """One eval-environment prompt version. ``text`` is the exact historical body."""

    key: str
    role: str
    version: int
    lineage: str  # "archived" | "frozen" | "mutation"
    parent: str | None  # mutations: the key they were derived from
    supersedes: str | None  # frozen v1: the archived <role>_v0 it replaced
    memo: str
    anchor_head: str | None
    created_at: str
    source: str
    sha256: str  # sha256(text.encode("utf-8")), eval-environment convention
    chars: int
    text: str
    changes: Mapping | None = field(hash=False)  # vs. the predecessor; None when none

    @property
    def predecessor(self) -> str | None:
        """Key this version follows: ``parent`` for mutations, else ``supersedes``."""
        return self.parent or self.supersedes


def _from_row(row: dict) -> PromptVersion:
    """Construct an immutable version record from one serialized lineage row."""
    changes = row.get("changes")
    return PromptVersion(
        key=row["key"],
        role=row["role"],
        version=int(row["version"]),
        lineage=row["lineage"],
        parent=row.get("parent"),
        supersedes=row.get("supersedes"),
        memo=row["memo"],
        anchor_head=row.get("anchor_head"),
        created_at=row["created_at"],
        source=row["source"],
        sha256=row["sha256"],
        chars=int(row["chars"]),
        text=row["text"],
        changes=MappingProxyType(dict(changes)) if changes is not None else None,
    )


def _validate(versions: dict[str, PromptVersion]) -> None:
    """Reject inconsistent version keys, lineage links, and text digests."""
    for v in versions.values():
        m = _KEY_RE.match(v.key)
        if not m or m["role"] != v.role or int(m["version"]) != v.version:
            raise ValueError(f"lineage entry {v.key!r}: role/version do not match its key")
        if v.lineage not in LINEAGES:
            raise ValueError(f"lineage entry {v.key!r}: unknown lineage {v.lineage!r}")
        pred = v.predecessor
        if pred is not None and pred not in versions:
            raise ValueError(f"lineage entry {v.key!r}: predecessor {pred!r} is missing")
        if v.chars != len(v.text) or v.sha256 != hashlib.sha256(v.text.encode("utf-8")).hexdigest():
            raise ValueError(f"lineage entry {v.key!r}: text does not match its sha256/chars")


@lru_cache(maxsize=1)
def _load() -> tuple[Mapping, tuple[PromptVersion, ...]]:
    """Load, validate, and cache source metadata and sorted lineage versions."""
    raw = resources.files("llm_dojo_scoring.prompts").joinpath(_DATA_FILE).read_text(encoding="utf-8")
    data = json.loads(raw)
    rows = data.get("versions") or []
    versions: dict[str, PromptVersion] = {}
    for row in rows:
        v = _from_row(row)
        if v.key in versions:
            raise ValueError(f"duplicate lineage key {v.key!r}")
        versions[v.key] = v
    _validate(versions)
    source = MappingProxyType({
        "source_repo": data["source_repo"],
        "source_commit": data["source_commit"],
        "lineage_id": data["lineage_id"],
    })
    ordered = tuple(sorted(versions.values(), key=lambda v: (v.role, v.version)))
    return source, ordered


def clear_lineage_cache() -> None:
    """Drop the loaded lineage (test isolation); also run by ``clear_prompt_cache()``."""
    _load.cache_clear()
    _index.cache_clear()


@lru_cache(maxsize=1)
def _index() -> dict[str, PromptVersion]:
    """Cache validated lineage versions indexed by their unique keys."""
    return {v.key: v for v in _load()[1]}


class _LazySource(Mapping):
    """``LINEAGE_SOURCE``: ``{source_repo, source_commit, lineage_id}`` (loaded on first use)."""

    def __getitem__(self, k):
        """Look up source metadata, loading the lineage on first access."""
        return _load()[0][k]

    def __iter__(self):
        """Iterate the lazily loaded source metadata keys."""
        return iter(_load()[0])

    def __len__(self):
        """Return the number of source metadata keys after lazy loading."""
        return len(_load()[0])

    def __repr__(self):
        """Represent the loaded source metadata as an ordinary dictionary."""
        return repr(dict(_load()[0]))


LINEAGE_SOURCE: Mapping = _LazySource()


def _unknown(kind: str, name: str, known) -> KeyError:
    """Build an unknown-prompt error with close matches from known names."""
    close = difflib.get_close_matches(name, sorted(known), n=5, cutoff=0.5)
    hint = f"; did you mean: {', '.join(close)}" if close else ""
    return KeyError(f"unknown prompt {kind} {name!r}{hint}")


def list_prompt_versions(role: str | None = None, lineage: str | None = None) -> list[PromptVersion]:
    """Every version, sorted by ``(role, version)``; optionally filtered.

    ``lineage`` is one of ``"archived"``, ``"frozen"``, ``"mutation"``
    (``ValueError`` otherwise). An unknown ``role`` yields ``[]``.
    """
    if lineage is not None and lineage not in LINEAGES:
        raise ValueError(f"unknown lineage {lineage!r}; expected one of {LINEAGES}")
    return [
        v for v in _load()[1]
        if (role is None or v.role == role) and (lineage is None or v.lineage == lineage)
    ]


def get_prompt_version(key: str) -> PromptVersion:
    """The version for ``key`` (e.g. ``"correspondence_specialist_v7"``).

    Raises ``KeyError`` with close-match suggestions when ``key`` is unknown.
    """
    try:
        return _index()[key]
    except (KeyError, TypeError):
        raise _unknown("version", str(key), _index()) from None


def prompt_roles() -> list[str]:
    """Sorted role names (``key`` minus the trailing ``_v<N>``)."""
    return sorted({v.role for v in _load()[1]})


def latest_prompt_version(role: str) -> PromptVersion:
    """Highest-numbered frozen or mutation version of ``role``.

    Archived entries count only when a role has nothing else, so the verbose
    ``contracts_specialist_v33`` re-key never outranks ``contracts_specialist_v3``.
    ``KeyError`` for an unknown role.
    """
    vs = list_prompt_versions(role=role)
    if not vs:
        raise _unknown("role", role, prompt_roles())
    live = [v for v in vs if v.lineage != "archived"]
    return (live or vs)[-1]


def default_prompt_version(role: str) -> PromptVersion:
    """eval-environment's default for ``role``: the frozen ``<role>_v1``."""
    try:
        v = _index()[f"{role}_v1"]
    except KeyError:
        raise _unknown("role", role, prompt_roles()) from None
    if v.lineage != "frozen":
        raise _unknown("role", role, [x.role for x in list_prompt_versions(lineage="frozen")])
    return v


def prompt_chain(key_or_role: str) -> list[PromptVersion]:
    """Versions oldest -> newest along predecessor links ending at the given key.

    A role name means the chain ending at ``latest_prompt_version(role)``. Archived ``_v0`` and
    sorter / judge roots have no predecessor and start their own chain.
    """
    idx = _index()
    if key_or_role in idx:
        tip = idx[key_or_role]
    elif key_or_role in prompt_roles():
        tip = latest_prompt_version(key_or_role)
    else:
        raise _unknown("key or role", str(key_or_role), list(idx) + prompt_roles())
    chain = [tip]
    while chain[-1].predecessor is not None:
        chain.append(idx[chain[-1].predecessor])
    return chain[::-1]


def diff_prompt_versions(a: str, b: str, context: int = 3) -> str:
    """Unified diff of the texts of keys ``a`` -> ``b`` (labels are the keys)."""
    va, vb = get_prompt_version(a), get_prompt_version(b)
    lines = difflib.unified_diff(
        va.text.splitlines(), vb.text.splitlines(),
        fromfile=va.key, tofile=vb.key, n=context, lineterm="",
    )
    out = "\n".join(lines)
    return out + "\n" if out else ""
