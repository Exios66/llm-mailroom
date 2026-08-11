"""Structured logging setup for Mailroom entrypoints.

Configures `structlog` once per process: level from `LOG_LEVEL` (default INFO),
renderer from `LOG_FORMAT` (`json` for machine-readable logs, `pretty` for the
dev console). Noisy third-party loggers (httpx, openai, langfuse,
opentelemetry) are silenced to WARNING.

Call `setup_logging()` right after `load_env()` in every process entrypoint
(watcher, API, ops monitor) and standalone script main(). Idempotent.

Log retention (audit item 10.3): when `LOG_FILE` is set, every structlog event
is also written to that path as a JSON line with **rotation**
(`LOG_MAX_BYTES` per file, default 10 MB; `LOG_BACKUP_COUNT` kept, default 5) —
the pipeline never writes an unbounded single log file.
"""

import json
import logging
import os
from logging.handlers import RotatingFileHandler

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


class _RotatingFileSink:
    """Structlog processor that emits the rendered event dict to a rotating
    stdlib file handler (bypasses the console renderer; rotation happens in
    ``RotatingFileHandler.emit``)."""

    def __init__(self, handler: RotatingFileHandler, level: int) -> None:
        self._handler = handler
        self._level = level

    def __call__(self, logger, method_name: str, event_dict: dict) -> dict:
        level = getattr(logging, method_name.upper(), logging.INFO)
        if level < self._level:
            return event_dict
        try:
            record = logging.LogRecord(
                logger or "mailroom", level, "", 0,
                json.dumps(event_dict, ensure_ascii=False, default=str),
                (), None,
            )
            self._handler.emit(record)
        except Exception:
            pass  # logging must never take the pipeline down
        return event_dict


def setup_logging(level: str | None = None, log_format: str | None = None) -> None:
    global _configured
    if _configured:
        return

    level = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    log_format = (log_format or os.environ.get("LOG_FORMAT") or "pretty").strip().lower()
    log_level = getattr(logging, level, logging.INFO)

    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # Optional rotating file sink (audit item 10.3 — no unbounded log files).
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        try:
            max_bytes = int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024))
            backup_count = int(os.environ.get("LOG_BACKUP_COUNT", 5))
        except ValueError:
            max_bytes, backup_count = 10 * 1024 * 1024, 5
        handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        processors.append(_RotatingFileSink(handler, log_level))

    processors.append(renderer)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=log_level, format="%(message)s")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
