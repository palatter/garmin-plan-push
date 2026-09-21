"""Session load, weekly aggregation, monotony, and the dashboard numbers.

Load here is a *number*, deliberately simple and deliberately not an
acute:chronic ratio (see ROADMAP, "Deliberately not doing"). It is a
session-RPE-style product of minutes and intensity, close in spirit to
Foster's method: a recovery jog scores low, an hour at threshold scores high,
and the weekly sum is something a runner can watch trend without pretending
it predicts injury.

Monotony (Foster 1998) is the mean of daily load over a week divided by its
standard deviation. Doing the same load every day scores high, and that
pattern -- not high load itself -- is what precedes overtraining in Foster's
data. It needs no history: it can be computed on the plan.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

from .plan import Plan, Workout
from .profile import Profile
from .timeline import workout_summary, workout_timeline

# Foster's monotony threshold; above this a week is "monotonous".
MONOTONY_LIMIT = 2.0
# Cap when a week has load but zero variance (std = 0).
MONOTONY_CAP = 10.0


def session_load(workout: Workout, profile: Profile) -> float:
    """Minutes x an RPE-like weight (2 at recovery, 10 all-out), scaled /10.

    An easy hour lands near 25, a 40-minute threshold session near 30, a
    two-hour long run near 55 -- the same order a coach would put them in.
    """
    total = 0.0
    for block in workout_timeline(workout, profile):
        if block["open_ended"]:
            continue
        rpe = 2.0 + 8.0 * float(block["intensity"])
        total += (block["seconds"] / 60.0) * rpe / 10.0
    return round(total, 1)


@dataclass
class WeekStats:
    start: dt.date
    sessions: int = 0
    seconds: float = 0.0
    hard_seconds: float = 0.0
    metres: float = 0.0
    load: float = 0.0
    longest_run_metres: float = 0.0
    quality_sessions: int = 0
    strides: bool = False
    medium_long: bool = False
    phases: set[str] = field(default_factory=set)
    daily_load: dict[dt.date, float] = field(default_factory=dict)

    @property
    def hard_share(self) -> float:
        return self.hard_seconds / self.seconds if self.seconds else 0.0

    @property
    def km(self) -> float:
        return self.metres / 1000.0

    def monotony(self) -> float | None:
        """Foster's monotony over the seven days, None if too little to judge."""
        loads = [self.daily_load.get(self.start + dt.timedelta(days=i), 0.0) for i in range(7)]
        if sum(1 for x in loads if x > 0) < 3:
            return None
        mean = statistics.fmean(loads)
        std = statistics.pstdev(loads)
        if std == 0:
            return MONOTONY_CAP if mean > 0 else None
        return round(mean / std, 2)

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "sessions": self.sessions,
            "minutes": round(self.seconds / 60),
            "hard_minutes": round(self.hard_seconds / 60),
            "hard_share": round(self.hard_share, 3),
            "km": round(self.km, 1),
            "load": round(self.load, 1),
            "longest_run_km": round(self.longest_run_metres / 1000, 1),
            "quality_sessions": self.quality_sessions,
            "monotony": self.monotony(),
            "phases": sorted(self.phases),
        }


def week_start(day: dt.date) -> dt.date:
    """Monday of the ISO week containing `day`."""
    return day - dt.timedelta(days=day.weekday())


def has_strides(workout: Workout) -> bool:
    def walk(steps) -> bool:
        return any(s.kind == "stride" or (s.is_repeat and walk(s.steps)) for s in steps)

    return walk(workout.steps)


def infer_role(workout: Workout, summary: dict, median_metres: float) -> str:
    """The role a workout plays, from its shape when the plan did not say."""
    if workout.role:
        return workout.role
    if workout.sport == "strength":
        return "strength"
    if summary["hard_seconds"] >= 600:
        return "quality"
    if median_metres and summary["metres"] >= 1.4 * median_metres:
        return "long"
    if summary["seconds"] >= 80 * 60:
        return "medium-long"
    return "easy"


def weekly_stats(plan: Plan, profile: Profile) -> list[WeekStats]:
    """Aggregate a plan into ISO weeks, in order, with empty weeks filled in."""
    workouts = plan.sorted_workouts()
    if not workouts:
        return []
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    running = [summaries[id(w)]["metres"] for w in workouts if w.sport == "running"]
    median_metres = statistics.median(running) if running else 0.0

    first = week_start(workouts[0].date)
    last = week_start(workouts[-1].date)
    weeks: dict[dt.date, WeekStats] = {}
    cursor = first
    while cursor <= last:
        weeks[cursor] = WeekStats(start=cursor)
        cursor += dt.timedelta(days=7)

    for workout in workouts:
        summary = summaries[id(workout)]
        week = weeks[week_start(workout.date)]
        role = infer_role(workout, summary, median_metres)
        load = session_load(workout, profile)
        week.sessions += 1
        week.seconds += summary["seconds"]
        week.hard_seconds += summary["hard_seconds"]
        week.load += load
        week.daily_load[workout.date] = week.daily_load.get(workout.date, 0.0) + load
        if workout.sport == "running":
            week.metres += summary["metres"]
            week.longest_run_metres = max(week.longest_run_metres, summary["metres"])
        if role in ("quality", "race"):
            week.quality_sessions += 1
        if role == "medium-long":
            week.medium_long = True
        if has_strides(workout):
            week.strides = True
        if workout.phase:
            week.phases.add(workout.phase)
    return list(weeks.values())


def plan_dashboard(plan: Plan, profile: Profile) -> dict:
    """The numbers the review screen shows above the workouts."""
    weeks = weekly_stats(plan, profile)
    total_seconds = sum(w.seconds for w in weeks)
    hard = sum(w.hard_seconds for w in weeks)
    return {
        "weeks": [w.to_dict() for w in weeks],
        "total_km": round(sum(w.km for w in weeks), 1),
        "total_minutes": round(total_seconds / 60),
        "hard_share": round(hard / total_seconds, 3) if total_seconds else 0.0,
        "sessions": sum(w.sessions for w in weeks),
        "peak_week_km": round(max((w.km for w in weeks), default=0.0), 1),
        "longest_run_km": round(max((w.longest_run_metres for w in weeks), default=0.0) / 1000, 1),
    }
