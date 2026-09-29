from __future__ import annotations

from pathlib import Path
import pandas as pd
from app.core.constants import CDR_COLUMNS

def _normalise_column(value: object) -> str:
    return " ".join(str(value).strip().casefold().split())

def _read_delimited(file_path: str | Path, encoding: str, sep: str, header: int | None) -> pd.DataFrame:
    return pd.read_csv(
        file_path,
        header=header,
        names=CDR_COLUMNS if header is None else None,
        engine="python",
        dtype=str,
        encoding=encoding,
        sep=sep,
    )

def _adapt_export_columns(frame: pd.DataFrame) -> pd.DataFrame | None:
    aliases = {
        "source": {"source", "caller id", "callerid"},
        "destination": {"destination", "queue"},
        "call_datetime": {"call datetime", "date", "datetime"},
        "duration": {"duration", "talk time", "talktime", "handle time"},
        "unique_id": {"unique id", "unique_id", "call id", "callid"},
    }
    columns = {_normalise_column(column): column for column in frame.columns}
    selected: dict[str, str] = {}
    for canonical, names in aliases.items():
        match = next((columns[name] for name in names if name in columns), None)
        if match is None:
            return None
        selected[canonical] = match

    adapted = pd.DataFrame({canonical: frame[column] for canonical, column in selected.items()})
    adapted["caller_id"] = adapted["source"]
    adapted["disposition"] = "Answered"
    return adapted[CDR_COLUMNS]

def read_cdr_csv(file_path: str | Path) -> pd.DataFrame:
    attempts = [
        {"encoding": "utf-16", "sep": "\t"},
        {"encoding": "utf-8-sig", "sep": ","},
        {"encoding": "latin1", "sep": ","},
    ]
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            headed = _read_delimited(file_path, header=0, **kwargs)
            adapted = _adapt_export_columns(headed)
            if adapted is not None:
                return adapted

            frame = _read_delimited(file_path, header=None, **kwargs)
            if frame[CDR_COLUMNS[1:]].isna().all().all():
                raise ValueError("The selected separator did not produce seven columns.")
            return frame
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Could not read CDR CSV. Last error: {last_error}")
