"""Deterministic provenance hashes for a workspace preview.

A hash detects change; it does not explain or prove semantics. Inputs are
grouped so a client can separate wiring changes from routing, taxonomy,
generated-contract and confidence/guard implementation changes. Missing
inputs are reported explicitly instead of being silently omitted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import Thresholds

# Repo-relative inputs. Keep these narrow and source-level: a hash over the
# whole tree would flag unrelated docs and hide which seam actually moved.
HASH_INPUTS: dict[str, tuple[str, ...]] = {
    "topology": (
        "src/graph/build_graph.py",
        "src/graph/contract.py",
        "src/graph/routing.py",
        "src/graph/state.py",
    ),
    "routing": ("src/graph/routing.py",),
    "taxonomy": ("src/config/taxonomy.yaml",),
    "contract": ("contract/mailroom-contract.json",),
    # Confidence/guard implementation can change behavior with identical
    # edges (for example PR 204's field-less-window merge).
    "implementation": (
        "src/agents/base.py",
        "src/agents/sorter.py",
        "src/langchain_agents/specialist_agents.py",
        "src/pipeline/guards.py",
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(
    root: Path, groups: dict[str, tuple[str, ...]] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Return ``{group: sha256}`` plus the sorted repo-relative missing paths.

    The digest includes the repo-relative name and a separator, so renaming a
    file changes the group hash even when its bytes are identical.
    """
    selected = HASH_INPUTS if groups is None else groups
    hashes: dict[str, str] = {}
    missing: set[str] = set()
    for group, paths in selected.items():
        digest = hashlib.sha256()
        present = 0
        for relative in sorted(paths):
            candidate = root / relative
            if not candidate.is_file():
                missing.add(relative)
                continue
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
            digest.update(b"\0")
            present += 1
        if present:
            hashes[group] = f"sha256:{digest.hexdigest()}"
    return hashes, sorted(missing)


def effective_policy_hash(thresholds: Thresholds) -> str:
    """Canonical digest of the allowlisted effective thresholds."""
    payload = thresholds.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def workspace_hashes(
    root: Path | None, thresholds: Thresholds
) -> tuple[dict[str, str], list[str]]:
    """Source hashes plus the effective-policy digest for a workspace preview."""
    hashes, missing = hash_inputs(root) if root is not None else ({}, [])
    hashes = dict(hashes)
    hashes["effective_policy"] = effective_policy_hash(thresholds)
    return hashes, missing
