"""Bearer/loopback authorization shared by every Mailroom HTTP app.

This module is deliberately import-safe: it has no startup warmups, watcher,
tracing or pipeline imports, so a diagnostics-only app can reuse the exact
producer policy without importing :mod:`api.main`. ``api.main`` imports the
same helpers, keeping one authorization implementation and its tests.
"""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _csv_tokens(raw: str | None) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def active_api_tokens() -> set[str]:
    """Live bearer tokens: ``MAILROOM_API_TOKEN`` plus ``MAILROOM_API_TOKENS``.

    ``MAILROOM_API_TOKEN_REVOKED`` is subtracted so a rotated key can be
    invalidated without restarting a second process. Empty set = loopback
    unauthenticated mode (bind check still requires a token off-loopback).
    """
    tokens = set()
    # Read per call, like the two CSV vars, so a token set after import counts.
    single = os.environ.get("MAILROOM_API_TOKEN", "").strip()
    if single:
        tokens.add(single)
    tokens |= _csv_tokens(os.environ.get("MAILROOM_API_TOKENS", ""))
    tokens -= _csv_tokens(os.environ.get("MAILROOM_API_TOKEN_REVOKED", ""))
    return tokens


_FORWARDING_HEADERS = (
    "cf-connecting-ip",
    "forwarded",
    "true-client-ip",
    "via",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
)


def _listener_is_loopback(server) -> bool:
    """True when the ASGI ``server`` address (the local socket the request
    arrived on) is loopback. Unknown addresses and unix sockets are not."""
    if not server or server[1] is None:
        return False
    return _host_is_loopback(str(server[0]))


# An HTTP Host value: name or bracketed IP, optional port. urlsplit alone would
# read ``127.0.0.1#@evil.example`` as 127.0.0.1.
_HOST_HEADER_RE = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(:[0-9]*)?")


def _url_names_loopback(url: str) -> bool:
    """True when a URL's host is loopback. ``null`` and malformed URLs are not."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return bool(host) and _host_is_loopback(host)


def _host_is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


def _require_token(request: Request) -> None:
    """Dependency: reject requests without a live bearer token (audit L-2).

    With no live token, only a loopback listener is served. This is checked
    per request from the real listener address, so it also covers a direct
    ``uvicorn api.main:app --host 0.0.0.0``, which never runs
    ``assert_bind_allowed`` (that reads ``MAILROOM_API_HOST``, not uvicorn's
    ``--host``).
    """
    tokens = active_api_tokens()
    if not tokens:
        # A same-host reverse proxy also arrives on loopback; its forwarding
        # headers show the request came from elsewhere. So does a public Host
        # (proxy, tunnel, DNS rebinding), another site's Origin (a browser page
        # posting to localhost) or a cross-site fetch (browsers send no Origin
        # on a plain GET).
        headers = request.headers
        proxied = any(h in headers for h in _FORWARDING_HEADERS)
        host, origin = headers.get("host"), headers.get("origin")
        local_names = (
            (host is None or (_HOST_HEADER_RE.fullmatch(host) and _url_names_loopback(f"//{host}")))
            and (origin is None or _url_names_loopback(origin))
            and headers.get("sec-fetch-site") != "cross-site"
        )
        if not proxied and local_names and _listener_is_loopback(request.scope.get("server")):
            return  # loopback-only dev without a token
        raise HTTPException(
            503,
            "API token not configured: set MAILROOM_API_TOKEN or "
            "MAILROOM_API_TOKENS to serve requests off loopback",
        )
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix) or auth[len(prefix):].strip() not in tokens:
        raise HTTPException(401, "Missing or invalid API token")
