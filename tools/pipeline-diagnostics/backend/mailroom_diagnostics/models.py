"""Typed models for the diagnostics snapshot and check report.

Strict on purpose: the snapshot is a machine contract, so a malformed graph or
an unsupported version fails loudly instead of producing a plausible-looking
inventory. Extra fields inside node/edge rows are ignored so a compatible
minor contract version can add enrichment without breaking this reader.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

SNAPSHOT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CONTRACT_MAJOR = 1
START = "__start__"
END = "__end__"

_THRESHOLD_FLOATS = ("high", "low", "judge_band_high", "conflict_threshold")
_THRESHOLD_INTS = ("retry_max", "arbiter_retry_max", "judge_max_passes")
_CLASS_THRESHOLD_FLOATS = ("high", "low", "judge_band_high")


class DiagnosticsError(Exception):
    """Input or environment error: the CLI reports JSON and exits 2."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=200)
    span: str = Field(min_length=1, max_length=200)
    observation_type: str = Field(min_length=1, max_length=64)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    conditional: bool = False
    label: str | None = None


class GraphPayload(BaseModel):
    """The graph section shared with ``contract/mailroom-contract.json``."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    start: str = Field(min_length=1, max_length=200)
    end: str = Field(min_length=1, max_length=200)
    nodes: list[GraphNode]
    edges: list[GraphEdge]

    @model_validator(mode="after")
    def _valid_topology(self) -> "GraphPayload":
        if not self.nodes:
            raise ValueError("graph has no nodes")
        if self.start == self.end:
            raise ValueError("graph start and end must differ")
        ids = [node.id for node in self.nodes]
        duplicates = sorted(node_id for node_id, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate node ids: {duplicates}")
        boundary = {self.start, self.end} & set(ids)
        if boundary:
            raise ValueError(f"node ids collide with graph boundaries: {sorted(boundary)}")
        # Identical edge rows are tolerated: the contract cannot distinguish two
        # route tokens that map to the same target, so a future revision may
        # legitimately list the same endpoints twice (checks dedupe findings).
        return self


class Thresholds(BaseModel):
    """Allowlisted confidence/routing numbers. Nothing else leaves the taxonomy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high: float | None = None
    low: float | None = None
    retry_max: int | None = None
    judge_band_high: float | None = None
    arbiter_retry_max: int | None = None
    judge_max_passes: int | None = None
    conflict_threshold: float | None = None
    by_class: dict[str, dict[str, float]] = Field(default_factory=dict)

    @classmethod
    def from_confidence(cls, block: Mapping[str, Any]) -> "Thresholds":
        data: dict[str, Any] = {}
        for name in _THRESHOLD_FLOATS:
            if block.get(name) is not None:
                if not _is_number(block[name]):
                    raise DiagnosticsError("invalid-schema", f"confidence.{name} must be finite numeric data")
                data[name] = float(block[name])
        for name in _THRESHOLD_INTS:
            value = block.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                data[name] = value
            elif value is not None:
                raise DiagnosticsError("invalid-schema", f"confidence.{name} must be an integer")
        by_class: dict[str, dict[str, float]] = {}
        rows = block.get("by_class", {})
        if not isinstance(rows, dict):
            raise DiagnosticsError("invalid-schema", "confidence.by_class must be an object")
        for class_key, row in rows.items():
            if not isinstance(row, dict):
                raise DiagnosticsError("invalid-schema", "class confidence must be an object")
            filtered = {}
            for name in _CLASS_THRESHOLD_FLOATS:
                if row.get(name) is not None:
                    if not _is_number(row[name]):
                        raise DiagnosticsError("invalid-schema", "class confidence must contain finite numbers")
                    filtered[name] = float(row[name])
            if filtered:
                by_class[str(class_key)] = filtered
        data["by_class"] = by_class
        return cls(**data)


class Provenance(BaseModel):
    """Where a snapshot came from. Both kinds are previews, never a worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workspace-preview", "artifact"]
    active_worker: bool = False
    pipeline_version: str | None = None
    contract_version: str | None = None
    git_sha: str | None = None
    git_dirty: bool | None = None
    artifact_sha256: str | None = None
    artifact_bytes: int | None = None
    tracing: Literal["disabled"] = "disabled"


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    tool_version: str
    provenance: Provenance
    graph: GraphPayload
    graph_hash: str
    thresholds: Thresholds


class Finding(BaseModel):
    """One deterministic, reproducible structural observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    rule: str
    rule_version: str = "1.0.0"
    category: Literal["structural"] = "structural"
    severity: Literal["error", "warning", "info"]
    proof_status: Literal["confirmed", "inconclusive", "unavailable"]
    message: str
    affected: dict[str, Any] = Field(default_factory=dict)
    witness: dict[str, Any] = Field(default_factory=dict)


class Coverage(BaseModel):
    """What the run did, and what it explicitly did not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["structural"] = "structural"
    live_observations: bool = False
    checks: list[str]
    limitations: list[str]


class CheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    tool_version: str
    mode: Literal["check"] = "check"
    provenance: Provenance
    graph_hash: str
    findings: list[Finding]
    coverage: Coverage


def graph_hash(graph: GraphPayload) -> str:
    """Deterministic content hash of the exported topology."""
    blob = json.dumps(graph.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
