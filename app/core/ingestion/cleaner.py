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
    if pd.isna(value):
        return None
    text = str(value).strip().casefold()
    if not text:
        return None
    if text.count(":") == 2 and not any(unit in text for unit in ("d", "h", "m", "s")):
        return hms_to_seconds(text)
    import re
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)", text)
    if not matches:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return int(sum(float(amount) * units[unit] for amount, unit in matches))

def clean_percent(value) -> float:
    number = float(str(value).strip().replace("%", ""))
    if number > 1:
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

    frame["call_datetime"] = pd.to_datetime(frame["call_datetime"], errors="coerce")

    parts = frame["duration"].astype(str).str.strip().str.split(":", expand=True)
    if parts.shape[1] == 3:
        h = pd.to_numeric(parts[0], errors="coerce")
        m = pd.to_numeric(parts[1], errors="coerce")
        s = pd.to_numeric(parts[2], errors="coerce")
        valid_hms = (h >= 0) & (m >= 0) & (m < 60) & (s >= 0) & (s < 60)
        dur = pd.Series(float("nan"), index=frame.index, dtype="float64")
        dur.loc[valid_hms] = (h.loc[valid_hms] * 3600 + m.loc[valid_hms] * 60 + s.loc[valid_hms]).astype(float)
        frame["duration_seconds"] = dur
    else:
        frame["duration_seconds"] = frame["duration"].map(talk_time_to_seconds)

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
