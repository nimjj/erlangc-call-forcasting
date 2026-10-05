from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_FORECAST_ROWS = 40_000
MAX_AGENT_COUNT = 10_000
MAX_FORECAST_DAYS = 3_650

MAX_WORKER_THREADS = 8

# Comma-separated origins allowed to call the API from a browser; empty disables CORS.
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
