from __future__ import annotations

import asyncio
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import Any, Callable

from fastapi import HTTPException

from app.config import FORECAST_PROCESSES, MAX_QUEUED_COMPUTATIONS

# Forecasts run in worker processes, not threads: the work holds the GIL (pandas row loops, STL), so in a
# thread it starved the event loop and /health timed out -- Docker then restarted the container and lost
# every job. A process pool keeps the API process free to answer.
_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
# Running plus waiting computations; past this the service answers 503 instead of queueing without end.
_slots = threading.BoundedSemaphore(FORECAST_PROCESSES + MAX_QUEUED_COMPUTATIONS)


class WorkerCrashed(Exception):
    """A worker process died (e.g. killed for memory). Not a RuntimeError, so it isn't reported as bad input."""


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=FORECAST_PROCESSES, mp_context=multiprocessing.get_context("spawn"),
            )
        return _pool


def _discard_broken_pool(broken: ProcessPoolExecutor) -> None:
    # A pool with a dead worker refuses every later job; start a fresh one on next use.
    global _pool
    with _pool_lock:
        if _pool is broken:
            _pool = None
    broken.shutdown(wait=False, cancel_futures=True)


def busy() -> HTTPException:
    return HTTPException(status_code=503, detail="The forecasting service is busy. Try again shortly.")


def reserve_slot() -> bool:
    return _slots.acquire(blocking=False)


def release_slot() -> None:
    _slots.release()


def run_in_process(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run fn in the pool and wait for it (from a worker thread). The caller holds a slot."""
    pool = _get_pool()
    try:
        return pool.submit(partial(fn, **kwargs)).result()
    except BrokenProcessPool as exc:
        _discard_broken_pool(pool)
        raise WorkerCrashed("The forecast worker stopped unexpectedly.") from exc


async def compute(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run fn in the pool for a request, or raise 503 when the service is already full."""
    if not reserve_slot():
        raise busy()
    pool = _get_pool()
    try:
        return await asyncio.get_running_loop().run_in_executor(pool, partial(fn, **kwargs))
    except BrokenProcessPool as exc:
        _discard_broken_pool(pool)
        raise WorkerCrashed("The forecast worker stopped unexpectedly.") from exc
    finally:
        release_slot()
