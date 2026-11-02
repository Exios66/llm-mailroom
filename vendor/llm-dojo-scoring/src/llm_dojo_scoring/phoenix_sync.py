"""Phoenix / OTLP local trace-sink reader.

The (standalone) llm-entity-extraction pipeline's default tracing backend is Arize Phoenix
running locally (``http://localhost:6006``), which ingests OpenTelemetry spans
via its OTLP HTTP receiver. This module lets the dojo suite check that sink
and pull span data when it is up — and degrade gracefully (clear message, no
crash) when it is not, since the sink is only alive during/after local runs.

Phoenix has no stable public REST API for reading traces; the supported access
paths are:
- the ``arize-phoenix`` Python client (``phoenix.session.client`` /
  ``px.Client``) for querying exported spans, and
- the SQL query endpoint used by the UI.

We therefore use the Python client when available and report the sink status
when it is not. The dojo suite never depends on Phoenix being up.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_PHOENIX_URL = os.environ.get("PHOENIX_URL", "http://localhost:6006")


def phoenix_available(base_url: str = DEFAULT_PHOENIX_URL, timeout: float = 3.0) -> bool:
    """True when a Phoenix / OpenInference sink answers on ``base_url``."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/v1/healthcheck", timeout=timeout):
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/", timeout=timeout):
            return True
    except Exception:
        return False


@dataclass
class PhoenixStatus:
    """Sink reachability and optional client error for a Phoenix endpoint."""

    available: bool
    base_url: str
    client_error: str | None = None

    def describe(self) -> str:
        """Describe sink reachability and how to start an unavailable sink."""
        if self.available:
            return f"Phoenix sink reachable at {self.base_url}."
        return (f"Phoenix sink not reachable at {self.base_url} — start it "
                f"(e.g. `python -m phoenix.server.main serve`) before syncing "
                f"local spans.")


def check_phoenix(base_url: str = DEFAULT_PHOENIX_URL) -> PhoenixStatus:
    """Best-effort status probe of the local trace sink."""
    return PhoenixStatus(available=phoenix_available(base_url), base_url=base_url)


class PhoenixClient:
    """Read spans from a running Phoenix sink via the Python client.

    The client is imported lazily so the dojo package works without
    ``arize-phoenix`` installed. If the sink is down or the client is missing,
    :meth:`spans` returns ``None`` and :attr:`error` carries the reason.
    """

    def __init__(self, base_url: str = DEFAULT_PHOENIX_URL):
        """Normalize the endpoint and try to initialize an available client."""
        self.base_url = base_url.rstrip("/")
        self._client: Any = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        """Probe the sink and load a modern or legacy client, recording failures."""
        if not phoenix_available(self.base_url):
            self.error = f"no Phoenix sink at {self.base_url}"
            return
        # arize-phoenix-client (phoenix>=20): Client(base_url=...) with
        # c.projects.list() and c.spans.get_spans_dataframe(project_name=...).
        try:
            from phoenix.client import Client as NewClient

            self._client = NewClient(base_url=self.base_url)
            self._modern = True
            return
        except Exception:
            pass
        # arize-phoenix (legacy): phoenix.session.client.Client(endpoint=...).
        try:
            from phoenix.session.client import Client as SessionClient

            self._client = SessionClient(endpoint=self.base_url)
            self._modern = False
        except Exception as exc:  # pragma: no cover
            self.error = f"arize-phoenix not importable: {exc}"

    @property
    def ready(self) -> bool:
        """Return whether a Phoenix Python client was initialized."""
        return self._client is not None

    def _spans_modern(self, project: str, limit: int | None) -> Optional[list[dict]]:
        """Fetch project spans through the modern client and optionally limit rows."""
        df = self._client.spans.get_spans_dataframe(project_name=project)
        if df is None or df.empty:
            return []
        records = df.to_dict(orient="records")
        return records[:limit] if limit else records

    def _spans_legacy(self, project: str, limit: int | None) -> Optional[list[dict]]:
        """Fetch project spans through the legacy client and optionally limit rows."""
        df = self._client.get_spans_dataframe(project_name=project)
        if df is None or df.empty:
            return []
        records = df.to_dict(orient="records")
        return records[:limit] if limit else records

    def spans(self, project_name: str | None = None, limit: int | None = None) -> Optional[list[dict]]:
        """Query project spans as a list of dicts (or None when unavailable).

        Project name defaults to ``PHOENIX_PROJECT`` / "default". Span rows
        carry name, kind, start/end time, status, and context (trace/span id).
        """
        if not self.ready:
            return None
        try:
            project = project_name or os.environ.get("PHOENIX_PROJECT", "default")
            if getattr(self, "_modern", False):
                return self._spans_modern(project, limit)
            return self._spans_legacy(project, limit)
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            return None

    def describe(self) -> str:
        """Describe client readiness or the recorded initialization error."""
        if self.ready:
            return f"Phoenix client connected to {self.base_url}."
        return f"Phoenix client unavailable: {self.error or 'unknown reason'}."


__all__ = ["phoenix_available", "check_phoenix", "PhoenixClient",
           "PhoenixStatus", "DEFAULT_PHOENIX_URL"]
