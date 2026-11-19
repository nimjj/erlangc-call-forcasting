from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from app.config import JOB_TTL_SECONDS, MAX_WORKER_THREADS

@dataclass
class JobInfo:
    job_id: str
    status: str
    progress: int = 0
    message: str = "Job queued"
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[float] = None

class TaskManager:
    """
    In-memory background task manager for one process.

    Jobs live only in this process: they are gone after a restart, and with several replicas or uvicorn
    workers a poll can reach a process that never saw the job (404). Finished jobs are dropped after
    JOB_TTL_SECONDS. How many may wait is bounded by app.workers.compute's slots.
    """
    def __init__(self, max_workers: int = MAX_WORKER_THREADS, ttl_seconds: int = JOB_TTL_SECONDS):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="async-worker")
        self._jobs: Dict[str, JobInfo] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl
        expired = [job_id for job_id, job in self._jobs.items()
                   if job.finished_at is not None and job.finished_at < cutoff]
        for job_id in expired:
            del self._jobs[job_id]

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._evict_expired()
            self._jobs[job_id] = JobInfo(
                job_id=job_id,
                status="queued",
                progress=0,
                message="Job queued for processing",
            )
        return job_id

    def update_job(
        self,
        job_id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            job = self._jobs[job_id]
            if status is not None:
                job.status = status
                if status in {"completed", "failed"}:
                    job.finished_at = time.monotonic()
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            job.updated_at = datetime.now(timezone.utc)

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        with self._lock:
            self._evict_expired()
            return self._jobs.get(job_id)

    def submit_task(self, job_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        # Stays "queued" until a worker thread picks it up.
        self._executor.submit(self._run_wrapper, job_id, fn, *args, **kwargs)

    def _run_wrapper(self, job_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            result = fn(job_id, *args, **kwargs)
            self.update_job(
                job_id,
                status="completed",
                progress=100,
                message="Processing completed successfully.",
                result=result,
            )
        except (ValueError, RuntimeError) as exc:
            # Input problems: the caller can act on the message.
            self.update_job(job_id, status="failed", progress=100, message=f"Processing failed: {exc}", error=str(exc))
        except Exception:
            # Anything else stays in the service log, not in the response.
            self.update_job(
                job_id, status="failed", progress=100,
                message="Processing failed unexpectedly.", error="Internal error; see the service logs.",
            )

task_manager = TaskManager()
