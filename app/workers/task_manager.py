from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.config import MAX_WORKER_THREADS

@dataclass
class JobInfo:
    job_id: str
    status: str
    progress: int = 0
    message: str = "Job queued"
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

class TaskManager:
    """
    In-memory asynchronous background task manager.
    Decoupled and thread-safe. Can be transparently upgraded to Celery/Redis in distributed setups.
    """
    def __init__(self, max_workers: int = MAX_WORKER_THREADS):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="async-worker")
        self._jobs: Dict[str, JobInfo] = {}
        self._lock = threading.Lock()

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
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
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            job.updated_at = datetime.utcnow()

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        with self._lock:
            return self._jobs.get(job_id)

    def submit_task(self, job_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.update_job(job_id, status="processing", progress=5, message="Starting task...")
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
        except Exception as exc:
            self.update_job(
                job_id,
                status="failed",
                progress=100,
                message=f"Processing failed: {exc}",
                error=str(exc),
            )

task_manager = TaskManager()
