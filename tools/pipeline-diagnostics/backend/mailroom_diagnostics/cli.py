"""Command line for the native pipeline diagnostics.

Machine mode (``--json``) writes exactly one JSON document to stdout; logs and
human notes go to stderr. This CLI is a preview, never the active worker, and
it never contacts a network service or a model.

Exit codes: 0 clean, 1 observed structural finding, 2 invalid input or runtime
failure, 3 reserved for incomplete evidence (not emitted by this slice).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .checks import run_checks
from .models import CheckReport, DiagnosticsError, Snapshot
from .snapshot import build_workspace_snapshot, load_artifact

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_INVALID = 2
EXIT_INCOMPLETE = 3  # reserved: no partial-evidence mode in this slice


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Do not echo arbitrary argument values into shareable machine output.
        raise DiagnosticsError("invalid-arguments", "invalid command line; use --help")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="mailroom-diagnostics", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="print a workspace-preview or artifact snapshot")
    inspect.add_argument(
        "--contract",
        type=Path,
        metavar="PATH",
        help="read a display-contract JSON artifact instead of compiling the graph",
    )
    inspect.add_argument("--json", action="store_true", help="write one JSON document to stdout")

    check = sub.add_parser("check", help="run structural endpoint/reachability checks")
    check.add_argument(
        "--contract",
        type=Path,
        metavar="PATH",
        help="check a display-contract JSON artifact instead of compiling the graph",
    )
    check.add_argument("--json", action="store_true", help="write one JSON document to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    with _logging_context():
        return _run(raw_args)


def _run(raw_args: list[str]) -> int:
    json_mode = "--json" in raw_args
    try:
        args = build_parser().parse_args(raw_args)
        snapshot = _snapshot(args)
        if args.command == "inspect":
            _emit(snapshot.model_dump(mode="json"), args.json, _inspect_text(snapshot))
            return EXIT_OK
        report = run_checks(snapshot)
        payload = report.model_dump(mode="json")
        blocking = any(finding.severity in ("error", "warning") for finding in report.findings)
        payload["exit_code"] = EXIT_FINDINGS if blocking else EXIT_OK
        _emit(payload, args.json, _check_text(report))
        return int(payload["exit_code"])
    except DiagnosticsError as exc:
        return _fail(exc, json_mode)
    except Exception:  # environment exceptions may contain private paths or payloads
        return _fail(DiagnosticsError("runtime-error", "unable to inspect the pipeline environment"), json_mode)


def _snapshot(args: argparse.Namespace) -> Snapshot:
    if args.contract is not None:
        return load_artifact(args.contract)
    # A preview process must not initialize a tracing backend; set this before
    # the pipeline graph is imported for the first time.
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
    return build_workspace_snapshot()


class _StderrProxy:
    """Cached loggers must not retain a caller's temporary capture stream."""

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


@contextmanager
def _logging_context() -> Iterator[None]:
    """Route CLI logs to stderr, then restore the embedding caller's settings.

    The console entrypoint runs in its own interpreter. Keeping this scope
    reversible also permits sequential in-process agent/test calls.
    """
    import structlog

    config, configured = structlog.get_config().copy(), structlog.is_configured()
    root = logging.getLogger()
    handlers, previous_level = root.handlers[:], root.level
    provider = os.environ.get("OBSERVABILITY_PROVIDER")
    level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    handler = logging.StreamHandler(_StderrProxy())
    try:
        root.handlers = [handler]
        root.setLevel(getattr(logging, level, logging.WARNING))
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=_StderrProxy()))
        yield
    finally:
        if configured:
            structlog.configure(**config)
        else:
            structlog.reset_defaults()
        root.handlers, root.level = handlers, previous_level
        handler.close()
        if provider is None:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)
        else:
            os.environ["OBSERVABILITY_PROVIDER"] = provider


def _emit(payload: dict[str, Any], json_mode: bool, text: str) -> None:
    if json_mode:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False, allow_nan=False) + "\n")
    else:
        sys.stdout.write(text + "\n")


def _fail(exc: DiagnosticsError, json_mode: bool) -> int:
    if json_mode:
        payload = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        print(f"mailroom-diagnostics: {exc.code}: {exc.message}", file=sys.stderr)
    return EXIT_INVALID


def _inspect_text(snapshot: Snapshot) -> str:
    provenance = snapshot.provenance
    return "\n".join(
        [
            f"{provenance.kind}  pipeline={provenance.pipeline_version or 'unknown'}"
            f"  git={provenance.git_sha or 'unknown'}",
            f"graph {snapshot.graph_hash}",
            f"nodes {len(snapshot.graph.nodes)}  edges {len(snapshot.graph.edges)}",
            f"entry {snapshot.graph.start}  exit {snapshot.graph.end}",
        ]
    )


def _check_text(report: CheckReport) -> str:
    lines = [f"{report.provenance.kind}  graph {report.graph_hash}"]
    if not report.findings:
        lines.append(
            "no structural findings (coverage: endpoint consistency, entry reachability)"
        )
    for finding in report.findings:
        lines.append(f"{finding.severity.upper()} {finding.id} {finding.message}")
    return "\n".join(lines)
