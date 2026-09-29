from __future__ import annotations
from collections import defaultdict
import pandas as pd
from app.core.scheduling.shifts import build_shift_requirements, calculate_schedule_headcount, shift_definitions

def build_monthly_agent_schedule(
    forecast: pd.DataFrame,
    year: int,
    month: int,
    agent_count: int | None = None,
    shift_start_times: list[str] | None = None,
) -> tuple[pd.DataFrame, dict]:

    requirements = build_shift_requirements(
        forecast=forecast,
        year=year,
        month=month,
        shift_start_times=shift_start_times,
    )

    minimum_agents = calculate_schedule_headcount(
        requirements,
        working_days_per_week=5,
    )

    if agent_count is None:
        agent_count = minimum_agents

    agent_count = int(agent_count)

    if agent_count <= 0:
        raise ValueError("agent_count must be greater than 0.")

    if agent_count < minimum_agents:
        raise ValueError(
            f"At least {minimum_agents} agents are estimated to be required "
            f"for {year}-{month:02d}; received {agent_count}."
        )

    agents = [
        f"Agent {number:03d}"
        for number in range(1, agent_count + 1)
    ]

    total_assignments = defaultdict(int)
    shift_assignments = defaultdict(lambda: defaultdict(int))
    weekly_workdays = defaultdict(lambda: defaultdict(int))
    worked_dates = defaultdict(set)
    last_work_end = {}

    schedule_rows = []
    coverage_rows = []

    requirements = requirements.sort_values(
        ["date", "start_hour"]
    ).reset_index(drop=True)

    for requirement in requirements.itertuples(index=False):
        date_value = pd.Timestamp(requirement.date)

        week_start = (
            date_value
            - pd.Timedelta(days=date_value.weekday())
        ).date()

        required = int(requirement.required_agents)

        shift_start = date_value + pd.Timedelta(hours=requirement.start_hour)

        candidates = [
            agent
            for agent in agents
            if date_value.date() not in worked_dates[agent]
            and weekly_workdays[agent][week_start] < 5
            and (
                agent not in last_work_end
                or shift_start - last_work_end[agent]
                >= pd.Timedelta(hours=8)
            )
        ]

        candidates.sort(
            key=lambda agent: (
                weekly_workdays[agent][week_start],
                total_assignments[agent],
                shift_assignments[agent][requirement.shift_code],
                agent,
            )
        )

        selected_agents = candidates[:required]

        for agent in selected_agents:
            schedule_rows.append(
                {
                    "agent_id": agent,
                    "date": date_value.strftime("%Y-%m-%d"),
                    "weekday": date_value.day_name(),
                    "shift_code": requirement.shift_code,
                    "shift": requirement.shift_label,
                    "status": "WORK",
                }
            )

            total_assignments[agent] += 1
            shift_assignments[agent][requirement.shift_code] += 1
            weekly_workdays[agent][week_start] += 1
            worked_dates[agent].add(date_value.date())
            last_work_end[agent] = date_value + pd.Timedelta(
                hours=requirement.end_hour
            )

        assigned = len(selected_agents)

        coverage_rows.append(
            {
                "date": date_value.strftime("%Y-%m-%d"),
                "weekday": date_value.day_name(),
                "shift_code": requirement.shift_code,
                "shift": requirement.shift_label,
                "required_agents": required,
                "assigned_agents": assigned,
                "shortage": max(required - assigned, 0),
                "status": "OK" if assigned >= required else "SHORT",
            }
        )

    all_dates = pd.date_range(
        start=f"{year}-{month:02d}-01",
        end=(
            pd.Timestamp(f"{year}-{month:02d}-01")
            + pd.offsets.MonthEnd(0)
        ),
        freq="D",
    )

    existing_assignments = {
        (row["agent_id"], row["date"])
        for row in schedule_rows
    }

    for agent in agents:
        for date_value in all_dates:
            date_text = date_value.strftime("%Y-%m-%d")

            if (agent, date_text) not in existing_assignments:
                schedule_rows.append(
                    {
                        "agent_id": agent,
                        "date": date_text,
                        "weekday": date_value.day_name(),
                        "shift_code": "OFF",
                        "shift": "OFF",
                        "status": "OFF",
                    }
                )

    schedule = pd.DataFrame(schedule_rows).sort_values(
        ["agent_id", "date"]
    ).reset_index(drop=True)

    coverage = pd.DataFrame(coverage_rows)

    summary = {
        "year": int(year),
        "month": int(month),
        "minimum_agents": int(minimum_agents),
        "agent_count": int(agent_count),
        "shift_hours": 8,
        "shifts": shift_definitions(shift_start_times),
        "working_days_per_week": 5,
        "days_off_per_week": 2,
        "total_required_shift_assignments": int(
            requirements["required_agents"].sum()
        ),
        "total_assigned_shift_assignments": int(
            (schedule["status"] == "WORK").sum()
        ),
        "coverage_shortage": int(
            coverage["shortage"].sum()
        ),
        "coverage_ok": bool(
            coverage["shortage"].sum() == 0
        ),
        "coverage": coverage.to_dict(orient="records"),
    }
    return schedule, summary
