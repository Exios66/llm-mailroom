"""Structured logging setup for Mailroom entrypoints.

Configures `structlog` once per process: level from `LOG_LEVEL` (default INFO),
renderer from `LOG_FORMAT` (`json` for machine-readable logs, `pretty` for the
dev console). Noisy third-party loggers (httpx, openai, langfuse,
opentelemetry) are silenced to WARNING.

Call `setup_logging()` right after `load_env()` in every process entrypoint
(watcher, API, ops monitor) and standalone script main(). Idempotent.
"""

import logging
import os

import structlog

_configured = False

NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "openai",
    "langfuse",
    "opentelemetry",
    "aiosqlite",
    "urllib3",
    "watchdog",
)


def setup_logging(level: str | None = None, log_format: str | None = None) -> None:
    global _configured
    if _configured:
        return

    level = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    log_format = (log_format or os.environ.get("LOG_FORMAT") or "pretty").strip().lower()

    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(message)s")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
