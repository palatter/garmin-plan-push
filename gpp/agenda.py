"""What is next, and what the week looks like (#156, #171).

The two questions a runner asks a plan most often: what am I doing next,
and what does this week (and the one after) hold. Stryd's assistant gives
two or three days' notice before a phase changes; the week view here says
the same thing from the plan's own phases.
"""

from __future__ import annotations

import datetime as dt

from .load import roles_for, session_load, week_start, weekly_stats
from .plan import Plan, Workout
from .profile import Profile
from .render import render_workout
from .timeline import workout_summary
from .units import format_duration


def next_session(plan: Plan, today: dt.date | None = None) -> Workout | None:
    today = today or dt.date.today()
    for workout in plan.sorted_workouts():
        if workout.date >= today:
            return workout
    return None


def render_next(plan: Plan, profile: Profile, today: dt.date | None = None) -> str:
    from .compile import compile_workout

    workout = next_session(plan, today)
    if workout is None:
        return "Nothing left on the plan."
    today = today or dt.date.today()
    gap = (workout.date - today).days
    when = (
        "today"
        if gap == 0
        else "tomorrow"
        if gap == 1
        else f"in {gap} days ({workout.date.strftime('%A')})"
    )
    head = f"Next: {workout.name}, {when}"
    return head + "\n" + render_workout(compile_workout(workout, profile, plan.plan), profile)


def week_view(plan: Plan, profile: Profile, today: dt.date | None = None) -> dict:
    """This week's sessions and totals, and a preview of next week."""
    today = today or dt.date.today()
    monday = week_start(today)
    workouts = plan.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    stats = {w.start: w for w in weekly_stats(plan, profile)}

    def describe(monday: dt.date) -> dict:
        sessions = [w for w in workouts if monday <= w.date < monday + dt.timedelta(days=7)]
        week = stats.get(monday)
        key = max(sessions, key=lambda w: session_load(w, profile), default=None)
        phases = sorted({w.phase for w in sessions if w.phase})
        return {
            "start": monday.isoformat(),
            "sessions": [
                {
                    "date": w.date.isoformat(),
                    "day": w.date.strftime("%a"),
                    "name": w.name,
                    "role": roles[id(w)],
                    "minutes": round(summaries[id(w)]["seconds"] / 60),
                    "km": round(summaries[id(w)]["metres"] / 1000, 1),
                    "done": w.date < today,
                }
                for w in sessions
            ],
            "km": week.km if week else 0.0,
            "minutes": round(week.seconds / 60) if week else 0,
            "hard_share": round(week.hard_share, 3) if week else 0.0,
            "phases": phases,
            "key_session": key.name if key else None,
        }

    this_week = describe(monday)
    next_week = describe(monday + dt.timedelta(days=7))
    change = None
    if next_week["phases"] and next_week["phases"] != this_week["phases"]:
        change = (
            f"Phase changes on {next_week['start']}: {', '.join(this_week['phases']) or 'unlabelled'} -> "
            f"{', '.join(next_week['phases'])}"
        )
    race = plan.a_race
    days_to_race = (race.date - today).days if race else None
    return {
        "today": today.isoformat(),
        "this_week": this_week,
        "next_week": next_week,
        "phase_change": change,
        "days_to_race": days_to_race,
        "race": race.name if race else None,
    }


def render_week(view: dict) -> str:
    lines = []
    for label, week in (("This week", view["this_week"]), ("Next week", view["next_week"])):
        lines.append(
            f"{label} (from {week['start']}): {len(week['sessions'])} sessions, "
            f"{week['km']:.0f} km, {format_duration(week['minutes'] * 60)}, {week['hard_share']:.0%} hard"
            + (f", key session: {week['key_session']}" if week["key_session"] else "")
        )
        for s in week["sessions"]:
            mark = "x" if s["done"] else " "
            lines.append(
                f"  [{mark}] {s['day']} {s['date']}  {s['name']:<28} {s['role']:<12} {s['minutes']:>4} min"
            )
        lines.append("")
    if view["phase_change"]:
        lines.append(view["phase_change"])
    if view["days_to_race"] is not None and view["days_to_race"] >= 0:
        lines.append(f"{view['days_to_race']} days to {view['race']}")
    return "\n".join(lines).rstrip()
