"""Human-readable rendering of a plan.

This is the safety net. Nothing reaches your Garmin account until you have
seen this and agreed with it, because a structured workout that is subtly
wrong is worse than no workout at all -- the watch will beep at you for an
hour trying to enforce it.
"""

from __future__ import annotations

import datetime as dt

from .compile import CompiledWorkout, resolve_pace_bounds
from .plan import Plan, Step, Target
from .profile import Profile
from .units import format_distance, format_duration, format_pace, parse_distance, parse_duration

KIND_LABELS = {
    "warmup": "Warm up",
    "run": "Run",
    "stride": "Stride",
    "exercise": "Exercise",
    "recover": "Recover",
    "rest": "Rest",
    "cooldown": "Cool down",
}


def describe_target(target: Target, profile: Profile) -> str:
    if target.type in (None, "none"):
        return "no target"

    if target.type == "pace":
        slow, fast = resolve_pace_bounds(target, profile)
        label = f"{profile.canonical_zone(str(target.zone))} " if target.zone is not None else ""
        return f"{label}{format_pace(slow, profile.imperial)}-{format_pace(fast, profile.imperial)}"

    if target.type == "hr":
        if target.zone is not None:
            low, high = profile.hr_zone(int(target.zone))
            return f"HR Z{target.zone} {low}-{high} bpm"
        return f"HR {target.low}-{target.high} bpm"

    if target.type == "cadence":
        return f"cadence {target.low}-{target.high} spm"

    if target.type == "power":
        if target.zone is not None:
            return f"power Z{target.zone}"
        return f"power {target.low}-{target.high} W"

    if target.type == "rpe":
        return f"RPE {target.value}/10"

    return target.type


def describe_extent(step: Step, profile: Profile) -> str:
    if step.count is not None:
        return f"{step.count} reps"
    if step.duration is not None:
        return format_duration(parse_duration(step.duration))
    if step.distance is not None:
        text = format_distance(parse_distance(step.distance), profile.imperial)
        if step.grade:
            text += f" @{step.grade:+g}%"
        return text
    if step.until == "lap":
        return "lap button"
    return "?"


def step_label(step: Step) -> str:
    if step.kind == "exercise":
        label = step.exercise or "Exercise"
        if step.weight:
            label += f" {step.weight:g} kg"
        return label
    return KIND_LABELS.get(step.kind, step.kind)


def _render_step(step: Step, profile: Profile, number: str, lines: list[str], indent: int) -> None:
    pad = "  " * indent
    if step.is_repeat:
        note = f"   ({step.note})" if step.note else ""
        lines.append(f"  {number:<6} {pad}Repeat x{step.reps}{note}")
        for index, child in enumerate(step.steps, start=1):
            _render_step(child, profile, f"{number}.{index}", lines, indent + 1)
        return

    label = step_label(step)
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
    tags = " ".join(t for t in (workout.role, workout.phase) if t)
    header = f"{date:%a %d %b}  {workout.name}"
    if tags:
        header += f"  [{tags}]"
    lines = [f"{header}{'':<4}(~{estimate})"]
    if workout.notes:
        lines.append(f"         {workout.notes}")
    for index, step in enumerate(workout.steps, start=1):
        _render_step(step, profile, str(index), lines, 0)
    return "\n".join(lines)


def render_plan(plan: Plan, compiled: list[CompiledWorkout], profile: Profile) -> str:
    total = sum(c.estimated_seconds for c in compiled)
    head = f"{plan.plan} - {len(compiled)} workout(s), ~{format_duration(total)} total"
    race = plan.a_race
    if race:
        head += f" - A race {race.name} on {race.date.isoformat()}"
    lines = [head, ""]
    for item in compiled:
        lines.append(render_workout(item, profile))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
