"""Structured logging configuration with structlog bridge.

Provides a stdlib ``logging.Logger`` configured through structlog processors.
Toggle JSON output via ``CURATOR_JSON_LOGGING=1``.

Falls back to plain stdlib formatting if structlog is not installed.
"""

import logging
import os


def configure_logging() -> logging.Logger:
    """Set up and return the ``curator`` logger.

    - ``CURATOR_JSON_LOGGING=1`` → JSON lines via structlog
    - Default → colored console output via structlog (or plain stdlib fallback)
    - ``CURATOR_DEBUG`` → DEBUG level
    """
    logger = logging.getLogger("curator")

    if logger.handlers:
        return logger

    debug = os.getenv("CURATOR_DEBUG", "") in ("1", "true", "yes", "on")
    json_mode = os.getenv("CURATOR_JSON_LOGGING", "0") == "1"

    try:
        import structlog

        shared_processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
        ]

        if json_mode:
            renderer = structlog.processors.JSONRenderer()
        else:
            renderer = structlog.dev.ConsoleRenderer()

        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=shared_processors,
        )

        structlog.configure(
            processors=shared_processors
            + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    except ImportError:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    return logger
