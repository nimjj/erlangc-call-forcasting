from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

LOGS_DIR = BASE_DIR / "logs"

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_FORECAST_ROWS = 40_000
MAX_AGENT_COUNT = 10_000
MAX_FORECAST_DAYS = 3_650

MAX_WORKER_THREADS = 8

# Bounds on the work one request can ask for (an upload's size says nothing about its date span).
MAX_HISTORY_DAYS = int(os.environ.get("MAX_HISTORY_DAYS", 3 * 366))
MAX_HISTORY_INTERVALS = 160_000
# Whole request body, checked while it streams in (before Starlette spools the parts to disk).
MAX_REQUEST_BYTES = MAX_UPLOAD_FILES * MAX_UPLOAD_BYTES + 1024 * 1024

# Worker processes for forecasts, and how many more may wait before requests get 503.
FORECAST_PROCESSES = max(1, int(os.environ.get("FORECAST_PROCESSES", 2)))
MAX_QUEUED_COMPUTATIONS = max(0, int(os.environ.get("MAX_QUEUED_COMPUTATIONS", 4)))
# Finished background jobs (and their results) are dropped after this many seconds.
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 3600))

# When set, every /api request must send it as X-Internal-Key (ciap-api does; browsers should not call
# this service directly).
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# Containers can log to stdout only (LOG_TO_FILE=false) instead of needing a writable logs/ folder.
LOG_TO_FILE = os.environ.get("LOG_TO_FILE", "true").strip().lower() not in {"0", "false", "no"}

# Comma-separated origins allowed to call the API from a browser; empty disables CORS.
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
