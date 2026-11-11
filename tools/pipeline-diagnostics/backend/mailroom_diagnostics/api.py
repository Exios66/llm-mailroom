"""Read-only diagnostics HTTP API: capabilities, snapshot and findings.

The routes are served by the standalone inspection app
(:mod:`mailroom_diagnostics.app`) and, when explicitly enabled, mounted by the
producer API under ``/v1/diagnostics``. Every route uses the producer's
bearer/loopback authorization (:mod:`api.auth`); there is no separate
permissive diagnostics scheme.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import _require_token

from . import __version__
from .audit import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    document_audit,
    list_documents,
    valid_doc_id,
)
from .checks import run_checks
from .models import SNAPSHOT_SCHEMA_VERSION
from .runs import list_experiments, list_runs, load_experiment, load_run
from .snapshot import build_workspace_snapshot

router = APIRouter(dependencies=[Depends(_require_token)])

MOUNT = "/diagnostics"


def capabilities() -> dict[str, Any]:
    """What this read-only slice can and cannot answer.

    Stated explicitly so a client never infers live telemetry, replay,
    calibration or UI availability from the presence of the API.
    """
    return {
        "tool": "mailroom-diagnostics",
        "version": __version__,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "read_only": True,
        "source_modes": ["workspace-preview"],
        "features": {
            "graph": True,
            "structural_checks": True,
            "running_worker_snapshot": False,
            "runs": True,
            "experiments": True,
            "audit": True,
            "live_events": False,
            "replay": "cli",
            "calibration": False,
            "bundles": False,
            "ui": "optional-not-built-in-this-slice",
        },
        "audit": {
            "read_only": True,
            "schema_modes": ["event-chain", "unified-row"],
            "verifies": ["audit chain", "terminal row", "attempts", "addenda", "migrated legacy"],
            "truncated_reads_verified": False,
        },
        "mount": MOUNT,
        "docs": "tools/pipeline-diagnostics/QUICKSTART.md",
    }


@router.get("/capabilities")
def get_capabilities() -> dict[str, Any]:
    return capabilities()


@router.get("/snapshot")
def get_snapshot() -> dict[str, Any]:
    """Compile the checked-out workspace graph for this request.

    No cache: the point of a workspace preview is to show the code that is on
    disk now. The response is labelled ``workspace-preview``; it is never
    presented as a running worker's active graph.
    """
    return build_workspace_snapshot().model_dump(mode="json")


@router.get("/findings")
def get_findings() -> dict[str, Any]:
    """Run the implemented structural checks against the current workspace."""
    report = run_checks(build_workspace_snapshot())
    payload = report.model_dump(mode="json")
    blocking = any(finding.severity in ("error", "warning") for finding in report.findings)
    payload["exit_code"] = 1 if blocking else 0
    return payload


@router.get("/runs")
def get_runs() -> dict[str, Any]:
    """Recorded pipeline runs, newest first (read-only journals)."""
    return {"runs": list_runs()}


@router.get("/runs/{execution_id}")
def get_run(execution_id: str) -> dict[str, Any]:
    payload = load_run(execution_id)
    if payload is None:
        raise HTTPException(404, "run not found")
    return payload


@router.get("/experiments")
def get_experiments() -> dict[str, Any]:
    return {"experiments": list_experiments()}


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict[str, Any]:
    payload = load_experiment(experiment_id)
    if payload is None:
        raise HTTPException(404, "experiment not found")
    return payload


@router.get("/audit/documents")
def get_audit_documents(
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict[str, Any]:
    """Bounded document ids from the audit store (no raw audit detail).

    An absent store is reported in ``store`` with a safe reason code; it is
    never created and its path is never echoed.
    """
    return list_documents(limit=limit, offset=offset)


@router.get("/audit/documents/{doc_id}")
def get_audit_document(doc_id: str) -> dict[str, Any]:
    """One document's audit record, verification verdict and redacted view."""
    if not valid_doc_id(doc_id):
        raise HTTPException(422, "invalid document id")
    return document_audit(doc_id)
