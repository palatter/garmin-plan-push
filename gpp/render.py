"""Human-readable rendering of a plan.

This is the safety net. Nothing reaches your Garmin account until you have
seen this and agreed with it, because a structured workout that is subtly
wrong is worse than no workout at all -- the watch will beep at you for an
hour trying to enforce it.
"""

from __future__ import annotations

import datetime as dt

from .compile import CompiledWorkout, _target_mid_pace  # noqa: F401
from .plan import Plan, Step, Target
from .profile import Profile
from .units import (
    format_distance,
    format_duration,
    format_pace,
    parse_distance,
    parse_duration,
    parse_pace,
)

KIND_LABELS = {
    "warmup": "Warm up",
    "run": "Run",
    "recover": "Recover",
    "rest": "Rest",
    "cooldown": "Cool down",
}


def describe_target(target: Target, profile: Profile) -> str:
    if target.type in (None, "none"):
        return "no target"

    if target.type == "pace":
        if target.zone is not None:
            slow, fast = profile.pace_zone(str(target.zone))
            return (
                f"{target.zone} "
                f"{format_pace(slow, profile.imperial)}"
                f"-{format_pace(fast, profile.imperial)}"
            )
        unit = "mi" if profile.imperial else "km"
        slow = parse_pace(target.slow, unit)
        fast = parse_pace(target.fast, unit)
        return f"{format_pace(slow, profile.imperial)}-{format_pace(fast, profile.imperial)}"

    if target.type == "hr":
        if target.zone is not None:
            low, high = profile.hr_zone(int(target.zone))
            return f"HR Z{target.zone} {low}-{high} bpm"
        return f"HR {target.low}-{target.high} bpm"

    if target.type == "cadence":
        return f"cadence {target.low}-{target.high} spm"

    return target.type


def describe_extent(step: Step, profile: Profile) -> str:
    if step.duration is not None:
        return format_duration(parse_duration(step.duration))
    if step.distance is not None:
        return format_distance(parse_distance(step.distance), profile.imperial)
    if step.until == "lap":
        return "lap button"
    return "?"


def _render_step(step: Step, profile: Profile, number: str, lines: list[str], indent: int) -> None:
    pad = "  " * indent
    if step.is_repeat:
        note = f"   ({step.note})" if step.note else ""
        lines.append(f"  {number:<6} {pad}Repeat x{step.reps}{note}")
        for index, child in enumerate(step.steps, start=1):
            _render_step(child, profile, f"{number}.{index}", lines, indent + 1)
        return

    label = KIND_LABELS.get(step.kind, step.kind)
    extent = describe_extent(step, profile)
    target = describe_target(step.target, profile)
    line = f"  {number:<6} {pad}{label:<10} {extent:<12} {target}"
    if step.note:
        line += f"   ({step.note})"
    lines.append(line.rstrip())


def render_workout(compiled: CompiledWorkout, profile: Profile) -> str:
    workout = compiled.source
    date = dt.date.fromisoformat(compiled.date)
    estimate = format_duration(compiled.estimated_seconds)
    header = f"{date:%a %d %b}  {workout.name}"
    lines = [f"{header}{'':<4}(~{estimate})"]
    if workout.notes:
        lines.append(f"         {workout.notes}")
    for index, step in enumerate(workout.steps, start=1):
        _render_step(step, profile, str(index), lines, 0)
    return "\n".join(lines)


def render_plan(plan: Plan, compiled: list[CompiledWorkout], profile: Profile) -> str:
    total = sum(c.estimated_seconds for c in compiled)
    lines = [
        f"{plan.plan} - {len(compiled)} workout(s), ~{format_duration(total)} total",
        "",
    ]
    for item in compiled:
        lines.append(render_workout(item, profile))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
