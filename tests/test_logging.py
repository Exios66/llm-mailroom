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

    def test_rotating_file_sink(self, monkeypatch, tmp_path):
        self._reset(monkeypatch)
        import logging

        log_file = tmp_path / "mailroom.log"
        monkeypatch.setenv("LOG_FILE", str(log_file))
        monkeypatch.setenv("LOG_MAX_BYTES", "1024")  # tiny → forces rotation
        monkeypatch.setenv("LOG_BACKUP_COUNT", "2")
        from pipeline.logging import setup_logging
        import structlog

        setup_logging(level="INFO", log_format="json")
        for i in range(50):
            structlog.get_logger("rot-test").info("rotating_event", n=i)
        for h in logging.getLogger().handlers:
            h.flush()
        assert log_file.exists()
        assert log_file.stat().st_size > 0

    def test_no_file_sink_without_log_file(self, monkeypatch):
        self._reset(monkeypatch)
        import logging as std_logging

        root = std_logging.getLogger()
        root.handlers = [h for h in root.handlers
                         if "FileHandler" not in type(h).__name__]
        monkeypatch.delenv("LOG_FILE", raising=False)
        from pipeline.logging import setup_logging

        setup_logging(level="INFO", log_format="json")
        handlers = [h for h in root.handlers
                    if "FileHandler" in type(h).__name__]
        assert handlers == []
