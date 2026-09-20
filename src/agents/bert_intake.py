"""BERT intake fast-path lane (mailroom-issues #85 M6a / #98).

Deterministic local-model classification consumed inside the intake node.
Runs the sibling ``mailroom-ml`` package when it is installed and enabled,
and ALWAYS returns a handoff-shaped dict — fail-open by construction: a
missing package, missing bundle, or model error degrades to the
deterministic clerk (``routing_path: clerk_only``), never crashes intake.

Handoff contract (v1, consumed by ``state.intake_handoff`` + the terminal
manifest's ``intake.bert`` block):

    {
      "available": bool,
      "reason": "flag_off" | "no_package" | "no_model" | "error" | "ok",
      "method": "bert" | "deterministic",
      "routing_path": "fast_path" | "clerk_only",
      # present when available: the mailroom_ml classify_document result,
      # flattened at top level — status, doc_type, subclass, route, score,
      # quality, calibrated_confidence, subclass_confidence, n_windows,
      # artifact_sha, agreement, margin, guard_failures, elapsed_ms.
    }

The ``bert_triage`` dict is the same object; the sorter's Tier-1 prior-scoped
lane (#108) consumes it via :func:`format_bert_type_prior` on the existing
``intake_prior=`` channel — no prompt mutation, zero new LLM calls.

Env knobs: ``MAILROOM_BERT_INTAKE`` (gate, default OFF), ``ML_MODEL_DIR``
(bundle dir, consumed by mailroom_ml), ``MAILROOM_BERT_DEBUG`` (debug-capture
kill switch; default ON like the triage/relations precedents).
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_GATE_ENV = "MAILROOM_BERT_INTAKE"
_DEBUG_ENV = "MAILROOM_BERT_DEBUG"

#: Reason the lane is unavailable/not used (unified fail-open vocabulary).
FLAG_OFF = "flag_off"
NO_PACKAGE = "no_package"
NO_MODEL = "no_model"
ERROR = "error"
OK = "ok"


def bert_intake_enabled() -> bool:
    """Whether the BERT intake pass is on (default: OFF — explicit opt-in).

    Set ``MAILROOM_BERT_INTAKE=1`` to enable. Fail-open contract: with the
    gate off the lane still emits a ``{available: false, reason: flag_off}``
    handoff so downstream consumers can rely on its presence.
    """
    return str(os.environ.get(_GATE_ENV, "0")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _load_mailroom_ml() -> tuple[Any | None, str | None]:
    """Lazily import the sibling ``mailroom_ml`` package (never raises).

    Returns ``(module, None)`` on success or ``(None, reason)`` — depending
    on the failure mode. The package is an undeclared sibling: when it is
    not installed the lane stays fully deterministic.
    """
    try:
        import mailroom_ml  # type: ignore[import-not-found]

        return mailroom_ml, None
    except ImportError as exc:
        logger.debug(
            "bert_package_unavailable",
            error=str(exc)[:200],
            hint="install the sibling mailroom-ml package to enable the BERT fast path",
        )
        return None, NO_PACKAGE
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("bert_package_load_failed", error=str(exc)[:200])
        return None, ERROR


def _debug_dir(filename: str | None) -> Path:
    """``<base>/debug/bert_intake/<UTC>_<slug>/`` — the triage/relations shape.

    Sanitized filename slug; empty names fall back to ``doc``. Never gated on
    the lane being available — failures write too (debug payloads, not
    excerpts, matching ``_write_triage_debug``).
    """
    from pipeline.bins import get_base_dir

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", (filename or "doc")[:60]) or "doc"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(get_base_dir()) / "debug" / "bert_intake" / f"{stamp}_{slug}"


def _write_bert_debug(
    handoff: dict[str, Any],
    doc_text: str,
    filename: str | None,
) -> None:
    """Best-effort full-I/O capture under ``data/debug/bert_intake/``."""
    if str(os.environ.get(_DEBUG_ENV, "1")).strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    try:
        out = _debug_dir(filename)
        out.mkdir(parents=True, exist_ok=True)
        (out / "input.txt").write_text(doc_text, encoding="utf-8")
        import json as _json

        (out / "result.json").write_text(
            _json.dumps(handoff, indent=2, default=str), encoding="utf-8"
        )
        (out / "meta.json").write_text(
            _json.dumps(
                {
                    "filename": filename,
                    "chars": len(doc_text),
                    "gate": bert_intake_enabled(),
                    "hook": "intake_node",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("bert_debug_write_failed", filename=filename)


def _clerk_handoff(reason: str, **extra: Any) -> dict[str, Any]:
    """The always-emitted deterministic-clerk fallback handoff."""
    handoff: dict[str, Any] = {
        "available": False,
        "reason": reason,
        "method": "deterministic",
        "routing_path": "clerk_only",
    }
    handoff.update(extra)
    return handoff


def run_bert_intake(
    doc_text: str,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    """Run the ModernBERT fast-path triage against ``doc_text``.

    Never raises. The returned handoff is the single contract for both the
    graph state (``bert_triage`` / ``intake_handoff``) and the manifest's
    ``intake.bert`` block.
    """
    start = time.monotonic()
    if not bert_intake_enabled():
        return _clerk_handoff(FLAG_OFF, elapsed_ms=0.0)

    module, reason = _load_mailroom_ml()
    if module is None:
        elapsed = (time.monotonic() - start) * 1000.0
        handoff = _clerk_handoff(reason, elapsed_ms=round(elapsed, 2))
        _write_bert_debug(handoff, doc_text, filename)
        return handoff

    try:
        classifier = module.inference.classify_document  # type: ignore[attr-defined]
        try:
            result = classifier(doc_text, filename=filename)
        except TypeError:
            result = classifier(doc_text)
    except Exception as exc:  # model load / inference / malformed bundle
        logger.exception(
            "bert_classify_failed",
            filename=filename,
            error=str(exc)[:300],
        )
        elapsed = (time.monotonic() - start) * 1000.0
        handoff = _clerk_handoff(ERROR, elapsed_ms=round(elapsed, 2))
        _write_bert_debug(handoff, doc_text, filename)
        return handoff

    if not isinstance(result, dict):
        elapsed = (time.monotonic() - start) * 1000.0
        handoff = _clerk_handoff(ERROR, elapsed_ms=round(elapsed, 2))
        _write_bert_debug(handoff, doc_text, filename)
        return handoff

    # Absent bundle on disk -> the runner reports it with a bundle reason;
    # treat any missing-model marker as no_model, else surface as-is.
    missing_markers = ("no_model", "bundle_missing", "model_missing")
    if result.get("status") in missing_markers or result.get("reason") in missing_markers:
        elapsed = (time.monotonic() - start) * 1000.0
        handoff = _clerk_handoff(NO_MODEL, elapsed_ms=round(elapsed, 2))
        _write_bert_debug(handoff, doc_text, filename)
        return handoff

    elapsed = (time.monotonic() - start) * 1000.0
    handoff: dict[str, Any] = {
        "available": True,
        "reason": OK,
        "method": "bert",
        "routing_path": "fast_path" if result.get("route") == "fast_path" else "clerk_only",
        "elapsed_ms": round(elapsed, 2),
    }
    # Flatten the runner's fields onto the handoff (bounded, whitelisted)
    for key in (
        "status",
        "doc_type",
        "subclass",
        "route",
        "score",
        "quality",
        "calibrated_confidence",
        "subclass_confidence",
        "n_windows",
        "artifact_sha",
        "agreement",
        "margin",
        "guard_failures",
        "reason",
    ):
        if key in result and result[key] is not None:
            handoff[key] = result[key]
    _write_bert_debug(handoff, doc_text, filename)
    return handoff


def format_bert_type_prior(triage: dict[str, Any] | None) -> str:
    """Render the BERT triage as the sorter's Tier-1 prior block (#108).

    Rides the EXISTING ``intake_prior=`` channel (agents/sorter.py — the
    vendored ``sorter_v14`` prompt is never mutated). The prior is only
    emitted on a fast-path route with a concretely-predicted primary class;
    the subclass is explicitly labeled UNVERIFIED so the sorter resolves it
    independently and may overrule the type with cited evidence.

    Returns ``""`` when there is nothing to say (gate off, clerk-only route,
    no class) — callers compose it harmlessly.
    """
    triage = dict(triage or {})
    if not triage.get("doc_type") or triage.get("route") != "fast_path":
        return ""
    confidence = triage.get("calibrated_confidence") or triage.get("confidence")
    lines = [
        "[bert prior — deterministic ModernBERT classifier; the primary class is "
        "VERIFIED, verify the subclass independently; overrule only with cited evidence]",
        f"primary class: {triage['doc_type']}",
    ]
    if confidence is not None:
        lines.append(f"type confidence: {confidence}")
    subclass = triage.get("subclass")
    if subclass:
        lines.append(f"candidate subclass: {subclass} (UNVERIFIED — decide independently)")
    return "\n".join(lines)