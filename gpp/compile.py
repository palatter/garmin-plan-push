"""DSL -> Garmin Connect workout JSON.

This is where correctness lives. It is pure: no network, no credentials, fully
unit-testable. If this module is right, the push layer is just plumbing.

Garmin quirks handled here:

  * `stepOrder` is a flat counter across the whole workout, and a repeat group
    consumes an order slot itself before its children do.
  * pace targets are stored as METRES PER SECOND, so a *faster* pace is a
    *larger* number. Getting this backwards silently produces a workout that
    tells you to run your recovery jog at 5k pace, which the watch will
    happily enforce. We always write One = slower, Two = faster.
  * power targets share the heart-rate pattern: a zone number, or an explicit
    low/high range in watts on the same target type.
  * RPE has no Garmin target type, so it compiles to "no target" plus a step
    note ("RPE 7/10") -- honest, and still useful without a strap.
  * strength exercises are ExecutableStepDTOs with `category` / `exerciseName`
    from Garmin's catalog and a `reps` end condition. Names are normalised to
    Garmin's UPPER_SNAKE form; ones the catalog does not know will be
    rejected at upload, which `gpp push --verify` reports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import models
from .constants import (
    END_CONDITIONS,
    SPORT_TYPES,
    STEP_TYPES,
    TAG_PREFIX,
    TARGET_TYPES,
)
from .plan import Plan, Step, Target, Workout
from .profile import Profile
from .units import pace_to_mps, parse_distance, parse_duration, parse_pace

# DSL step kind -> Garmin step type key.
KIND_TO_STEP_TYPE = {
    "warmup": "warmup",
    "run": "interval",
    "stride": "interval",
    "exercise": "interval",
    "recover": "recovery",
    "rest": "rest",
    "cooldown": "cooldown",
}

WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# Longest a "why this session" note can be on the watch's first step.
CUE_LIMIT = 200


class CompileError(ValueError):
    """The plan is valid DSL but cannot be expressed as a Garmin workout."""


@dataclass
class CompiledWorkout:
    """A workout ready to upload, plus everything the CLI needs to talk about it."""

    name: str
    date: str
    payload: dict[str, Any]
    tag: str
    estimated_seconds: float
    source: Workout = field(repr=False, default=None)  # type: ignore[assignment]


@dataclass
class _Counters:
    step_order: int = 0
    step_id: int = 0
    child_step_id: int = 0

    def next_step(self) -> tuple[int, int]:
        self.step_order += 1
        self.step_id += 1
        return self.step_id, self.step_order

    def next_child_group(self) -> int:
        self.child_step_id += 1
        return self.child_step_id


# --- targets ----------------------------------------------------------------


def _no_target() -> dict[str, Any]:
    return {
        "targetType": TARGET_TYPES["no.target"],
        "targetValueOne": None,
        "targetValueTwo": None,
    }


def _compile_target(target: Target, profile: Profile, where: str) -> dict[str, Any]:
    """Return the targetType / targetValueOne / targetValueTwo fragment."""
    if target.type in (None, "none", "rpe"):
        return _no_target()

    if target.type == "pace":
        slow_spk, fast_spk = resolve_pace_bounds(target, profile)
        if fast_spk > slow_spk:
            raise CompileError(f"{where}: pace bounds are inverted")
        # Slower pace -> smaller m/s. One = slower, Two = faster.
        return {
            "targetType": TARGET_TYPES["pace.zone"],
            "targetValueOne": round(pace_to_mps(slow_spk), 4),
            "targetValueTwo": round(pace_to_mps(fast_spk), 4),
        }

    if target.type == "hr":
        if target.zone is not None:
            low, high = profile.hr_zone(int(target.zone))
        else:
            low, high = int(target.low), int(target.high)
        return {
            "targetType": TARGET_TYPES["heart.rate.zone"],
            "targetValueOne": float(low),
            "targetValueTwo": float(high),
        }

    if target.type == "cadence":
        return {
            "targetType": TARGET_TYPES["cadence.zone"],
            "targetValueOne": float(target.low),
            "targetValueTwo": float(target.high),
        }

    if target.type == "power":
        if target.zone is not None:
            return {
                "targetType": TARGET_TYPES["power.zone"],
                "targetValueOne": None,
                "targetValueTwo": None,
                "zoneNumber": int(target.zone),
            }
        return {
            "targetType": TARGET_TYPES["power.zone"],
            "targetValueOne": float(target.low),
            "targetValueTwo": float(target.high),
        }

    raise CompileError(f"{where}: unsupported target type {target.type!r}")


def resolve_pace_bounds(target: Target, profile: Profile) -> tuple[float, float]:
    """(slower, faster) seconds/km for a pace target, from zone or explicit."""
    unit = "mi" if profile.imperial else "km"
    if target.zone is not None:
        return profile.pace_zone(str(target.zone))
    return parse_pace(target.slow, unit), parse_pace(target.fast, unit)


def step_note(step: Step) -> str | None:
    """The note that reaches the watch, with RPE folded in."""
    parts = []
    if step.target.type == "rpe" and step.target.value:
        parts.append(f"RPE {step.target.value}/10")
    if step.note:
        parts.append(step.note)
    return " - ".join(parts) or None


# --- end conditions ---------------------------------------------------------


def _compile_end_condition(step: Step, where: str) -> dict[str, Any]:
    if step.count is not None:
        return {"endCondition": END_CONDITIONS["reps"], "endConditionValue": float(step.count)}
    if step.duration is not None:
        seconds = parse_duration(step.duration)
        if seconds <= 0:
            raise CompileError(f"{where}: duration must be positive")
        return {"endCondition": END_CONDITIONS["time"], "endConditionValue": float(seconds)}
    if step.distance is not None:
        metres = parse_distance(step.distance)
        if metres <= 0:
            raise CompileError(f"{where}: distance must be positive")
        return {"endCondition": END_CONDITIONS["distance"], "endConditionValue": float(metres)}
    if step.until == "lap":
        return {"endCondition": END_CONDITIONS["lap.button"], "endConditionValue": None}
    raise CompileError(f"{where}: no end condition")


# --- duration estimation ----------------------------------------------------

# Seconds to assume per rep for a strength exercise, and per open-ended step.
SECONDS_PER_REP = 3.0
OPEN_STEP_SECONDS = 0.0


def _estimate_step_seconds(step: Step, profile: Profile) -> float:
    if step.is_repeat:
        inner = sum(_estimate_step_seconds(c, profile) for c in step.steps)
        return inner * int(step.reps or 1)
    if step.count is not None:
        return step.count * SECONDS_PER_REP
    if step.duration is not None:
        return parse_duration(step.duration)
    if step.distance is not None:
        metres = parse_distance(step.distance)
        pace_spk = _target_mid_pace(step.target, profile)
        factor = models.gap_factor(step.grade) if step.grade else 1.0
        return (metres / 1000.0) * pace_spk * factor
    return OPEN_STEP_SECONDS  # lap-button step: genuinely unknown


def _target_mid_pace(target: Target, profile: Profile) -> float:
    """Seconds per km to assume for a distance step."""
    if target.type == "pace":
        slow, fast = resolve_pace_bounds(target, profile)
        return (slow + fast) / 2
    if target.type == "power" and target.low and profile.power_cp and profile.power_pace_at_cp:
        mid = (target.low + (target.high or target.low)) / 2
        return models.pace_for_power(mid, profile.power_cp, profile.power_pace_at_cp)
    return profile.estimate_pace()


# --- steps ------------------------------------------------------------------


def garmin_exercise_name(name: str) -> str:
    """Garmin's catalog uses UPPER_SNAKE_CASE ("GOBLET_SQUAT")."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in name.strip())
    return "_".join(cleaned.split()).upper()


