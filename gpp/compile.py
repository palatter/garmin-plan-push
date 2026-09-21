"""DSL -> Garmin Connect workout JSON.

This is where correctness lives. It is pure: no network, no credentials, fully
unit-testable. If this module is right, the push layer is just plumbing.

Two Garmin quirks worth knowing, both handled here:

  * `stepOrder` is a flat counter across the whole workout, and a repeat group
    consumes an order slot itself before its children do.
  * pace targets are stored as METRES PER SECOND, so a *faster* pace is a
    *larger* number. Getting this backwards silently produces a workout that
    tells you to run your recovery jog at 5k pace, which the watch will
    happily enforce. We always write One = slower, Two = faster.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

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
    "recover": "recovery",
    "rest": "rest",
    "cooldown": "cooldown",
}


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


def _compile_target(target: Target, profile: Profile, where: str) -> dict[str, Any]:
    """Return the targetType / targetValueOne / targetValueTwo fragment."""
    if target.type in (None, "none"):
        return {
            "targetType": TARGET_TYPES["no.target"],
            "targetValueOne": None,
            "targetValueTwo": None,
        }

    if target.type == "pace":
        if target.zone is not None:
            slow_spk, fast_spk = profile.pace_zone(str(target.zone))
        else:
            slow_spk = parse_pace(target.slow, "mi" if profile.imperial else "km")
            fast_spk = parse_pace(target.fast, "mi" if profile.imperial else "km")
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

    raise CompileError(f"{where}: unsupported target type {target.type!r}")


# --- end conditions ---------------------------------------------------------


def _compile_end_condition(step: Step, profile: Profile, where: str) -> dict[str, Any]:
    if step.duration is not None:
        seconds = parse_duration(step.duration)
        if seconds <= 0:
            raise CompileError(f"{where}: duration must be positive")
        return {
            "endCondition": END_CONDITIONS["time"],
            "endConditionValue": float(seconds),
        }
    if step.distance is not None:
        metres = parse_distance(step.distance)
        if metres <= 0:
            raise CompileError(f"{where}: distance must be positive")
        return {
            "endCondition": END_CONDITIONS["distance"],
            "endConditionValue": float(metres),
        }
    if step.until == "lap":
        return {
            "endCondition": END_CONDITIONS["lap.button"],
            "endConditionValue": None,
        }
    raise CompileError(f"{where}: no end condition")


# --- duration estimation ----------------------------------------------------


def _estimate_step_seconds(step: Step, profile: Profile) -> float:
    if step.is_repeat:
        inner = sum(_estimate_step_seconds(c, profile) for c in step.steps)
        return inner * int(step.reps or 1)
    if step.duration is not None:
        return parse_duration(step.duration)
    if step.distance is not None:
        metres = parse_distance(step.distance)
        pace_spk = _target_mid_pace(step.target, profile)
        return (metres / 1000.0) * pace_spk
    return 0.0  # lap-button step: genuinely unknown


def _target_mid_pace(target: Target, profile: Profile) -> float:
    """Seconds per km to assume for a distance step."""
    if target.type == "pace":
        if target.zone is not None:
            slow, fast = profile.pace_zone(str(target.zone))
        else:
            slow = parse_pace(target.slow, "mi" if profile.imperial else "km")
            fast = parse_pace(target.fast, "mi" if profile.imperial else "km")
        return (slow + fast) / 2
    return profile.estimate_pace()


# --- steps ------------------------------------------------------------------


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
        "description": step.note,
    }
    compiled.update(_compile_end_condition(step, profile, where))
    compiled.update(_compile_target(step.target, profile, where))
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
    #
    # That leaves `parent_child_id` unused here, deliberately: it is threaded
    # through so executable steps can carry it, and a nested group does not.
    # Observed Connect payloads only ever show one level of nesting, so if a
    # two-level workout renders oddly on the watch this is the first place to
    # look -- `gpp push --verify` will report what Garmin actually stored.
    del parent_child_id
    return group


# --- workouts ---------------------------------------------------------------


def content_tag(plan_name: str, payload: dict[str, Any]) -> str:
    """A stable marker identifying this tool's workouts and their content.

    Pushing an unchanged workout twice is a no-op because the tag matches;
    editing a step changes the hash, so the push layer knows to replace it.
    The plan slug lets you keep several plans on one calendar.
    """
    skeleton = json.dumps(payload.get("workoutSegments", []), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(skeleton.encode("utf-8")).hexdigest()[:8]
    slug = "".join(c for c in plan_name.lower() if c.isalnum())[:16] or "plan"
    return f"[{TAG_PREFIX}:{slug}:{digest}]"


def compile_workout(workout: Workout, profile: Profile, plan_name: str = "plan") -> CompiledWorkout:
    sport = SPORT_TYPES.get(workout.sport)
    if sport is None:
        raise CompileError(f"unsupported sport {workout.sport!r}")

    counters = _Counters()
    steps = [
        _compile_step(step, profile, counters, f"{workout.name} step {index}", None)
        for index, step in enumerate(workout.steps, start=1)
    ]

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
