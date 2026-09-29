from __future__ import annotations

from pyworkforce.queuing import ErlangC
from app.core.ingestion.cleaner import clean_percent

def calculate_traffic(call_volume: float, aht_seconds: float, interval_seconds: float) -> float:
    if call_volume < 0:
        raise ValueError("Call volume cannot be negative.")
    if aht_seconds <= 0:
        raise ValueError("AHT seconds must be greater than 0.")
    if interval_seconds <= 0:
        raise ValueError("Interval seconds must be greater than 0.")
    return (call_volume * aht_seconds) / interval_seconds

def average_speed_of_answer(erlang_c: float, aht_seconds: float, agents: int, traffic: float) -> float:
    agents = int(agents)
    if agents <= 0:
        raise ValueError("Agents must be greater than 0.")
    if agents <= traffic:
        return float("inf")
    return erlang_c * aht_seconds / (agents - traffic)

def required_agents(
    call_volume: float,
    aht_seconds: float,
    interval_seconds: float,
    target_seconds: float,
    target_service_level: float,
    shrinkage: float = 0.0,
    max_agents: int = 1000,
) -> dict:
    target_service_level = clean_percent(target_service_level)
    shrinkage = clean_percent(shrinkage)

    if call_volume < 0:
        raise ValueError("Call volume cannot be negative.")
    if aht_seconds <= 0:
        raise ValueError("AHT seconds must be greater than 0.")
    if interval_seconds <= 0:
        raise ValueError("Interval seconds must be greater than 0.")
    if target_seconds < 0:
        raise ValueError("Target seconds cannot be negative.")
    if not 0 < target_service_level < 1:
        raise ValueError("Target service level must be between 0% and 100%.")
    if not 0 <= shrinkage < 1:
        raise ValueError("Shrinkage must be between 0% and less than 100%.")
    if max_agents <= 0:
        raise ValueError("max_agents must be greater than 0.")

    if call_volume == 0:
        return {
            "traffic_erlangs": 0.0,
            "raw_agents": 0,
            "scheduled_agents": 0,
            "service_level": 1.0,
            "probability_waiting": 0.0,
            "occupancy": 0.0,
            "asa_seconds": 0.0,
        }

    model = ErlangC(
        transactions=call_volume,
        aht=aht_seconds / 60,
        asa=target_seconds / 60,
        interval=interval_seconds / 60,
        shrinkage=shrinkage,
    )
    result = model.required_positions(service_level=target_service_level)

    raw_agents = int(result["raw_positions"])
    scheduled_agents = int(result["positions"])
    if raw_agents > max_agents or scheduled_agents > max_agents:
        raise RuntimeError("Required agents exceed max_agents limit.")

    traffic = calculate_traffic(call_volume, aht_seconds, interval_seconds)
    asa = average_speed_of_answer(
        float(result["waiting_probability"]),
        aht_seconds,
        raw_agents,
        traffic,
    )
    return {
        "traffic_erlangs": float(traffic),
        "raw_agents": raw_agents,
        "scheduled_agents": scheduled_agents,
        "service_level": float(result["service_level"]),
        "probability_waiting": float(result["waiting_probability"]),
        "occupancy": float(result["occupancy"]),
        "asa_seconds": float(asa),
    }
