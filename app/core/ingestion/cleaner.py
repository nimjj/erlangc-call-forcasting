from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
import pandas as pd
from app.core.ingestion.reader import read_cdr_csv

def hms_to_seconds(value) -> Optional[int]:
    if pd.isna(value):
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except (TypeError, ValueError):
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds

def talk_time_to_seconds(value) -> Optional[int]:
    """Seconds from H:MM:SS, MM:SS, a plain number of seconds, or unit text such as "1m 30s"."""
    if pd.isna(value):
        return None
    text = str(value).strip().casefold()
    if not text:
        return None
    if not any(unit in text for unit in ("d", "h", "m", "s")):
        if text.count(":") == 2:
            return hms_to_seconds(text)
        if text.count(":") == 1:
            return hms_to_seconds(f"0:{text}")
        try:
            seconds = float(text)
        except ValueError:
            return None
        return int(seconds) if seconds >= 0 else None
    import re
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)", text)
    if not matches:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return int(sum(float(amount) * units[unit] for amount, unit in matches))

def clean_percent(value) -> float:
    """A percentage as a fraction. "80%", 80 and 0.8 all mean 80%; "1%" and 1 mean 1%.

    Text with a % sign is always a percentage. A bare number of 1 or more is a percentage; below 1 it
    is already a fraction.
    """
    text = str(value).strip()
    number = float(text.replace("%", ""))
    if "%" in text or number >= 1:
        number /= 100
    return number

def infer_single_year(clean_df: pd.DataFrame, filename: str = "dataset") -> int:
    years = sorted(clean_df["call_datetime"].dt.year.dropna().astype(int).unique().tolist())
    if len(years) != 1:
        raise ValueError(f"{filename} must contain exactly one calendar year; found {years or 'none'}.")
    return int(years[0])

def preprocess_cdr(
    file_path: str | Path,
    min_duration_seconds: int = 1,
    max_duration_seconds: int = 4 * 3600,
    include_dispositions: Optional[Sequence[str]] = None,
    raw_frame: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if min_duration_seconds < 0:
        raise ValueError("Minimum duration cannot be negative.")
    if max_duration_seconds < min_duration_seconds:
        raise ValueError("Maximum duration must be >= minimum duration.")

    allowed = {value.casefold() for value in (include_dispositions or ["Answered"])}
    frame = raw_frame.copy() if raw_frame is not None else read_cdr_csv(file_path)

    # format="mixed" parses each value on its own; inferring one format from the first row turned every
    # row written differently into NaT and silently dropped those calls.
    frame["call_datetime"] = pd.to_datetime(frame["call_datetime"], errors="coerce", format="mixed")

    # H:MM:SS (the usual export) is parsed in bulk; anything else -- MM:SS, plain seconds, "1m 30s" --
    # value by value, so a file mixing formats keeps every row.
    text = frame["duration"].astype(str).str.strip()
    hms = text.str.extract(r"^(\d+):(\d{1,2}):(\d{1,2})$").apply(pd.to_numeric, errors="coerce")
    valid_hms = hms[0].notna() & (hms[1] < 60) & (hms[2] < 60)
    dur = pd.Series(float("nan"), index=frame.index, dtype="float64")
    dur.loc[valid_hms] = (hms.loc[valid_hms, 0] * 3600 + hms.loc[valid_hms, 1] * 60 + hms.loc[valid_hms, 2]).astype(float)
    others = ~valid_hms & frame["duration"].notna()
    dur.loc[others] = frame.loc[others, "duration"].map(talk_time_to_seconds).astype("float64")
    frame["duration_seconds"] = dur

    source_ok = frame["source"].notna() & frame["source"].astype(str).str.strip().ne("")
    destination_text = frame["destination"].fillna("").astype(str).str.strip()
    destination_ok = destination_text.ne("") & destination_text.str.casefold().ne("s")
    disposition_ok = (
        frame["disposition"].fillna("").astype(str).str.strip().str.casefold().isin(allowed)
    )

    clean = frame.loc[
        frame["call_datetime"].notna()
        & frame["duration_seconds"].notna()
        & source_ok
        & destination_ok
        & disposition_ok
    ].copy()
    clean["duration_seconds"] = clean["duration_seconds"].astype(int)
    clean = clean.loc[
        clean["duration_seconds"].between(min_duration_seconds, max_duration_seconds)
    ]
    return clean.sort_values("call_datetime").reset_index(drop=True)
