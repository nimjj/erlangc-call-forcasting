from __future__ import annotations

CDR_COLUMNS = [
    "source",
    "destination",
    "call_datetime",
    "duration",
    "disposition",
    "unique_id",
    "caller_id",
]

FORECAST_COLUMNS = [
    "interval_start",
    "date",
    "day_of_year",
    "month",
    "day",
    "weekday",
    "weekday_name",
    "hour",
    "minute",
    "call_volume",
    "aht_seconds",
    "traffic_erlangs",
    "raw_agents",
    "scheduled_agents",
    "service_level_percent",
    "probability_waiting_percent",
    "occupancy_percent",
    "asa_seconds",
]

SHIFT_DEFINITIONS = [
    {
        "code": "NIGHT",
        "name": "Night",
        "start_hour": 0,
        "end_hour": 8,
        "label": "00:00-08:00",
    },
    {
        "code": "MORNING",
        "name": "Morning",
        "start_hour": 8,
        "end_hour": 16,
        "label": "08:00-16:00",
    },
    {
        "code": "EVENING",
        "name": "Evening",
        "start_hour": 16,
        "end_hour": 24,
        "label": "16:00-00:00",
    },
]
