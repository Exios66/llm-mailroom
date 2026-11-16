"""Mailroom taxonomy -> llm-dojo-scoring wiring (single thin caller).

hub#62: the taxonomy→Settings mapping now lives ONCE in the package
(``llm_dojo_scoring.configure_from_taxonomy``). This module is the ONLY
mailroom-side caller: it loads ``config/taxonomy.yaml`` via
``pipeline.config.load_config`` and wires it at import time, so every
downstream ``llm_dojo_scoring`` getter (thresholds, ``embedding_enabled``,
``type_bands``, doc-class field-type maps) observes the mailroom calibration
before any scoring call.

Import this module (for the side effect) anywhere scoring runs and you need
the mailroom thresholds — e.g. the pipeline relations node, the API, the
watcher, the pilot runners, ``observability.agent_eval`` and
``observability.local_eval_packs``. It is idempotent and safe to import multiple
times.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def wire_taxonomy() -> None:
    """Wire mailroom's taxonomy.yaml into llm-dojo-scoring settings.

    Best-effort: any failure (config loader unavailable, malformed block)
    leaves the package defaults in place — scoring still works, just with the
    package's built-in thresholds. That is a silent calibration drift, so the
    failure is logged as a warning (``dojo_taxonomy_wiring_failed``) rather than
    swallowed. Idempotent; safe to call repeatedly.
    """
    try:
        from pipeline.config import load_config
        from llm_dojo_scoring import configure_from_taxonomy

        configure_from_taxonomy(load_config())
    except Exception as exc:
        logger.warning(
            "dojo_taxonomy_wiring_failed",
            error=f"{type(exc).__name__}: {exc}",
            exc_info=True,
        )


wire_taxonomy()