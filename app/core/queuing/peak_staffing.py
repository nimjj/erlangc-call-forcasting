"""Shift staffing sized to the busiest interval a shift is likely to see.

A shift needs the Erlang C agents for its busiest interval, and the busiest interval of a day is
almost always busier than the forecast average (the maximum of several random counts exceeds the
maximum of their means). Staffing from the point forecast therefore leaves most shifts short.
Here each shift-day is simulated many times (Poisson arrivals around the forecast, with a
day-level wobble for forecast error), each draw is converted to agents with Erlang C, and the
requested coverage quantile of the shift maximum is returned.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.queuing.staffing import compute_interval_staffing

SIMULATIONS = 300
DAY_LEVEL_SIGMA = 0.15
# Fixed so the same forecast always produces the same schedule.
SEED = 20260101


def simulate_shift_agents(
    volume: np.ndarray,
    aht_seconds: np.ndarray,
    group_starts: np.ndarray,
    coverage_percent: float,
    interval_seconds: int,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float,
    max_agents: int,
    simulations: int = SIMULATIONS,
) -> np.ndarray:
    """Agents per shift such that `coverage_percent` of simulated shifts are fully covered.

    `volume` and `aht_seconds` hold one value per forecast interval, ordered so each shift's
    intervals are contiguous; `group_starts` gives the index where each shift begins.
    """
    volume = np.maximum(np.nan_to_num(np.asarray(volume, dtype=float)), 0.0)
    aht = np.nan_to_num(np.asarray(aht_seconds, dtype=float))
    group_starts = np.asarray(group_starts, dtype=np.int64)
    group_sizes = np.diff(np.append(group_starts, len(volume)))
    group_of_row = np.repeat(np.arange(len(group_starts)), group_sizes)

    rng = np.random.default_rng(SEED)
    level = np.exp(rng.normal(-DAY_LEVEL_SIGMA ** 2 / 2, DAY_LEVEL_SIGMA, size=(simulations, len(group_starts))))
    draws = rng.poisson(volume[None, :] * level[:, group_of_row])

    # Each distinct (calls, AHT) pair goes through Erlang C once.
    aht_values, aht_index = np.unique(np.round(aht, 2), return_inverse=True)
    width = int(draws.max()) + 1
    pairs, inverse = np.unique(aht_index[None, :] * width + draws, return_inverse=True)
    lookup = compute_interval_staffing(
        pd.DataFrame({"call_volume": pairs % width, "aht_seconds": aht_values[pairs // width]}),
        interval_seconds=interval_seconds,
        target_seconds=target_seconds,
        target_service_level=target_service_level,
        shrinkage=shrinkage,
        max_agents=max_agents,
    )["scheduled_agents"].to_numpy()
    agents = lookup[inverse].reshape(draws.shape)

    shift_peak = np.maximum.reduceat(agents, group_starts, axis=1)
    return np.quantile(shift_peak, coverage_percent / 100, axis=0, method="higher").astype(int)
