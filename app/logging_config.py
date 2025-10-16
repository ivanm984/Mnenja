"""Centralised logging configuration helpers."""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional


DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: Optional[str] = None) -> None:
    """Initialise root logging configuration.

    The log level can be controlled via the ``APP_LOG_LEVEL`` environment
    variable or by passing an explicit ``level`` argument. Output is routed to
    stdout so that container orchestrators can capture it.
    """

    desired_level = (level or os.getenv("APP_LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, desired_level, logging.INFO)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        root_logger.addHandler(handler)
    else:
        # Ensure existing handlers share the same formatter for consistency.
        formatter = logging.Formatter(DEFAULT_LOG_FORMAT)
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    root_logger.setLevel(log_level)


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger with the global configuration applied."""

    setup_logging()
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger", "DEFAULT_LOG_FORMAT"]
