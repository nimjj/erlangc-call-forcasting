from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from app.config import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    MAX_FORECAST_DAYS,
    MAX_AGENT_COUNT,
)
from app.core.forecasting.backtest import run_backtest
from app.logger import logger
from app.workers.compute import busy, compute, release_slot, reserve_slot
from app.workers.task_manager import task_manager
from app.workers.jobs import build_forecast_response, run_stl_forecast_worker

router = APIRouter(prefix="/cdr", tags=["Demand Forecasting"])

async def _save_upload(upload: UploadFile, destination: Path) -> None:
    total_bytes = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Uploaded files must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.",
                )
            output.write(chunk)
    if total_bytes == 0:
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'Uploaded file'} is empty.")

def _check_percentages(shrinkage: float) -> None:
    # Percent fields take whole percentages. Between 0 and 1 the core would read a value as a fraction
    # (0.5 -> 50%), so it is refused here rather than guessed.
    if 0 < shrinkage < 1:
        raise HTTPException(status_code=400, detail="shrinkage is a percentage: use 0, or a value from 1 to 99 (e.g. 30 for 30%).")


def _check_uploads(files: list[UploadFile]) -> None:
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one CDR dataset.")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Upload no more than {MAX_UPLOAD_FILES} files at a time.")


async def _save_all(files: list[UploadFile], directory: Path) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    filenames: list[str] = []
    for index, upload in enumerate(files, start=1):
        filename = upload.filename or f"dataset_{index}.csv"
        suffix = Path(filename).suffix or ".csv"
        if suffix.lower() not in {".csv", ".txt"}:
            raise HTTPException(status_code=400, detail=f"Unsupported file type for {filename}.")
        path = directory / f"dataset_{index}{suffix}"
        await _save_upload(upload, path)
        paths.append(path)
        filenames.append(filename)
    return paths, filenames


def _unexpected(what: str, exc: Exception) -> HTTPException:
    # The detail stays in the log; callers (and ciap-web) get a plain message.
    logger.exception(f"{what} failed: {exc}")
    return HTTPException(status_code=500, detail=f"{what} failed unexpectedly. See the forecasting service logs.")


@router.post("/stl-forecast", summary="Forecast demand and staffing", description="Generate STL forecasts and Erlang C staffing, with a labeled rolling-profile fallback for short history.")
async def stl_forecast(
    files: Annotated[list[UploadFile], File(description="One or more CDR CSV files with at least one day of valid records")],
    interval_minutes: Annotated[int, Form()] = 30,
    forecast_days: Annotated[int, Form(description="365 selects automatic duration: 1-6 historical days -> next day; 7-27 -> next 7 days; 28 days to under 12 months -> next calendar month; 12+ months -> next calendar year. Other values override, except histories under 7 days always predict one day.")] = 365,
    seasonal_period: Annotated[int | None, Form(description="STL cycle length in intervals; omitted or zero defaults to one week. At least 2. Fewer than two cycles uses rolling-profile fallback.")] = None,
    trend_lookback_days: Annotated[int, Form(description="Recent days of the STL trend used for linear extrapolation; at least 7, limited to available history. Not used by rolling-profile fallback.")] = 90,
    annual_seasonality: Annotated[Literal["auto", "off"], Form(description="auto uses the daily model with annual seasonality and US holidays when history spans at least 365 days; off keeps the interval-level weekly STL.")] = "auto",
    target_seconds: Annotated[float, Form(gt=0, description="Answer-time target in seconds.")] = 20,
    target_service_level: Annotated[float, Form(ge=1, lt=100, description="Percent of calls answered within target_seconds, e.g. 80.")] = 80,
    shrinkage: Annotated[float, Form(ge=0, lt=100, description="Percent of paid time agents are unavailable, e.g. 30. 0, or between 1 and 99.")] = 30,
    max_occupancy: Annotated[float, Form(ge=1, le=100, description="Most of an agent's time that may go to calls, in percent. Default 85 (85-90 is typical); 100 = no cap.")] = 85,
    max_agents: Annotated[int, Form(ge=1, le=MAX_AGENT_COUNT)] = 1000,
    include_forecast_rows: Annotated[bool, Form()] = True,
) -> dict:
    _check_uploads(files)
    _check_percentages(shrinkage)
    if forecast_days < 1 or forecast_days > MAX_FORECAST_DAYS:
        raise HTTPException(status_code=400, detail=f"forecast_days must be between 1 and {MAX_FORECAST_DAYS}.")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, filenames = await _save_all(files, Path(temp_dir))
            return await compute(
                build_forecast_response,
                file_paths=paths,
                filenames=filenames,
                interval_minutes=interval_minutes,
                forecast_days=forecast_days,
                seasonal_period=seasonal_period,
                trend_lookback_days=trend_lookback_days,
                annual_seasonality=annual_seasonality,
                target_seconds=target_seconds,
                target_service_level=target_service_level,
                shrinkage=shrinkage,
                max_occupancy=max_occupancy,
                max_agents=max_agents,
                include_forecast_rows=include_forecast_rows,
            )
    except HTTPException:
        raise
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected("STL forecast", exc) from exc

