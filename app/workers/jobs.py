from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from app.core.forecasting.stl import build_stl_forecast
from app.core.forecasting.aggregates import build_dashboard_aggregates
from app.workers.task_manager import task_manager
from app.logger import logger

def run_stl_forecast_worker(
    job_id: str,
    file_paths: Sequence[Path],
    filenames: Sequence[str],
    interval_minutes: int,
    forecast_days: int,
    seasonal_period: int | None,
    trend_lookback_days: int,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float,
    max_agents: int,
    include_forecast_rows: bool,
    temp_dir: Path | None = None,
) -> dict:
    logger.info(f"[Job {job_id}] Worker started for {len(file_paths)} file(s): {filenames}")

    def progress_callback(percent: int, message: str) -> None:
        logger.info(f"[Job {job_id}] [{percent}%] {message}")
        task_manager.update_job(
            job_id,
            status="processing",
            progress=percent,
            message=message,
        )

    try:
        forecast, summary = build_stl_forecast(
            file_paths=file_paths,
            filenames=filenames,
            interval_minutes=interval_minutes,
            forecast_days=forecast_days,
            seasonal_period=seasonal_period,
            trend_lookback_days=trend_lookback_days,
            target_seconds=target_seconds,
            target_service_level=target_service_level,
            shrinkage=shrinkage,
            max_agents=max_agents,
            progress_callback=progress_callback,
        )

        progress_callback(95, "Compiling chart visualizations...")
        response = {
            **summary,
            "parameters": {
                "interval_minutes": interval_minutes,
                "forecast_days": summary["days"],
                "seasonal_period": summary["seasonal_period"],
                "trend_lookback_days": trend_lookback_days,
                "target_seconds": target_seconds,
                "target_service_level_percent": target_service_level,
                "shrinkage_percent": shrinkage,
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

        logger.info(f"[Job {job_id}] Completed successfully! Predicted calls: {summary['total_predicted_calls']:,}")
        return response
    except Exception as exc:
        logger.exception(f"[Job {job_id}] Failed with exception: {exc}")
        raise
    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
