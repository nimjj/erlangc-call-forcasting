from __future__ import annotations

"""
Backward-compatibility facade for calculator.py.
Exports all functions and constants from the modular app.core package.
Existing unit tests and notebooks can import calculator directly without changes.
"""

from app.core import (
    CDR_COLUMNS,
    FORECAST_COLUMNS,
    SHIFT_DEFINITIONS,
    read_cdr_csv,
    hms_to_seconds,
    clean_percent,
    infer_single_year,
    preprocess_cdr,
    build_cdr_intervals,
    calculate_traffic,
    average_speed_of_answer,
    required_agents,
    _cached_required_agents,
    compute_interval_staffing,
    build_stl_forecast,
    build_dashboard_aggregates,
    build_shift_requirements,
    calculate_schedule_headcount,
    build_monthly_agent_schedule,
    validate_agent_rest_period,
)

__all__ = [
    "CDR_COLUMNS",
    "FORECAST_COLUMNS",
    "SHIFT_DEFINITIONS",
    "read_cdr_csv",
    "hms_to_seconds",
    "clean_percent",
    "infer_single_year",
    "preprocess_cdr",
    "build_cdr_intervals",
    "calculate_traffic",
    "average_speed_of_answer",
    "required_agents",
    "_cached_required_agents",
    "compute_interval_staffing",
    "build_stl_forecast",
    "build_dashboard_aggregates",
    "build_shift_requirements",
    "calculate_schedule_headcount",
    "build_monthly_agent_schedule",
    "validate_agent_rest_period",
]