@router.post("/stl-forecast/async", summary="Submit a background forecast", description="Accept uploads and return a job ID. Poll /api/v1/jobs/{job_id} for the forecast result or processing errors.")
async def stl_forecast_async(
    files: Annotated[list[UploadFile], File(description="One or more CDR CSV files with at least one day of valid records")],
    interval_minutes: Annotated[int, Form()] = 30,
    forecast_days: Annotated[int, Form(description="365 selects automatic duration: 1-6 historical days -> next day; 7-27 -> next 7 days; 28 days to under 12 months -> next calendar month; 12+ months -> next calendar year. Other values override, except histories under 7 days always predict one day.")] = 365,
    seasonal_period: Annotated[int | None, Form(description="STL cycle length in intervals; omitted or zero defaults to one week. At least 2. Fewer than two cycles uses rolling-profile fallback.")] = None,
    trend_lookback_days: Annotated[int, Form(description="Recent days of the STL trend used for linear extrapolation; at least 7, limited to available history. Not used by rolling-profile fallback.")] = 90,
    annual_seasonality: Annotated[Literal["auto", "off"], Form(description="auto uses the daily model with annual seasonality and US holidays when history spans at least 365 days; off keeps the interval-level weekly STL.")] = "auto",
    target_seconds: Annotated[float, Form(gt=0, description="Answer-time target in seconds.")] = 20,
    target_service_level: Annotated[float, Form(ge=1, lt=100, description="Percent of calls answered within target_seconds, e.g. 80.")] = 80,
    shrinkage: Annotated[float, Form(ge=0, lt=100, description="Percent of paid time agents are unavailable, e.g. 30. 0, or between 1 and 99.")] = 30,
    max_occupancy: Annotated[float, Form(ge=1, le=100, description="Most of an agent's time that may go to calls, in percent. Default 85 (85-90 is typical); 100 = no cap.")] = 85,
    max_agents: Annotated[int, Form(ge=1, le=MAX_AGENT_COUNT)] = 1000,
    include_forecast_rows: Annotated[bool, Form()] = True,
) -> dict:
    """
    Non-blocking Asynchronous Endpoint.
    Returns immediately with a job_id. Status can be polled at /api/v1/jobs/{job_id}
    or streamed via SSE at /api/v1/jobs/{job_id}/stream. Jobs live in this process only and are
    dropped an hour after they finish.
    """
    _check_uploads(files)
    _check_percentages(shrinkage)
    if forecast_days < 1 or forecast_days > MAX_FORECAST_DAYS:
        raise HTTPException(status_code=400, detail=f"forecast_days must be between 1 and {MAX_FORECAST_DAYS}.")
    # The slot is held until the job finishes (released by the worker), so queued jobs are bounded too.
    if not reserve_slot():
        raise busy()

    temp_dir_path = Path(tempfile.mkdtemp(prefix="cdr_upload_"))
    try:
        paths, filenames = await _save_all(files, temp_dir_path)
        job_id = task_manager.create_job()
        task_manager.submit_task(
            job_id,
            run_stl_forecast_worker,
            temp_dir=temp_dir_path,
            file_paths=paths,
            filenames=filenames,
            interval_minutes=interval_minutes,
            forecast_days=forecast_days,
            seasonal_period=seasonal_period,
            trend_lookback_days=trend_lookback_days,
            annual_seasonality=annual_seasonality,
            target_seconds=target_seconds,
            target_service_level=target_service_level,
            shrinkage=shrinkage,
            max_occupancy=max_occupancy,
            max_agents=max_agents,
            include_forecast_rows=include_forecast_rows,
        )
        return {
            "status": "queued",
            "job_id": job_id,
            "message": "Forecast processing started asynchronously.",
            "poll_url": f"/api/v1/jobs/{job_id}",
            "stream_url": f"/api/v1/jobs/{job_id}/stream",
        }
    except Exception:
        release_slot()
        import shutil
        if temp_dir_path.exists():
            shutil.rmtree(temp_dir_path, ignore_errors=True)
        raise

@router.post("/backtest", summary="Backtest the forecast against held-out data", description="Train on records before train_end, forecast the remaining days, and score predictions against actual call volumes, a weekly seasonal-naive baseline, and (with a year of training history) a yearly seasonal-naive baseline.")
async def backtest(
    files: Annotated[list[UploadFile], File(description="One or more CDR CSV files covering both the training and test periods")],
    train_end: Annotated[str | None, Form(description="First held-out date (YYYY-MM-DD). Defaults to 14 days after the first record, the minimum for STL to apply.")] = None,
    interval_minutes: Annotated[int, Form()] = 30,
    seasonal_period: Annotated[int | None, Form()] = None,
    trend_lookback_days: Annotated[int, Form()] = 90,
    annual_seasonality: Annotated[Literal["auto", "off"], Form(description="auto uses the daily model with annual seasonality and US holidays when history spans at least 365 days; off keeps the interval-level weekly STL.")] = "auto",
    target_seconds: Annotated[float, Form(gt=0, description="Answer-time target in seconds.")] = 20,
    target_service_level: Annotated[float, Form(ge=1, lt=100, description="Percent of calls answered within target_seconds, e.g. 80.")] = 80,
    shrinkage: Annotated[float, Form(ge=0, lt=100, description="Percent of paid time agents are unavailable, e.g. 30. 0, or between 1 and 99.")] = 30,
    max_occupancy: Annotated[float, Form(ge=1, le=100, description="Most of an agent's time that may go to calls, in percent. Default 85 (85-90 is typical); 100 = no cap.")] = 85,
    max_agents: Annotated[int, Form(ge=1, le=MAX_AGENT_COUNT)] = 1000,
) -> dict:
    _check_uploads(files)
    _check_percentages(shrinkage)

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, filenames = await _save_all(files, Path(temp_dir))
            return await compute(
                run_backtest,
                file_paths=paths,
                filenames=filenames,
                train_end=train_end or None,
                interval_minutes=interval_minutes,
                seasonal_period=seasonal_period,
                trend_lookback_days=trend_lookback_days,
                annual_seasonality=annual_seasonality,
                target_seconds=target_seconds,
                target_service_level=target_service_level,
                shrinkage=shrinkage,
                max_occupancy=max_occupancy,
                max_agents=max_agents,
            )
    except HTTPException:
        raise
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected("Backtest", exc) from exc