def _compile_step(
    step: Step,
    profile: Profile,
    counters: _Counters,
    where: str,
    child_step_id: int | None,
) -> dict[str, Any]:
    if step.is_repeat:
        return _compile_repeat(step, profile, counters, where, child_step_id)

    step_type_key = KIND_TO_STEP_TYPE.get(step.kind)
    if step_type_key is None:
        raise CompileError(f"{where}: unknown step kind {step.kind!r}")

    step_id, step_order = counters.next_step()
    compiled: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepId": step_id,
        "stepOrder": step_order,
        "stepType": STEP_TYPES[step_type_key],
        "description": step_note(step),
    }
    compiled.update(_compile_end_condition(step, where))
    compiled.update(_compile_target(step.target, profile, where))

    if step.kind == "exercise":
        if not step.exercise:
            raise CompileError(f"{where}: exercise step has no exercise name")
        compiled["exerciseName"] = garmin_exercise_name(step.exercise)
        compiled["category"] = garmin_exercise_name(step.category or step.exercise.split()[-1])
        if step.weight:
            compiled["weightValue"] = float(step.weight)
            compiled["weightUnit"] = WEIGHT_UNIT_KG

    if child_step_id is not None:
        compiled["childStepId"] = child_step_id
    return compiled


def _compile_repeat(
    step: Step,
    profile: Profile,
    counters: _Counters,
    where: str,
    parent_child_id: int | None,
) -> dict[str, Any]:
    # The group itself takes an order slot before its children.
    step_id, step_order = counters.next_step()
    group_child_id = counters.next_child_group()

    children = [
        _compile_step(child, profile, counters, f"{where}.{index}", group_child_id)
        for index, child in enumerate(step.steps, start=1)
    ]

    group: dict[str, Any] = {
        "type": "RepeatGroupDTO",
        "stepId": step_id,
        "stepOrder": step_order,
        "stepType": STEP_TYPES["repeat"],
        "numberOfIterations": int(step.reps or 1),
        "smartRepeat": False,
        "childStepId": group_child_id,
        "endCondition": END_CONDITIONS["iterations"],
        "endConditionValue": float(step.reps or 1),
        "workoutSteps": children,
        "description": step.note,
    }
    # NOTE on nesting. `childStepId` on a group is the id its own children
    # carry, so it must stay `group_child_id` even when this group is itself
    # inside another repeat -- overwriting it with the parent's id would
    # orphan this group's children. Membership of the outer group is expressed
    # by position in the parent's `workoutSteps`, not by this field.
    # Observed Connect payloads only ever show one level of nesting, so if a
    # two-level workout renders oddly on the watch this is the first place to
    # look -- `gpp push --verify` will report what Garmin actually stored.
    del parent_child_id
    return group


