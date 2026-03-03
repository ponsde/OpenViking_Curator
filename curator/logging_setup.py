"""Structured logging configuration with structlog bridge.

Provides a stdlib ``logging.Logger`` configured through structlog processors.
Toggle JSON output via config settings.

Falls back to plain stdlib formatting if structlog is not installed.
"""

import logging


def configure_logging() -> logging.Logger:
    """Set up and return the ``curator`` logger.

    - JSON mode -> JSON lines via structlog
    - Default -> colored console output via structlog (or plain stdlib fallback)
    - Debug mode -> DEBUG level
    """
    logger = logging.getLogger("curator")

    if logger.handlers:
        return logger

    from .settings import CuratorSettings

    settings = CuratorSettings()
    debug = settings.debug in ("1", "true", "yes", "on")
    json_mode = settings.json_logging == "1"

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
            renderer = structlog.dev.ConsoleRenderer()  # type: ignore[assignment]

        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=shared_processors,  # type: ignore[arg-type]
        )

        structlog.configure(
            processors=shared_processors  # type: ignore[arg-type]
            + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    except ImportError:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")  # type: ignore[assignment]

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    return logger
