from __future__ import annotations

import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.workers.task_manager import task_manager

router = APIRouter(prefix="/jobs", tags=["Jobs & Tasks"])

@router.get("/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = task_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "result": job.result if job.status == "completed" else None,
    }

@router.get("/{job_id}/stream")
async def stream_job_status(job_id: str):
    job = task_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

    async def event_generator():
        last_progress = -1
        last_status = ""
        while True:
            current_job = task_manager.get_job(job_id)
            if not current_job:
                break

            if current_job.progress != last_progress or current_job.status != last_status:
                payload = {
                    "job_id": current_job.job_id,
                    "status": current_job.status,
                    "progress": current_job.progress,
                    "message": current_job.message,
                    "error": current_job.error,
                    "has_result": current_job.result is not None,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                last_progress = current_job.progress
                last_status = current_job.status

            if current_job.status in {"completed", "failed"}:
                break

            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