# --- workouts ---------------------------------------------------------------


def content_tag(plan_name: str, payload: dict[str, Any]) -> str:
    """A stable marker identifying this tool's workouts and their content.

    Pushing an unchanged workout twice is a no-op because the tag matches;
    editing a step -- or changing your threshold, which changes every
    compiled pace -- changes the hash, so the push layer knows to replace it.
    The plan slug lets you keep several plans on one calendar.
    """
    skeleton = json.dumps(payload.get("workoutSegments", []), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(skeleton.encode("utf-8")).hexdigest()[:8]
    slug = "".join(c for c in plan_name.lower() if c.isalnum())[:16] or "plan"
    return f"[{TAG_PREFIX}:{slug}:{digest}]"


def _first_executable(steps: list[dict]) -> dict | None:
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            found = _first_executable(step.get("workoutSteps", []))
            if found:
                return found
        else:
            return step
    return None


def compile_workout(workout: Workout, profile: Profile, plan_name: str = "plan") -> CompiledWorkout:
    sport = SPORT_TYPES.get(workout.sport)
    if sport is None:
        raise CompileError(f"unsupported sport {workout.sport!r}")

    counters = _Counters()
    steps = [
        _compile_step(step, profile, counters, f"{workout.name} step {index}", None)
        for index, step in enumerate(workout.steps, start=1)
    ]

    # "Why this session", on the wrist: the workout's purpose becomes the
    # first step's note when that step has none of its own.
    first = _first_executable(steps)
    if workout.notes and first is not None and not first.get("description"):
        first["description"] = workout.notes.strip()[:CUE_LIMIT]

    estimated = sum(_estimate_step_seconds(s, profile) for s in workout.steps)

    payload: dict[str, Any] = {
        "sportType": sport,
        "workoutName": workout.name,
        "estimatedDurationInSecs": round(estimated),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport,
                "workoutSteps": steps,
            }
        ],
    }

    tag = content_tag(plan_name, payload)
    description = f"{workout.notes.strip()}\n\n{tag}" if workout.notes else tag
    payload["description"] = description

    return CompiledWorkout(
        name=workout.name,
        date=workout.date.isoformat(),
        payload=payload,
        tag=tag,
        estimated_seconds=estimated,
        source=workout,
    )


def compile_plan(plan: Plan, profile: Profile) -> list[CompiledWorkout]:
    return [compile_workout(w, profile, plan.plan) for w in plan.workouts]
