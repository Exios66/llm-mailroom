"""Standalone inspection app for the native diagnostics workbench.

Import-safe by construction: this module never imports ``api.main`` (whose
module scope starts score/embedding warmups) and never starts the watcher,
Gmail intake, status mail, relations effects or a tracing backend. Opening the
tool inspects code; it does not process documents.

The React screen is optional. Without built assets ``/diagnostics`` serves a
small accessible page explaining how to build it; the JSON API keeps working.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .api import MOUNT, router
from .models import DiagnosticsError

STATIC_ROOT = Path(__file__).with_name("static")


def static_root() -> Path:
    """Built assets directory; MAILROOM_DIAGNOSTICS_STATIC overrides for dev builds."""
    override = os.environ.get("MAILROOM_DIAGNOSTICS_STATIC", "").strip()
    return Path(override).expanduser() if override else STATIC_ROOT

_PLACEHOLDER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mailroom diagnostics</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.55 system-ui, sans-serif; margin: 0; }
  main { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  h1 { font-size: 1.6rem; margin: 0 0 .35rem; }
  p.lede { margin: 0 0 1.75rem; opacity: .8; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre { padding: .9rem 1rem; overflow-x: auto; border-radius: .5rem;
        background: color-mix(in srgb, currentColor 8%, transparent); }
  ul { padding-left: 1.2rem; }
  a { color: inherit; }
  .note { border-left: .25rem solid currentColor; padding-left: .9rem; opacity: .85; }
</style>
</head>
<body>
<main>
  <h1>Mailroom pipeline diagnostics</h1>
  <p class="lede">Read-only inspection of the LangGraph wiring in this checkout.</p>
  <p class="note">The optional React workbench is not built in this checkout.
  The JSON API and CLI work without it.</p>
  <h2>Use the API</h2>
  <ul>
    <li><a href="/v1/diagnostics/capabilities">/v1/diagnostics/capabilities</a></li>
    <li><a href="/v1/diagnostics/snapshot">/v1/diagnostics/snapshot</a></li>
    <li><a href="/v1/diagnostics/findings">/v1/diagnostics/findings</a></li>
  </ul>
  <h2>Build the screen (planned phase D1.1)</h2>
  <pre>npm --prefix tools/pipeline-diagnostics/frontend install
npm --prefix tools/pipeline-diagnostics/frontend run build</pre>
  <p>Commands and current limitations: <code>tools/pipeline-diagnostics/QUICKSTART.md</code>.</p>
</main>
</body>
</html>
"""


def static_available() -> bool:
    """True only when a built index exists; source files alone do not count."""
    return (static_root() / "index.html").is_file()


def mount_ui(application: FastAPI) -> None:
    """Mount the optional screen under ``/diagnostics`` (never blocks startup)."""
    root = static_root()
    if (root / "index.html").is_file():

        @application.get(MOUNT, include_in_schema=False)
        async def diagnostics_index() -> FileResponse:
            return FileResponse(root / "index.html")

        @application.get(f"{MOUNT}/{{path:path}}", include_in_schema=False)
        async def diagnostics_path(path: str) -> FileResponse:
            candidate = (root / path).resolve()
            resolved_root = root.resolve()
            if candidate.is_file() and resolved_root in candidate.parents:
                return FileResponse(candidate)
            # SPA fallback is restricted to this mount; unknown paths are not
            # echoed back and never escape the static root.
            return FileResponse(resolved_root / "index.html")
    else:

        @application.get(MOUNT, include_in_schema=False)
        async def diagnostics_missing() -> HTMLResponse:
            return HTMLResponse(_PLACEHOLDER)

        @application.get(f"{MOUNT}/{{path:path}}", include_in_schema=False)
        async def diagnostics_missing_path(path: str) -> HTMLResponse:
            return HTMLResponse(_PLACEHOLDER, status_code=404)


async def _diagnostics_error(_request: Request, exc: DiagnosticsError) -> JSONResponse:
    # Environment/graph errors are actionable but may contain private paths;
    # return the validated code/message, never a traceback.
    return JSONResponse(
        {"ok": False, "error": {"code": exc.code, "message": exc.message}},
        status_code=503,
    )


async def _unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": "runtime-error", "message": "diagnostics request failed"}},
        status_code=500,
    )


def create_app() -> FastAPI:
    application = FastAPI(
        title="Mailroom Diagnostics",
        description=(
            "Read-only native inspection of the pipeline graph. "
            "No watcher, document processing, hosted tracing or model call."
        ),
        version=__version__,
        openapi_url="/v1/diagnostics/openapi.json",
        docs_url="/v1/diagnostics/docs",
        redoc_url=None,
    )
    application.include_router(router, prefix="/v1/diagnostics")
    application.add_exception_handler(DiagnosticsError, _diagnostics_error)
    application.add_exception_handler(Exception, _unexpected_error)

    @application.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "tool": "mailroom-diagnostics",
            "version": __version__,
            "ui": static_available(),
        }

    mount_ui(application)
    return application


app = create_app()


def prepare_bind(host: str) -> None:
    """Load repository configuration, then enforce the off-loopback token rule.

    ``load_env`` runs here rather than at import so a token configured only in
    ``.env`` counts, matching the producer's entrypoints. The rule is unchanged:
    loopback is tokenless-friendly; any other bind needs a live token.
    """
    from api.auth import _host_is_loopback, active_api_tokens
    from pipeline.env import load_env

    load_env()
    if not _host_is_loopback(host) and not active_api_tokens():
        raise SystemExit(
            "refusing an off-loopback diagnostics bind without MAILROOM_API_TOKEN "
            "or MAILROOM_API_TOKENS"
        )


def serve(host: str = "127.0.0.1", port: int = 8100) -> None:
    """Serve the standalone app after loading env and validating the bind."""
    prepare_bind(host)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
