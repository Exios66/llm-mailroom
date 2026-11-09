"""Read-only diagnostics HTTP API: capabilities, snapshot and findings.

The routes are served by the standalone inspection app
(:mod:`mailroom_diagnostics.app`) and, when explicitly enabled, mounted by the
producer API under ``/v1/diagnostics``. Every route uses the producer's
bearer/loopback authorization (:mod:`api.auth`); there is no separate
permissive diagnostics scheme.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.auth import _require_token

from . import __version__
from .checks import run_checks
from .models import SNAPSHOT_SCHEMA_VERSION
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
            "runs": False,
            "live_events": False,
            "replay": False,
            "calibration": False,
            "bundles": False,
            "ui": "optional-not-built-in-this-slice",
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
