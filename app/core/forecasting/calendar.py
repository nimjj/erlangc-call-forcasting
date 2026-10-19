"""US holiday calendar shared by the annual forecaster and the synthetic data generator.

This module imports only pandas so tools can load it without the rest of the app.
"""
from __future__ import annotations

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

CLOSURE = "closure"
REDUCED = "reduced"
PRE_HOLIDAY_PEAK = "pre_holiday_peak"
NORMAL = "normal"

# Used when the history has not observed a holiday; learned multipliers replace these.
DEFAULT_MULTIPLIERS = {CLOSURE: 0.15, REDUCED: 0.6, PRE_HOLIDAY_PEAK: 1.6, NORMAL: 1.0}

HOLIDAY_CLASSES = {
    "New Year's Day": CLOSURE,
    "Birthday of Martin Luther King, Jr.": REDUCED,
    "Washington's Birthday": NORMAL,
    "Memorial Day": CLOSURE,
    "Juneteenth National Independence Day": REDUCED,
    "Independence Day": REDUCED,
    "Labor Day": CLOSURE,
    "Columbus Day": NORMAL,
    "Veterans Day": NORMAL,
    "Thanksgiving Day": CLOSURE,
    "Christmas Day": CLOSURE,
    "Thanksgiving Eve": PRE_HOLIDAY_PEAK,
    "Day after Thanksgiving": REDUCED,
    "Christmas Eve": NORMAL,
    "New Year's Eve": NORMAL,
}


def us_holidays(start, end) -> pd.Series:
    """Observed US federal holidays plus Thanksgiving eve/after and Christmas/New Year's eves.

    Returns a Series of holiday names indexed by normalized date, limited to [start, end].
    """
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    # Pad so the derived days around a holiday just outside the range are still found.
    federal = USFederalHolidayCalendar().holidays(
        start - pd.Timedelta(days=7), end + pd.Timedelta(days=7), return_name=True,
    )
    days: dict[pd.Timestamp, str] = {pd.Timestamp(day).normalize(): name for day, name in federal.items()}
    for day, name in list(days.items()):
        if name == "Thanksgiving Day":
            days.setdefault(day - pd.Timedelta(days=1), "Thanksgiving Eve")
            days.setdefault(day + pd.Timedelta(days=1), "Day after Thanksgiving")
    for year in range(start.year - 1, end.year + 1):
        for day, name in ((pd.Timestamp(year, 12, 24), "Christmas Eve"), (pd.Timestamp(year, 12, 31), "New Year's Eve")):
            if day.dayofweek < 5:
                days.setdefault(day, name)
    holidays = pd.Series(days, dtype=object).sort_index()
    return holidays.loc[(holidays.index >= start) & (holidays.index <= end)]


def holiday_class(name: str) -> str:
    return HOLIDAY_CLASSES.get(name, NORMAL)
