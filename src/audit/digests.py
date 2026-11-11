"""Content digests shared by the audit writers, the trace sinks and the verifier (spec §5.5, §5.6, §6).

Both digests are SHA-256 over canonical JSON (sorted keys, ``default=str``), the
same canonical form ``schemas.audit.compute_audit_hash`` uses for ``detail``.
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
    return hashlib.sha256(_canonical({k: v for k, v in detail.items() if k != "recovered_from"})).hexdigest()


def io_sha256(prompt: str | None, completion: str | None) -> str:
    """SHA-256 of one call's full prompt and completion (a visit's ``io_sha256``, §4.4)."""
    return hashlib.sha256(_canonical({"prompt": prompt, "completion": completion})).hexdigest()
