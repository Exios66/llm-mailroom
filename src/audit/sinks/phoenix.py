"""The ``phoenix`` trace sink (spec §5.5): added automatically when it is usable.

Usable means: the OpenTelemetry packages import, ``PHOENIX_TRACING`` is not
disabled, the hosted-container rule allows it (a Hugging Face Space or Railway
container has no local ``phoenix serve``, so a localhost endpoint is skipped
there), and a 1-second TCP probe of ``PHOENIX_ENDPOINT`` answers. I/O becomes
attributes of an ``audit.io`` span; pending rows become ``audit.pending_row``
spans. Phoenix is write-only here: nothing is read back.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_ENDPOINT = "http://localhost:6006/v1/traces"
PROBE_TIMEOUT_S = 1.0
_LOCAL = ("localhost", "127.0.0.1", "::1", "[::1]")


def endpoint() -> str:
    return os.environ.get("PHOENIX_ENDPOINT", DEFAULT_ENDPOINT)


def _hosted() -> bool:
    return bool(os.environ.get("SPACE_ID") or os.environ.get("HF_SPACE_ID")
                or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))


PROBE_TTL_S = 60.0
_probe_cache: dict[str, tuple[float, bool]] = {}


def probe_cached(url: str) -> bool:
    """Cache each endpoint's TCP probe result for up to one minute."""
    now = time.monotonic()
    hit = _probe_cache.get(url)
    if hit is not None and now - hit[0] < PROBE_TTL_S:
        return hit[1]
    ok = probe(url)
    _probe_cache[url] = (now, ok)
    return ok


def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when something accepts a TCP connection at the endpoint's host and port."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tracer():
    from observability.phoenix_setup import _init_opentelemetry
    from opentelemetry import trace

    _init_opentelemetry()
    return trace.get_tracer("mailroom.audit")


class PhoenixSink:
    name = "phoenix"

    @staticmethod
    def usable() -> bool:
        """Require the SDK, enabled tracing, and a reachable allowed endpoint."""
        try:
            import opentelemetry.sdk.trace  # noqa: F401
        except Exception:
            return False
        from observability.phoenix_setup import phoenix_enabled

        if not phoenix_enabled():
            return False
        url = endpoint()
        if _hosted() and (urlparse(url).hostname or "") in _LOCAL:
            return False
        return probe_cached(url)

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        from audit.digests import io_sha256

        with _tracer().start_as_current_span("audit.io") as span:
            span.set_attribute("audit.visit_id", visit_id)
            span.set_attribute("audit.doc_id", str(doc_id or ""))
            span.set_attribute("input.value", prompt or "")
            span.set_attribute("output.value", completion or "")
            span.set_attribute("audit.io_sha256", io_sha256(prompt, completion))
            return format(span.get_span_context().span_id, "016x")

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        with _tracer().start_as_current_span("audit.pending_row") as span:
            span.set_attribute("audit.doc_id", str(record.get("doc_id") or ""))
            span.set_attribute("audit.record", json.dumps(record, default=str))
            return format(span.get_span_context().span_id, "016x")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["PhoenixSink", "endpoint", "probe", "probe_cached"]
