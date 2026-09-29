from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import LOGS_DIR

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | [%(name)s] | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

class DailyTimedRotatingFileHandler(TimedRotatingFileHandler):
    def __init__(self, directory: Path, prefix: str = "app_", **kwargs):
        self.directory = directory
        self.prefix = prefix
        current_date = datetime.now().strftime("%Y-%m-%d")
        initial_filename = directory / f"{prefix}{current_date}.log"
        super().__init__(
            filename=str(initial_filename),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            **kwargs,
        )

    def doRollover(self):
        super().doRollover()
        next_date = datetime.now().strftime("%Y-%m-%d")
        self.baseFilename = os.path.abspath(str(self.directory / f"{self.prefix}{next_date}.log"))

def setup_logging() -> logging.Logger:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(isinstance(h, DailyTimedRotatingFileHandler) for h in root_logger.handlers):
        formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

        file_handler = DailyTimedRotatingFileHandler(directory=LOGS_DIR, prefix="app_")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
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

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
