"""Logging setup: rotating file logs, disabled by default (TZ §12, §14).

Only events and errors are written; dialog content is never logged.
The logger is reconfigured on every call so config changes apply without restart.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from . import paths

if TYPE_CHECKING:  # pragma: no cover
    from .config import ConfigManager

_LOGGER_NAME = "voiceruna"
_HANDLER_TAG = "voiceruna-file"


def setup_logging(config: "ConfigManager | None" = None) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_vox_tag", None) == _HANDLER_TAG:
            logger.removeHandler(handler)
            handler.close()

    enabled = bool(config.get("logging.enabled", False)) if config else False
    if not enabled:
        logger.addHandler(logging.NullHandler())
        return logger

    level_name = str(config.get("logging.level", "info")).upper() if config else "INFO"
    level = getattr(logging, level_name, logging.INFO)
    rotate_mb = int(config.get("logging.rotate_mb", 5)) if config else 5

    handler = RotatingFileHandler(
        paths.logs_dir() / "sidecar.log",
        maxBytes=max(1, rotate_mb) * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler._vox_tag = _HANDLER_TAG  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
