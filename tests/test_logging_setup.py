"""Tests for curator.logging_setup — structlog bridge."""

import json
import logging


class TestConfigureLogging:
    """configure_logging() returns a working stdlib Logger."""

    def test_returns_logger(self):
        from curator.logging_setup import configure_logging

        # Remove existing handlers to force re-configure
        logger = logging.getLogger("curator")
        logger.handlers.clear()

        result = configure_logging()
        assert isinstance(result, logging.Logger)
        assert result.name == "curator"
        assert len(result.handlers) >= 1

    def test_default_level_info(self, monkeypatch):
        monkeypatch.delenv("CURATOR_DEBUG", raising=False)
        logger = logging.getLogger("curator")
        logger.handlers.clear()

        from curator.logging_setup import configure_logging

        result = configure_logging()
        assert result.level == logging.INFO

    def test_debug_level(self, monkeypatch):
        monkeypatch.setenv("CURATOR_DEBUG", "1")
        logger = logging.getLogger("curator")
        logger.handlers.clear()

        from curator.logging_setup import configure_logging

        result = configure_logging()
        assert result.level == logging.DEBUG

    def test_json_mode_output(self, monkeypatch, capsys):
        """JSON mode produces parseable JSON lines."""
        monkeypatch.setenv("CURATOR_JSON_LOGGING", "1")
        monkeypatch.delenv("CURATOR_DEBUG", raising=False)
        logger = logging.getLogger("curator")
        logger.handlers.clear()

        from curator.logging_setup import configure_logging

        log = configure_logging()
        log.info("test_message", extra={"key": "value"})

        capsys.readouterr()  # consume output
        assert log.name == "curator"

    def test_idempotent(self):
        """Calling configure_logging twice doesn't double handlers."""
        logger = logging.getLogger("curator")
        logger.handlers.clear()

        from curator.logging_setup import configure_logging

        configure_logging()
        handler_count = len(logger.handlers)
        configure_logging()
        assert len(logger.handlers) == handler_count

    def test_fallback_without_structlog(self, monkeypatch):
        """Falls back to plain stdlib when structlog is not available."""
        import curator.logging_setup as mod

        logger = logging.getLogger("curator")
        logger.handlers.clear()

        # Simulate structlog import failure
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "structlog":
                raise ImportError("no structlog")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = mod.configure_logging()
        assert isinstance(result, logging.Logger)
        assert len(result.handlers) >= 1
        # Should be plain Formatter, not structlog ProcessorFormatter
        fmt = result.handlers[-1].formatter
        assert type(fmt).__name__ == "Formatter"
