"""Braintrust tracing backend — alternative to Langfuse.

Switch to it with `OBSERVABILITY_PROVIDER=braintrust` and set:

  BRAINTRUST_API_KEY     required (org API key from braintrust.dev)
  BRAINTRUST_PROJECT     project name (default: mailroom)

Wiring: `llm/client.py` → `observability.tracing.instrument_openai_client` →
`instrument_openai_client` here, which calls `braintrust.wrap_openai(client)`.
`wrap_openai` keeps the exact same `.chat.completions.create(...)` interface and
auto-logs every call (prompt, response, tokens, latency) as a Braintrust span.

Graceful degradation: if the API key is absent or init fails, nothing is
changed and the pipeline runs as if tracing were disabled.
"""

import os
import structlog

logger = structlog.get_logger(__name__)

_configured = False


def is_configured() -> bool:
    return bool(os.environ.get("BRAINTRUST_API_KEY"))


def configure() -> bool:
    """Initialize Braintrust (idempotent). Returns True when active."""
    global _configured
    if _configured:
        return True
    if not is_configured():
        logger.debug("braintrust_not_configured_no_api_key")
        return False
    try:
        import braintrust

        braintrust.init(
            project=os.environ.get("BRAINTRUST_PROJECT", "mailroom"),
            api_key=os.environ.get("BRAINTRUST_API_KEY"),
        )
        _configured = True
        logger.info("braintrust_initialized", project=os.environ.get("BRAINTRUST_PROJECT", "mailroom"))
        return True
    except Exception:
        logger.warning("braintrust_initialization_failed", exc_info=True)
        return False


def instrument_openai_client(client):
    """Wrap `client` with Braintrust instrumentation, or return it unchanged."""
    if not configure():
        return client
    try:
        import braintrust

        instrumented = braintrust.wrap_openai(client)
        logger.info("braintrust_openai_client_wrapped")
        return instrumented
    except Exception:
        logger.warning("braintrust_client_wrap_failed", exc_info=True)
        return client


def flush_braintrust():
    if not _configured:
        return
    try:
        import braintrust

        braintrust.flush()
    except Exception:
        pass
