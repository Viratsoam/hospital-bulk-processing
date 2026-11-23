"""Logging configuration.

Kept deliberately small: a single :func:`configure_logging` entry point called
once from the application factory. Batch IDs are threaded into log records via
the ``extra`` argument rather than string concatenation, so downstream tooling
can filter on them.
"""

import logging
import sys

from app.core.config import Settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(settings: Settings) -> None:
    """Configure root logging for the process.

    Idempotent: repeated calls replace handlers rather than stacking them,
    which matters because the app factory can run many times under test.

    Args:
        settings: Application settings supplying the desired log level.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; align them with ours so output is not
    # split across two formats.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger.

    Args:
        name: Usually ``__name__`` of the calling module.

    Returns:
        A standard library logger.
    """
    return logging.getLogger(name)
