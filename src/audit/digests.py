"""Content digests shared by the audit writers, the trace sinks and the verifier (spec §5.5, §5.6, §6).

This module is the one implementation: ``audit.sinks``, ``audit.writer``,
``audit.reconcile`` and ``audit.verify`` all hash through it, so a stored digest
cannot disagree with the verifier's recomputation.

* ``detail_sha256``: SHA-256 over canonical JSON (sorted keys, ``default=str``,
  ASCII-escaped), the same canonical form ``schemas.audit.compute_audit_hash`` uses.
* ``io_sha256``: SHA-256 over the UTF-8 JSON of ``{prompt, completion}`` with
  sorted keys and non-ASCII text kept as is (``ensure_ascii=False``). Text that
  is not valid Unicode (a lone surrogate) is encoded with ``surrogatepass`` so a
  digest never raises inside a node.
* ``io_sha256_ascii_escaped``: the earlier ``io_sha256`` form, which escaped
  non-ASCII text (``\\uXXXX``). Unreleased builds of the ``sql`` sink stored it,
  so the verifier still accepts it for a stored row; nothing writes it.
  The two forms are identical for ASCII text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, default=str).encode()


def detail_sha256(detail: dict[str, Any]) -> str:
    """SHA-256 of a row ``detail`` without its ``recovered_from`` key.

    A pending record carries this digest (§5.6 step 1); ``audit-reconcile``
    copies it into ``recovered_from.detail_sha256`` and the verifier recomputes it.
    """
    return hashlib.sha256(
        _canonical({k: v for k, v in (detail or {}).items() if k != "recovered_from"})
    ).hexdigest()


def io_sha256(prompt: str | None, completion: str | None) -> str:
    """SHA-256 of one call's full prompt and completion (a visit's ``io_sha256``, §4.4).

    ``None`` is part of the payload: a missing completion differs from an empty one.
    """
    payload = json.dumps({"prompt": prompt, "completion": completion}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def io_sha256_ascii_escaped(prompt: str | None, completion: str | None) -> str:
    """The ASCII-escaped form of ``io_sha256`` that unreleased sink builds stored (verifier fallback only)."""
    return hashlib.sha256(_canonical({"prompt": prompt, "completion": completion})).hexdigest()
