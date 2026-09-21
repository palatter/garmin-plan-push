"""Flatten a workout into drawable blocks.

A structured workout is a tree, but what a runner wants to see is the *shape*
of the session: where the hard bits are, how long the recoveries get, whether
the thing ramps or alternates. So repeats are expanded into real blocks with
real durations, and each block carries an intensity in 0..1 that the UI maps
to colour.

Lap-button steps have no knowable duration. Rather than silently drawing them
as zero width (which hides them) or guessing (which lies), they get a nominal
width and are flagged `open_ended` so the UI can draw them differently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .compile import _estimate_step_seconds
from .plan import Step, Target, Workout
from .profile import Profile
from .render import describe_extent, describe_target
from .units import parse_distance, parse_duration, parse_pace

# Where each named zone sits on a 0..1 intensity scale, for colour.
ZONE_INTENSITY = {
    "recovery": 0.08,
    "easy": 0.24,
    "steady": 0.42,
    "marathon": 0.56,
    "threshold": 0.72,
    "interval": 0.88,
    "repetition": 1.0,
}

KIND_FALLBACK_INTENSITY = {
    "warmup": 0.22,
    "run": 0.65,
    "recover": 0.12,
    "rest": 0.0,
    "cooldown": 0.18,
}

# Drawn width for a step whose real duration cannot be known.
OPEN_ENDED_NOMINAL_SECONDS = 60.0

MAX_BLOCKS = 400


@dataclass
class Block:
    label: str
    kind: str
    seconds: float
    intensity: float
    target: str
    extent: str
    open_ended: bool = False
    rep: int | None = None
    of: int | None = None
    note: str | None = None


def intensity_for(step: Step, profile: Profile) -> float:
    target = step.target
    if target.type == "pace":
        if target.zone is not None:
            name = str(target.zone).lower()
            if name in ZONE_INTENSITY:
                return ZONE_INTENSITY[name]
        elif target.fast is not None:
            # Position an explicit pace against the athlete's own zones.
            try:
                pace = parse_pace(target.fast, "mi" if profile.imperial else "km")
            except Exception:
                return KIND_FALLBACK_INTENSITY.get(step.kind, 0.5)
            return _intensity_from_pace(pace, profile)
    elif target.type == "hr" and target.zone is not None:
        return min(1.0, 0.12 + 0.2 * (int(target.zone) - 1))
    return KIND_FALLBACK_INTENSITY.get(step.kind, 0.5)


def _intensity_from_pace(pace: float, profile: Profile) -> float:
    """Find which of the athlete's zones a raw pace falls in."""
    best_name, best_gap = None, None
    for name in profile.pace_zones:
        slow, fast = profile.pace_zone(name)
        if fast <= pace <= slow:
            return ZONE_INTENSITY.get(name, 0.5)
        gap = min(abs(pace - slow), abs(pace - fast))
        if best_gap is None or gap < best_gap:
            best_name, best_gap = name, gap
    return ZONE_INTENSITY.get(best_name or "", 0.5)


def _emit(step: Step, profile: Profile, out: list[Block], rep: tuple[int, int] | None) -> None:
    if len(out) >= MAX_BLOCKS:
        return
    open_ended = step.until == "lap"
    seconds = _estimate_step_seconds(step, profile)
    if open_ended or seconds <= 0:
        seconds = OPEN_ENDED_NOMINAL_SECONDS
        open_ended = True

    out.append(
        Block(
            label=step.kind,
            kind=step.kind,
            seconds=seconds,
            intensity=intensity_for(step, profile),
            target=describe_target(step.target, profile),
            extent=describe_extent(step, profile),
            open_ended=open_ended,
            rep=rep[0] if rep else None,
            of=rep[1] if rep else None,
            note=step.note,
        )
    )


def _walk(
    steps: list[Step], profile: Profile, out: list[Block], rep: tuple[int, int] | None
) -> None:
    for step in steps:
        if len(out) >= MAX_BLOCKS:
            return
        if step.is_repeat:
            reps = int(step.reps or 1)
            for index in range(1, reps + 1):
                _walk(step.steps, profile, out, (index, reps))
        else:
            _emit(step, profile, out, rep)


def workout_timeline(workout: Workout, profile: Profile) -> list[dict]:
    blocks: list[Block] = []
    _walk(workout.steps, profile, blocks, None)
    return [asdict(b) for b in blocks]


def workout_summary(workout: Workout, profile: Profile) -> dict:
    """Headline numbers a runner actually cares about.

    Totals are computed over the whole tree, NOT over the drawable blocks:
    the block list is capped for display, and summing a capped list beside an
    uncapped distance produced impossible pairings like "38m / 19.4 km".
    """
    blocks: list[Block] = []
    _walk(workout.steps, profile, blocks, None)
    truncated = len(blocks) >= MAX_BLOCKS

    total, hard = _totals(workout.steps, profile)
    return {
        "seconds": total,
        "hard_seconds": hard,
        "metres": _total_distance(workout.steps, profile),
        "blocks": len(blocks),
        "truncated": truncated,
        "open_ended": any(b.open_ended for b in blocks),
    }


def _totals(steps: list[Step], profile: Profile) -> tuple[float, float]:
    """(total seconds, seconds at threshold or harder), over the full tree."""
    total = hard = 0.0
    for step in steps:
        if step.is_repeat:
            inner_total, inner_hard = _totals(step.steps, profile)
            reps = int(step.reps or 1)
            total += inner_total * reps
            hard += inner_hard * reps
            continue
        seconds = _estimate_step_seconds(step, profile)
        if step.until == "lap" or seconds <= 0:
            seconds = OPEN_ENDED_NOMINAL_SECONDS
        total += seconds
        if intensity_for(step, profile) >= ZONE_INTENSITY["threshold"]:
            hard += seconds
    return total, hard


def _total_distance(steps: list[Step], profile: Profile) -> float:
    total = 0.0
    for step in steps:
        if step.is_repeat:
            total += _total_distance(step.steps, profile) * int(step.reps or 1)
        elif step.distance is not None:
            total += parse_distance(step.distance)
        elif step.duration is not None:
            seconds = parse_duration(step.duration)
            pace = _pace_for(step.target, profile)
            total += (seconds / pace) * 1000
        # lap-button steps contribute nothing knowable
    return total


def _pace_for(target: Target, profile: Profile) -> float:
    if target.type == "pace":
        if target.zone is not None:
            slow, fast = profile.pace_zone(str(target.zone))
            return (slow + fast) / 2
        if target.fast is not None:
            unit = "mi" if profile.imperial else "km"
            return (parse_pace(target.slow, unit) + parse_pace(target.fast, unit)) / 2
    return profile.estimate_pace()
