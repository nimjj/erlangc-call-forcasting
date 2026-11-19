from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from app.core.forecasting.stl import build_stl_forecast
from app.core.forecasting.aggregates import build_dashboard_aggregates
from app.workers.compute import release_slot, run_in_process
from app.workers.task_manager import task_manager
from app.logger import logger


def build_forecast_response(
    file_paths: Sequence[Path],
    filenames: Sequence[str],
    interval_minutes: int,
    forecast_days: int,
    seasonal_period: int | None,
    trend_lookback_days: int,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float,
    max_occupancy: float,
    max_agents: int,
    include_forecast_rows: bool,
    annual_seasonality: str = "auto",
) -> dict:
    """The /stl-forecast response. Top level so it can run in a worker process."""
    forecast, summary = build_stl_forecast(
        file_paths=file_paths,
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
    )
    response = {
        **summary,
        "parameters": {
            "interval_minutes": interval_minutes,
            "forecast_days": summary["days"],
            "seasonal_period": summary["seasonal_period"],
            "trend_lookback_days": trend_lookback_days,
            "annual_seasonality": annual_seasonality,
            "target_seconds": target_seconds,
            "target_service_level_percent": target_service_level,
            "shrinkage_percent": shrinkage,
            "max_occupancy_percent": max_occupancy,
            "max_agents": max_agents,
        },
        "charts": build_dashboard_aggregates(forecast),
    }
    if include_forecast_rows:
        serializable = forecast.copy()
        serializable["interval_start"] = serializable["interval_start"].dt.strftime("%Y-%m-%dT%H:%M:%S")
        response["forecast"] = serializable.to_dict(orient="records")
    else:
        response["forecast"] = []
    return response


def run_stl_forecast_worker(job_id: str, temp_dir: Path | None = None, **params) -> dict:
    """Background job: the forecast runs in the process pool; the endpoint already reserved its slot."""
    logger.info(f"[Job {job_id}] Worker started for {len(params['file_paths'])} file(s)")
    task_manager.update_job(job_id, status="processing", progress=10, message="Forecasting demand and staffing...")
    try:
        response = run_in_process(build_forecast_response, **params)
        logger.info(f"[Job {job_id}] Completed. Predicted calls: {response['total_predicted_calls']:,}")
        return response
    except (ValueError, RuntimeError) as exc:
        # Bad input, not a fault: no stack trace at ERROR.
        logger.warning(f"[Job {job_id}] Rejected: {exc}")
        raise
    except Exception:
        logger.exception(f"[Job {job_id}] Failed")
        raise
    finally:
        release_slot()
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
