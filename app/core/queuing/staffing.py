from __future__ import annotations

from functools import lru_cache
import pandas as pd
from app.core.queuing.erlang_c import required_agents

@lru_cache(maxsize=100_000)
def _cached_required_agents(
    call_volume: int,
    aht_seconds_rounded: float,
    interval_seconds: int,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float,
    max_agents: int,
) -> tuple:
    result = required_agents(
        call_volume=call_volume,
        aht_seconds=aht_seconds_rounded,
        interval_seconds=interval_seconds,
        target_seconds=target_seconds,
        target_service_level=target_service_level,
        shrinkage=shrinkage,
        max_agents=max_agents,
    )
    return (
        result["traffic_erlangs"],
        result["raw_agents"],
        result["scheduled_agents"],
        result["service_level"],
        result["probability_waiting"],
        result["occupancy"],
        result["asa_seconds"],
    )

def compute_interval_staffing(
    future_df: pd.DataFrame,
    interval_seconds: int,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float,
    max_agents: int,
    max_workers: int = 4,
) -> pd.DataFrame:
    metrics_rows = []
    for row in future_df.itertuples(index=False):
        metrics_rows.append(
            _cached_required_agents(
                int(row.call_volume),
                round(float(row.aht_seconds), 2),
                interval_seconds,
                float(target_seconds),
                float(target_service_level),
                float(shrinkage),
                int(max_agents),
            )
        )
    return pd.DataFrame(
        metrics_rows,
        columns=[
            "traffic_erlangs",
            "raw_agents",
            "scheduled_agents",
            "service_level",
            "probability_waiting",
            "occupancy",
            "asa_seconds",
        ],
    )
