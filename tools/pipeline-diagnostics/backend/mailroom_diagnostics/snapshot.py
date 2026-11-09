"""Build a WORKSPACE PREVIEW snapshot, or read one from a contract artifact.

The workspace path compiles the graph through ``graph.contract`` (the same
helper the display-contract exporter uses) and therefore describes THIS
checkout, not any running worker. The artifact path never imports the pipeline
graph: it is an offline reader for an existing JSON contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .models import (
    SUPPORTED_CONTRACT_MAJOR,
    DiagnosticsError,
    GraphPayload,
    Provenance,
    Snapshot,
    Thresholds,
    graph_hash,
)

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024


def load_artifact(path: Path) -> Snapshot:
    """Read a display-contract JSON file without importing the pipeline graph."""
    raw = _read_bounded(path)
    try:
        data = json.loads(raw, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "msg", "decode error")
        raise DiagnosticsError("invalid-json", f"artifact is not valid JSON: {detail}") from exc
    if not isinstance(data, dict):
        raise DiagnosticsError("invalid-schema", "artifact must be a JSON object")
    version = data.get("contract_version")
    if not isinstance(version, str) or not version.strip():
        raise DiagnosticsError("invalid-schema", "artifact has no contract_version")
    _require_supported(version)
    graph = data.get("graph")
    if not isinstance(graph, dict):
        raise DiagnosticsError("invalid-schema", "artifact has no graph object")
    payload = _validated_graph(graph)
    confidence = data.get("confidence", {})
    if not isinstance(confidence, dict):
        raise DiagnosticsError("invalid-schema", "artifact confidence must be an object")
    pipeline_version = data.get("pipeline_version")
    return Snapshot(
        tool_version=__version__,
        provenance=Provenance(
            kind="artifact",
            contract_version=version,
            pipeline_version=pipeline_version if isinstance(pipeline_version, str) else None,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            artifact_bytes=len(raw),
        ),
        graph=payload,
        graph_hash=graph_hash(payload),
        thresholds=Thresholds.from_confidence(confidence),
    )


def build_workspace_snapshot() -> Snapshot:
    """Compile the current checkout's graph into a preview snapshot."""
    from graph.contract import graph_section

    payload = _validated_graph(graph_section())
    git_sha, git_dirty = _git_state(_repo_root())
    return Snapshot(
        tool_version=__version__,
        provenance=Provenance(
            kind="workspace-preview",
            pipeline_version=_pipeline_version(),
            git_sha=git_sha,
            git_dirty=git_dirty,
            tracing="disabled",
        ),
        graph=payload,
        graph_hash=graph_hash(payload),
        thresholds=_workspace_thresholds(),
    )


def _read_bounded(path: Path) -> bytes:
    """Read a bounded regular file; pipes/devices cannot stall inspection."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise DiagnosticsError("io-error", "artifact must be a regular file")
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise DiagnosticsError(
            "io-error", f"cannot read artifact: {exc.strerror or type(exc).__name__}"
        ) from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise DiagnosticsError(
            "input-too-large", f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte input limit"
        )
    return raw


def _invalid_constant(value: str) -> None:
    raise DiagnosticsError("invalid-json", "artifact contains a non-finite JSON number")


def _require_supported(version: str) -> None:
    head = version.split(".", 1)[0]
    try:
        major = int(head)
    except ValueError:
        raise DiagnosticsError(
            "unsupported-version", f"contract_version {version!r} is not a version"
        ) from None
    if major != SUPPORTED_CONTRACT_MAJOR:
        raise DiagnosticsError(
            "unsupported-version",
            f"contract_version {version!r} is unsupported; this tool reads "
            f"{SUPPORTED_CONTRACT_MAJOR}.x",
        )


def _validated_graph(graph: dict[str, Any]) -> GraphPayload:
    try:
        return GraphPayload.model_validate(graph)
    except ValidationError as exc:
        detail = exc.errors()[0]
        where = ".".join(str(part) for part in detail.get("loc", ())) or "graph"
        raise DiagnosticsError(
            "invalid-schema", f"graph is malformed at {where}: {detail.get('msg', 'invalid')}"
        ) from exc


def _pipeline_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("mailroom")
    except Exception:
        return None


def _repo_root() -> Path | None:
    """The checkout that owns the imported pipeline package, if it is one."""
    try:
        import pipeline
    except Exception:
        return None
    source = Path(pipeline.__file__).resolve()
    for parent in source.parents:
        expected = parent / "src" / "pipeline" / "__init__.py"
        if (parent / ".git").exists() and expected.is_file() and expected.resolve() == source:
            return parent
    return None


def _git_state(repo: Path | None) -> tuple[str | None, bool | None]:
    if repo is None:
        return None, None
    sha = _git(repo, "rev-parse", "HEAD")
    if sha is None:
        return None, None
    status = _git(repo, "status", "--porcelain")
    return sha, None if status is None else bool(status)


def _git(repo: Path, *args: str) -> str | None:
    """Run one fixed read-only git command; absence of git is not an error."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _workspace_thresholds() -> Thresholds:
    from pipeline.config import load_config

    block = load_config().get("confidence", {})
    if not isinstance(block, dict):
        raise DiagnosticsError("invalid-schema", "taxonomy confidence block must be an object")
    return Thresholds.from_confidence(block)
