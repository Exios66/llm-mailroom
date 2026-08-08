class TestLoggingSetup:
    def _reset(self, monkeypatch):
        monkeypatch.setattr("pipeline.logging._configured", False)

    def test_pretty_console(self, monkeypatch):
        self._reset(monkeypatch)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        from pipeline.logging import setup_logging
        import structlog

        setup_logging(level="INFO", log_format="pretty")
        structlog.get_logger("test").info("hello", key="value")  # must not raise

    def test_json_format(self, monkeypatch, capsys):
        self._reset(monkeypatch)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        from pipeline.logging import setup_logging
        import structlog

        setup_logging(level="DEBUG", log_format="json")
        structlog.get_logger("test-json").info("json_event", k=1)
        captured = capsys.readouterr()
        assert "json_event" in captured.out
        assert '"k": 1' in captured.out

    def test_idempotent(self, monkeypatch):
        self._reset(monkeypatch)
        from pipeline.logging import setup_logging

        setup_logging()
        setup_logging()  # second call is a no-op
