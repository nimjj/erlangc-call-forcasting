from __future__ import annotations
import pandas as pd
from app.core.scheduling.shifts import row_shift_hours

def validate_agent_rest_period(
    schedule: pd.DataFrame,
    agent_id: str,
    target_date: str,
    minimum_rest_hours: int = 8,
) -> dict:

    if schedule.empty:
        raise ValueError("Schedule is empty.")

    frame = schedule.copy()

    frame["date"] = pd.to_datetime(
        frame["date"]
    )

    target_date = pd.Timestamp(
        target_date
    )

    agent_rows = frame.loc[
        frame["agent_id"].astype(str)
        == str(agent_id)
    ].sort_values("date")

    target_rows = agent_rows.loc[
        agent_rows["date"] == target_date
    ]

    if target_rows.empty:
        raise ValueError(
            f"No schedule found for {agent_id} on "
            f"{target_date.strftime('%Y-%m-%d')}."
        )

    target_row = target_rows.iloc[0]

    if str(target_row["status"]) != "WORK":
        return {
            "valid": True,
            "message": "Agent is not working on the target date.",
        }

    shift_lookup = {
        "NIGHT": {
            "start": 0,
            "end": 8,
        },
        "MORNING": {
            "start": 8,
            "end": 16,
        },
        "EVENING": {
            "start": 16,
            "end": 24,
        },
    }

    target_shift_code = str(
        target_row["shift_code"]
    )

    if target_shift_code not in shift_lookup:
        return {
            "valid": True,
            "message": "No rest validation required.",
        }

    target_start = (
        target_date
        + pd.Timedelta(
            hours=row_shift_hours(target_row)["start"]
        )
    )

    target_end = (
        target_date
        + pd.Timedelta(
            hours=row_shift_hours(target_row)["end"]
        )
    )

    previous_date = (
        target_date - pd.Timedelta(days=1)
    )

    next_date = (
        target_date + pd.Timedelta(days=1)
    )

    previous_rows = agent_rows.loc[
        agent_rows["date"] == previous_date
    ]

    if not previous_rows.empty:

        previous_row = previous_rows.iloc[0]

        previous_status = str(
            previous_row["status"]
        )

        previous_shift_code = str(
            previous_row["shift_code"]
        )

        if (
            previous_status == "WORK"
            and previous_shift_code in shift_lookup
        ):

            previous_end = (
                previous_date
                + pd.Timedelta(
                    hours=row_shift_hours(previous_row)["end"]
                )
            )

            rest_hours = (
                target_start - previous_end
            ).total_seconds() / 3600

            if rest_hours < minimum_rest_hours:

                return {
                    "valid": False,
                    "reason": "PREVIOUS_SHIFT_REST",
                    "rest_hours": rest_hours,
                    "message": (
                        f"{agent_id} would only have "
                        f"{rest_hours:.1f} hours rest "
                        f"before the new shift."
                    ),
                }

    next_rows = agent_rows.loc[
        agent_rows["date"] == next_date
    ]

    if not next_rows.empty:

        next_row = next_rows.iloc[0]

        next_status = str(
            next_row["status"]
        )

        next_shift_code = str(
            next_row["shift_code"]
        )

        if (
            next_status == "WORK"
            and next_shift_code in shift_lookup
        ):

            next_start = (
                next_date
                + pd.Timedelta(
                    hours=row_shift_hours(next_row)["start"]
                )
            )

            rest_hours = (
                next_start - target_end
            ).total_seconds() / 3600

            if rest_hours < minimum_rest_hours:

                return {
                    "valid": False,
                    "reason": "NEXT_SHIFT_REST",
                    "rest_hours": rest_hours,
                    "message": (
                        f"{agent_id} would only have "
                        f"{rest_hours:.1f} hours rest "
                        f"before the next shift."
                    ),
                }

    return {
        "valid": True,
        "message": (
            f"{agent_id} satisfies the minimum "
            f"{minimum_rest_hours}-hour rest rule."
        ),
    }
