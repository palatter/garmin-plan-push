"""Pace <-> power transpilation of a whole plan.

Stryd's users run to power; most plans are written to pace. With a critical
power and the pace that goes with it in the profile, every pace target
becomes a watt range on the same linear model models.power_for_pace uses,
and back again. Zone names are kept in a note so nothing is lost.
"""

from __future__ import annotations

from copy import deepcopy

from . import models
from .compile import resolve_pace_bounds
from .plan import Plan, Step, Target
from .profile import Profile, ProfileError


def _needs_power(profile: Profile) -> tuple[int, float]:
    if not profile.power_cp or not profile.power_pace_at_cp:
        raise ProfileError(
            "transpiling to power needs [power] cp (and ideally pace_at_cp) in the profile"
        )
    return profile.power_cp, profile.power_pace_at_cp


def _to_power(step: Step, profile: Profile, cp: int, cp_pace: float) -> None:
    if step.target.type != "pace":
        return
    slow, fast = resolve_pace_bounds(step.target, profile)
    low = models.power_for_pace(slow, cp, cp_pace)
    high = models.power_for_pace(fast, cp, cp_pace)
    label = step.target.zone
    step.target = Target(type="power", low=round(low), high=max(round(high), round(low) + 1))
    if label and not (step.note or "").startswith(f"{label}"):
        step.note = f"{label}" + (f" - {step.note}" if step.note else "")


def _to_pace(step: Step, profile: Profile, cp: int, cp_pace: float) -> None:
    t = step.target
    if t.type != "power" or t.low is None:
        return
    slow = models.pace_for_power(t.low, cp, cp_pace)
    fast = models.pace_for_power(t.high or t.low, cp, cp_pace)
    from .units import format_pace

    step.target = Target(
        type="pace",
        slow=format_pace(slow, profile.imperial),
        fast=format_pace(fast, profile.imperial),
    )


def _walk(steps: list[Step], fn) -> None:
    for s in steps:
        if s.is_repeat:
            _walk(s.steps, fn)
        else:
            fn(s)


def to_power(plan: Plan, profile: Profile) -> Plan:
    cp, cp_pace = _needs_power(profile)
    out = deepcopy(plan)
    for w in out.workouts:
        _walk(w.steps, lambda s: _to_power(s, profile, cp, cp_pace))
    return out


def to_pace(plan: Plan, profile: Profile) -> Plan:
    cp, cp_pace = _needs_power(profile)
    out = deepcopy(plan)
    for w in out.workouts:
        _walk(w.steps, lambda s: _to_pace(s, profile, cp, cp_pace))
    return out
