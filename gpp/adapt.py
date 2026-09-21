"""Adapting a plan after life happens: layoffs, pauses, missed sessions.

Rules, not a model. Every tool researched that adapts plans is criticised
for adapting them badly and opaquely; a small set of stated rules that
explain themselves is the deliberate alternative. Each function returns the
new plan AND a list of plain-English reasons, so the UI can show "here is
what changed and why" and let the athlete keep the original instead.

Layoff tiers are Stryd's published ones (help.stryd.com):
  0-7 days   skip what was missed and resume
  8-14       restart the current phase
  15-28      go back to aerobic/base work
  29+        start again from foundation
"""

from __future__ import annotations

import datetime as dt
import re
from copy import deepcopy
from dataclasses import dataclass, field

from .load import roles_for
from .plan import Plan, Step, Workout
from .profile import Profile
from .timeline import workout_summary
from .units import (
    UnitError,
    format_distance,
    format_duration,
    parse_distance,
    parse_duration,
)

LAYOFF_TIERS = (
    (7, "resume", "Skip what was missed and carry on; a week off costs little."),
    (
        14,
        "restart-phase",
        "Restart the current phase: repeat the last week or two before pushing on.",
    ),
    (28, "back-to-base", "Go back to aerobic base work for a couple of weeks before any quality."),
    (
        10**6,
        "foundation",
        "Start again from foundation: easy running only, rebuild volume gradually.",
    ),
)

# Volume multiplier for the first week back, by tier.
RETURN_SCALE = {"resume": 1.0, "restart-phase": 0.8, "back-to-base": 0.65, "foundation": 0.5}


@dataclass
class Adaptation:
    plan: Plan
    reasons: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.to_dict(),
            "reasons": self.reasons,
            "dropped": self.dropped,
            "moved": self.moved,
        }


def layoff_tier(days_off: int) -> tuple[str, str]:
    """(tier, advice) for a break of this many days."""
    for limit, tier, advice in LAYOFF_TIERS:
        if days_off <= limit:
            return tier, advice
    return LAYOFF_TIERS[-1][1], LAYOFF_TIERS[-1][2]


# --- scaling a workout ------------------------------------------------------

_NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def _scale_extent(value: str | float | None, factor: float) -> str | float | None:
    """Scale "45m" -> "31m", "10km" -> "7km"; leave clock formats alone."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value) * factor)
    m = _NUM.match(value)
    if not m:
        # "1:05:00" / "1h05m" style: scale through seconds and re-format.
        if ":" in value or "h" in value.lower():
            try:
                return format_duration(parse_duration(value) * factor)
            except UnitError:
                return value
        return value
    number, unit = float(m.group(1)), m.group(2)
    scaled = number * factor
    text = (
        f"{scaled:.1f}".rstrip("0").rstrip(".")
        if unit.lower() in ("km", "k", "mi")
        else f"{int(scaled + 0.5)}"
    )
    return f"{text}{unit}"


def scale_workout(workout: Workout, factor: float) -> Workout:
    """Shorten the work in a session; recoveries and reps stay as they are."""
    out = deepcopy(workout)

    def walk(steps: list[Step]) -> None:
        for s in steps:
            if s.is_repeat:
                walk(s.steps)
            elif s.kind in ("run", "warmup", "cooldown", "stride") and s.until is None:
                s.duration = _scale_extent(s.duration, factor)
                s.distance = _scale_extent(s.distance, factor)

    walk(out.steps)
    return out


# --- pauses -----------------------------------------------------------------


def pause_plan(plan: Plan, start: dt.date, days: int, reason: str = "break") -> Adaptation:
    """Illness / holiday from `start` for `days`: drop what falls inside,
    shift the rest, protect the A race, and apply the layoff tier on return.
    """
    if days <= 0:
        return Adaptation(plan=deepcopy(plan), reasons=["Nothing to pause."])
    end = start + dt.timedelta(days=days)
    race = plan.a_race
    tier, advice = layoff_tier(days)
    scale = RETURN_SCALE[tier]

    kept: list[Workout] = []
    dropped: list[str] = []
    moved: list[str] = []
    for w in plan.sorted_workouts():
        if race and w.date == race.date:
            kept.append(deepcopy(w))  # the race does not move
            continue
        if w.date >= start:
            new_date = w.date + dt.timedelta(days=days)
            if race and new_date >= race.date:
                dropped.append(f"{w.date.isoformat()} {w.name} (no room before the race)")
                continue
            copy = deepcopy(w)
            copy.date = new_date
            if new_date < end + dt.timedelta(days=7) and scale < 1.0:
                copy = scale_workout(copy, scale)
                copy.phase = "recovery" if tier in ("back-to-base", "foundation") else copy.phase
            moved.append(f"{w.date.isoformat()} -> {new_date.isoformat()} {w.name}")
            kept.append(copy)
        else:
            kept.append(deepcopy(w))

    reasons = [
        f"Paused {days} day(s) from {start.isoformat()} for {reason}.",
        f"Return tier: {tier} -- {advice}",
    ]
    if scale < 1.0:
        reasons.append(f"First week back scaled to {scale:.0%} of planned work.")
    if race:
        reasons.append(f"A race {race.name} on {race.date.isoformat()} kept in place.")
    weeks = []
    for wk in plan.weeks:
        shifted = deepcopy(wk)
        if shifted.start >= start:
            shifted.start += dt.timedelta(days=days)
        weeks.append(shifted)
    new = Plan(
        plan=plan.plan,
        workouts=kept,
        race_date=plan.race_date,
        races=list(plan.races),
        weeks=weeks,
    )
    return Adaptation(plan=new, reasons=reasons, dropped=dropped, moved=moved)


# --- missed sessions --------------------------------------------------------


def replan_missed(
    plan: Plan, missed: list[dt.date], profile: Profile, today: dt.date | None = None
) -> Adaptation:
    """Rebuild the days after one or more missed sessions.

    Quality session missed with another quality session within two days:
    drop it (stacking them would break the hard-day rule). Otherwise move it
    to the next free, allowed day within three days, replacing an easy run
    if one is there. A missed long run goes to the next free weekend day
    within a week. Anything that cannot be placed is dropped and said so.
    """
    today = today or (max(missed) if missed else dt.date.today())
    workouts = plan.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    by_date: dict[dt.date, list[Workout]] = {}
    for w in workouts:
        by_date.setdefault(w.date, []).append(w)

    kept: dict[int, Workout] = {id(w): deepcopy(w) for w in workouts}
    reasons: list[str] = []
    dropped: list[str] = []
    moved: list[str] = []
    race = plan.a_race

    def kept_on(day: dt.date) -> list[Workout]:
        return [w for w in by_date.get(day, []) if id(w) in kept]

    def hard_on(day: dt.date, moving: Workout) -> bool:
        return any(roles[id(o)] in ("quality", "race") for o in kept_on(day) if o is not moving)

    def free_day(candidates: list[dt.date], role_needed: str, moving: Workout) -> dt.date | None:
        for day in candidates:
            if profile.availability and not profile.availability.allows(day):
                continue
            if race and day >= race.date:
                continue
            # The hard-day rules hold for the moved session too: no quality
            # next to quality, no long run the day after a quality session.
            before, after = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
            if role_needed == "quality" and (hard_on(before, moving) or hard_on(after, moving)):
                continue
            if role_needed == "long" and hard_on(before, moving):
                continue
            existing = kept_on(day)
            if not existing:
                return day
            if (
                all(roles[id(w)] in ("easy", "recovery") for w in existing)
                and role_needed != "easy"
            ):
                return day
        return None

    for day in sorted(missed):
        for w in by_date.get(day, []):
            if id(w) not in kept:
                continue
            role = roles[id(w)]
            label = f"{w.date.isoformat()} {w.name}"
            if role in ("easy", "recovery", "rest", "cross"):
                del kept[id(w)]
                dropped.append(label)
                reasons.append(f"{label}: easy session, simply skipped.")
                continue
            if role in ("quality", "race"):
                soon = [
                    o
                    for o in workouts
                    if id(o) in kept
                    and o is not w
                    and roles[id(o)] in ("quality", "race")
                    and 0 < (o.date - day).days <= 2
                ]
                if soon:
                    del kept[id(w)]
                    dropped.append(label)
                    reasons.append(
                        f"{label}: dropped -- the next quality session is only {(soon[0].date - day).days} day(s) away."
                    )
                    continue
                target = free_day([day + dt.timedelta(days=i) for i in range(1, 4)], "quality", w)
            elif role in ("long", "medium-long"):
                target = free_day(
                    [
                        day + dt.timedelta(days=i)
                        for i in range(1, 8)
                        if (day + dt.timedelta(days=i)).weekday() >= 5
                    ]
                    + [day + dt.timedelta(days=i) for i in range(1, 8)],
                    "long",
                    w,
                )
            else:
                target = free_day([day + dt.timedelta(days=i) for i in range(1, 4)], role, w)

            if target is None:
                del kept[id(w)]
                dropped.append(label)
                reasons.append(
                    f"{label}: no free day to move it to, so it is dropped rather than stacked."
                )
                continue
            for other in by_date.get(target, []):
                if id(other) in kept and roles[id(other)] in ("easy", "recovery"):
                    del kept[id(other)]
                    dropped.append(f"{other.date.isoformat()} {other.name}")
                    reasons.append(
                        f"{other.date.isoformat()} {other.name}: replaced by the moved session."
                    )
            kept[id(w)].date = target
            by_date.setdefault(target, []).append(w)
            moved.append(f"{label} -> {target.isoformat()}")
            reasons.append(f"{label}: moved to {target.isoformat()}.")

    new = Plan(
        plan=plan.plan,
        workouts=sorted(kept.values(), key=lambda w: (w.date, w.name)),
        race_date=plan.race_date,
        races=list(plan.races),
        weeks=list(plan.weeks),
    )
    if not reasons:
        reasons.append("Nothing on those dates to replan.")
    return Adaptation(plan=new, reasons=reasons, dropped=dropped, moved=moved)


def describe_workout_brief(workout: Workout, profile: Profile) -> str:
    s = workout_summary(workout, profile)
    return f"{workout.name}: {format_duration(s['seconds'])}, {format_distance(s['metres'], profile.imperial)}"


def scale_summary(workout: Workout, factor: float) -> str:
    """Human line for a scaled session, for the UI."""
    parts = []
    for step in workout.steps:
        if step.duration and not step.is_repeat:
            parts.append(format_duration(parse_duration(step.duration) * factor))
        elif step.distance and not step.is_repeat:
            parts.append(format_distance(parse_distance(step.distance) * factor))
    return ", ".join(parts)
