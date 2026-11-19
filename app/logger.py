from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from app.config import LOG_TO_FILE, LOGS_DIR

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | [%(name)s] | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def _file_handler(formatter: logging.Formatter) -> logging.Handler | None:
    """logs/app.log, rotated at midnight to app.log.YYYY-MM-DD and pruned after 30 days.

    A fixed base name keeps rotation and pruning working (renaming the base file each day wrote a day's
    lines into the previous day's file and never deleted old ones). A logs folder this process can't
    write -- e.g. a host folder mounted into the non-root container -- falls back to stdout only
    instead of stopping the service.
    """
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        handler = TimedRotatingFileHandler(
            LOGS_DIR / "app.log", when="midnight", backupCount=30, encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write(f"File logging disabled ({exc}); logging to stdout only.\n")
        return None
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    return handler

def setup_logging() -> logging.Logger:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(isinstance(h, TimedRotatingFileHandler) for h in root_logger.handlers):
        formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

        file_handler = _file_handler(formatter) if LOG_TO_FILE else None
        if file_handler:
            root_logger.addHandler(file_handler)

        if not any(isinstance(h, logging.StreamHandler) and h.stream == sys.stdout for h in root_logger.handlers):
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            root_logger.addHandler(console_handler)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers = root_logger.handlers

    return logging.getLogger("app")

logger = setup_logging()
