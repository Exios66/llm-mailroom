"""Embedded agent calls must not retain temporary streams or tracing settings."""
import contextlib
import io
import logging

import structlog

from mailroom_diagnostics import cli
from mailroom_diagnostics.snapshot import load_artifact


def test_main_restores_caller_logging_and_tracing(minimal_contract, monkeypatch):
    config = structlog.get_config().copy()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.setLevel(logging.DEBUG)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    probe = logging.getLogger("diagnostic-context-probe")
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "caller-setting")
    def snapshot():
        assert not probe.isEnabledFor(logging.DEBUG)
        return load_artifact(minimal_contract)

    monkeypatch.setattr(cli, "build_workspace_snapshot", snapshot)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["inspect", "--json"]) == 0
        assert structlog.get_config() == config
        assert root.handlers == handlers
        assert root.level == logging.DEBUG
        assert probe.isEnabledFor(logging.DEBUG)
        import os
        assert os.environ["OBSERVABILITY_PROVIDER"] == "caller-setting"
    finally:
        structlog.configure(**config)
        root.handlers = handlers
        root.setLevel(level)


def test_cached_logger_does_not_keep_a_closed_diagnostic_stream(minimal_contract, monkeypatch):
    config = structlog.get_config().copy()
    cached = []
    structlog.configure(cache_logger_on_first_use=True)

    def snapshot():
        logger = structlog.get_logger()
        logger.info("bind-during-inspection")
        cached.append(logger)
        return load_artifact(minimal_contract)

    monkeypatch.setattr(cli, "build_workspace_snapshot", snapshot)
    try:
        with io.StringIO() as stream, contextlib.redirect_stderr(stream):
            assert cli.main(["inspect", "--json"]) == 0
        cached[0].info("after-temporary-stream-closed")
    finally:
        structlog.configure(**config)
