"""Race-day pacing: split tables and a race workout to push (#151, #197).

Even pacing is the fastest strategy at every level in the largest split
analysis to date (PLOS One 2025, hundreds of thousands of marathon results);
recreational runners mostly positive-split, which is the thing to design
against. A negative split is offered for those who want it, and the
"10-10-10" structure for marathons: the first third a little slower than
goal, the middle at goal, the last at goal or a touch faster.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .plan import Workout
from .profile import GoalRace, Profile
from .units import METRES_PER_MILE, format_duration, format_pace, parse_distance, parse_duration

RACE_METRES = {
    "5k": 5000.0,
    "5 km": 5000.0,
    "10k": 10000.0,
    "10 km": 10000.0,
    "15k": 15000.0,
    "10 miles": 16093.44,
    "half": 21097.5,
    "half marathon": 21097.5,
    "marathon": 42195.0,
}
STRATEGIES = ("even", "negative", "10-10-10")
# Fraction of goal pace by which each strategy bends the segments.
BANDS = {
    "even": [(1.0, 0.0)],
    "negative": [(0.5, 0.015), (0.5, -0.015)],
    "10-10-10": [(1 / 3, 0.03), (1 / 3, 0.0), (1 / 3, -0.01)],
}


class RaceError(ValueError):
    pass


def race_metres(distance: str) -> float:
    key = distance.strip().lower()
    if key in RACE_METRES:
        return RACE_METRES[key]
    if "marathon" in key:
        return RACE_METRES["half"] if "half" in key else RACE_METRES["marathon"]
    try:
        return parse_distance(key)
    except Exception as exc:
        raise RaceError(f"unknown race distance {distance!r}") from exc


def goal_pace(goal_time: str, distance: str) -> float:
    """Seconds per kilometre for the goal."""
    seconds = parse_duration(goal_time)
    metres = race_metres(distance)
    if seconds <= 0 or metres <= 0:
        raise RaceError("goal time and distance must be positive")
    return seconds / (metres / 1000.0)


def splits(
    distance: str, goal_time: str, strategy: str = "even", imperial: bool = False
) -> list[dict[str, Any]]:
    """One row per kilometre (or mile) with the target pace and the clock at that point."""
    if strategy not in BANDS:
        raise RaceError(f"strategy must be one of {STRATEGIES}")
    metres = race_metres(distance)
    pace = goal_pace(goal_time, distance)
    unit = METRES_PER_MILE if imperial else 1000.0
    count = int(metres // unit) + (1 if metres % unit > 1 else 0)
    rows = []
    elapsed = 0.0
    covered = 0.0
    for index in range(1, count + 1):
        seg_end = min(index * unit, metres)
        seg_len = seg_end - covered
        fraction = (covered + seg_len / 2) / metres
        offset = _offset(strategy, fraction)
        seg_pace = pace * (1 + offset)
        elapsed += seg_pace * seg_len / 1000.0
        covered = seg_end
        rows.append(
            {
                "split": index,
                "at_m": round(seg_end),
                "pace": format_pace(seg_pace, imperial),
                "pace_spk": round(seg_pace, 1),
                "elapsed": format_duration(elapsed),
            }
        )
    return rows


def _offset(strategy: str, fraction: float) -> float:
    upto = 0.0
    for share, offset in BANDS[strategy]:
        upto += share
        if fraction <= upto + 1e-9:
            return offset
    return BANDS[strategy][-1][1]


def race_workout(
    goal: GoalRace | dict, profile: Profile, strategy: str = "even", band: float = 0.02
) -> Workout:
    """A race-day session: the distance split by strategy with pace targets."""
    if isinstance(goal, dict):
        goal = GoalRace(
            name=goal.get("name", "Race"),
            date=dt.date.fromisoformat(str(goal["date"])),
            distance=goal.get("distance"),
            goal_time=goal.get("goal_time"),
        )
    if not goal.distance or not goal.goal_time:
        raise RaceError("a race workout needs the goal distance and the goal time")
    if strategy not in BANDS:
        raise RaceError(f"strategy must be one of {STRATEGIES}")
    metres = race_metres(goal.distance)
    pace = goal_pace(goal.goal_time, goal.distance)
    unit = "mi" if profile.imperial else "km"
    steps = []
    for share, offset in BANDS[strategy]:
        seg_pace = pace * (1 + offset)
        steps.append(
            {
                "kind": "run",
                "distance": f"{round(metres * share)}m",
                "target": {
                    "type": "pace",
                    "slow": format_pace(seg_pace * (1 + band), profile.imperial).split("/")[0]
                    + f"/{unit}",
                    "fast": format_pace(seg_pace * (1 - band), profile.imperial).split("/")[0]
                    + f"/{unit}",
                },
            }
        )
    words = {
        "even": "even pace",
        "negative": "negative split",
        "10-10-10": "10-10-10: settle, hold, press",
    }
    return Workout.from_dict(
        {
            "name": f"Race: {goal.name}",
            "date": goal.date.isoformat(),
            "role": "race",
            "phase": "taper",
            "notes": f"Goal {goal.goal_time} ({format_pace(pace, profile.imperial)}), {words[strategy]}. Start controlled; the second half decides it.",
            "steps": steps,
        }
    )


def table(rows: list[dict[str, Any]], imperial: bool = False) -> str:
    unit = "mi" if imperial else "km"
    lines = [f"{'split':>5}  {'pace':>9}  {'elapsed':>9}"]
    for r in rows:
        lines.append(f"{r['split']:>4}{unit}  {r['pace']:>9}  {r['elapsed']:>9}")
    return "\n".join(lines)
