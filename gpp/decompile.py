"""Garmin workout JSON -> the plan DSL (#150, #183).

The compiler's inverse. It lets a workout built by hand in Garmin Connect,
or one Garmin Coach scheduled, be pulled into a plan, edited, saved to the
library and pushed back -- and it lets a whole calendar month become a plan.
Pace bounds are mapped back to a zone name when they match one of the
athlete's zones; otherwise they stay explicit paces.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from .client import parse_tag
from .plan import Plan, Workout
from .profile import Profile
from .units import format_pace, mps_to_pace

STEP_KINDS = {
    "warmup": "warmup",
    "cooldown": "cooldown",
    "interval": "run",
    "recovery": "recover",
    "rest": "rest",
}
SPORTS = {
    "running": "running",
    "cycling": "cycling",
    "swimming": "swimming",
    "strength_training": "strength",
}


def _extent(step: dict) -> dict:
    key = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if key == "time" and value:
        seconds = float(value)
        return {"duration": f"{seconds / 60:g}m" if seconds % 60 == 0 else f"{seconds:g}s"}
    if key == "distance" and value:
        metres = float(value)
        return {
            "distance": f"{metres / 1000:g}km"
            if metres % 100 == 0 and metres >= 1000
            else f"{metres:g}m"
        }
    if key == "reps" and value:
        return {"count": int(value)}
    return {"until": "lap"}


def _target(step: dict, profile: Profile | None) -> dict:
    key = (step.get("targetType") or {}).get("workoutTargetTypeKey")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    if key == "pace.zone" and one and two:
        slow, fast = mps_to_pace(float(one)), mps_to_pace(float(two))
        if slow < fast:
            slow, fast = fast, slow
        if profile is not None:
            for name, (zs, zf) in profile.zone_table().items():
                if abs(zs - slow) < 1.5 and abs(zf - fast) < 1.5:
                    return {"type": "pace", "zone": name}
        imperial = bool(profile and profile.imperial)
        return {
            "type": "pace",
            "slow": format_pace(slow, imperial),
            "fast": format_pace(fast, imperial),
        }
    if key == "heart.rate.zone":
        if step.get("zoneNumber"):
            return {"type": "hr", "zone": int(step["zoneNumber"])}
        if one and two:
            # The compiler writes zones as bpm bounds; map them back when they
            # are exactly one of the athlete's zones.
            if profile is not None and profile.lthr:
                for zone in sorted(profile.hr_zones):
                    if profile.hr_zone(zone) == (int(one), int(two)):
                        return {"type": "hr", "zone": zone}
            return {"type": "hr", "low": int(one), "high": int(two)}
    if key == "cadence.zone" and one and two:
        return {"type": "cadence", "low": int(one), "high": int(two)}
    if key == "power.zone":
        if step.get("zoneNumber"):
            return {"type": "power", "zone": int(step["zoneNumber"])}
        if one and two:
            return {"type": "power", "low": int(one), "high": int(two)}
    return {"type": "none"}


def _step(step: dict, profile: Profile | None) -> dict:
    if step.get("type") == "RepeatGroupDTO":
        return {
            "kind": "repeat",
            "reps": int(step.get("numberOfIterations") or 1),
            "steps": [_step(child, profile) for child in step.get("workoutSteps") or []],
            **({"note": step["description"]} if step.get("description") else {}),
        }
    key = (step.get("stepType") or {}).get("stepTypeKey", "interval")
    out: dict[str, Any] = {"kind": STEP_KINDS.get(key, "run")}
    if step.get("exerciseName"):
        out = {"kind": "exercise", "exercise": step["exerciseName"].replace("_", " ").lower()}
        if step.get("category"):
            out["category"] = str(step["category"]).replace("_", " ").lower()
        if step.get("weightValue"):
            out["weight"] = float(step["weightValue"])
    out.update(_extent(step))
    if out["kind"] == "exercise" and "until" in out:
        out.pop("until")
        out["count"] = int(step.get("endConditionValue") or 1)
    if out["kind"] != "exercise":
        target = _target(step, profile)
        if target["type"] != "none" or out["kind"] == "run":
            out["target"] = target
    note = step.get("description")
    if note and not parse_tag(note):
        out["note"] = str(note)[:212]
    return out


def workout_from_garmin(payload: dict, date: dt.date, profile: Profile | None = None) -> Workout:
    """One Garmin workout payload (as returned by GET /workout-service/workout/{id})."""
    segments = payload.get("workoutSegments") or [{}]
    steps = [_step(s, profile) for s in segments[0].get("workoutSteps") or []]
    sport = SPORTS.get((payload.get("sportType") or {}).get("sportTypeKey", "running"), "running")
    description = payload.get("description") or ""
    notes = (
        "\n".join(line for line in description.splitlines() if not parse_tag(line)).strip() or None
    )
    data: dict[str, Any] = {
        "name": payload.get("workoutName") or "Workout",
        "date": date.isoformat(),
        "sport": sport,
        "steps": steps or [{"kind": "run", "until": "lap", "target": {"type": "none"}}],
    }
    if notes:
        data["notes"] = notes[:500]
        # The compiler puts the workout's "why" line on the first step as its
        # cue; reading it back as a step note would double it.
        first = _first_executable(steps)
        if first is not None and first.get("note") == notes:
            first.pop("note")
    return Workout.from_dict(data)


def _first_executable(steps: list[dict]) -> dict | None:
    for step in steps:
        if step.get("kind") == "repeat":
            found = _first_executable(step.get("steps") or [])
            if found is not None:
                return found
        else:
            return step
    return None


def plan_from_calendar(
    items: list[dict],
    fetch: Callable[[int], dict],
    profile: Profile | None = None,
    name: str = "Garmin calendar",
) -> tuple[Plan, list[str]]:
    """Every scheduled workout in `items` (calendar-service rows) as one plan.

    Returns the plan and the rows that were skipped, with the reason.
    """
    workouts: list[Workout] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        wid = item.get("workoutId") or item.get("id")
        raw_date = (item.get("date") or "")[:10]
        if not wid or not raw_date or item.get("itemType") not in (None, "workout"):
            skipped.append(f"{raw_date or '?'} {item.get('title') or '?'}: not a workout")
            continue
        try:
            payload = fetch(int(wid))
            workout = workout_from_garmin(payload, dt.date.fromisoformat(raw_date), profile)
        except Exception as exc:
            skipped.append(f"{raw_date} {item.get('title') or wid}: {exc}")
            continue
        key = (raw_date, workout.name.lower())
        if key in seen:
            workout.name = f"{workout.name} ({wid})"
        seen.add(key)
        workouts.append(workout)
    if not workouts:
        raise ValueError(
            "no workouts in that range" + (f" ({len(skipped)} rows skipped)" if skipped else "")
        )
    return Plan(plan=name, workouts=sorted(workouts, key=lambda w: (w.date, w.name))), skipped
