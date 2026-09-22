"""Property-based tests (#192): the parsers round-trip, the compiler never
crashes on a valid plan, the FIT encoder round-trips, the checks never raise."""

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from gpp import checks, fit
from gpp.compile import compile_workout, content_tag
from gpp.plan import Plan, Workout
from gpp.profile import Profile
from gpp.units import (
    format_distance,
    format_duration,
    format_pace,
    parse_distance,
    parse_duration,
    parse_pace,
)

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}})
ZONES = ["recovery", "easy", "steady", "marathon", "threshold", "interval", "repetition"]


@given(st.integers(min_value=1, max_value=36_000))
def test_durations_round_trip(seconds):
    assert parse_duration(format_duration(seconds)) == seconds


@given(st.integers(min_value=150, max_value=900))
def test_paces_round_trip_within_a_second(sec_per_km):
    assert abs(parse_pace(format_pace(sec_per_km)) - sec_per_km) <= 0.5
    imperial = format_pace(sec_per_km, imperial=True)
    assert abs(parse_pace(imperial) - sec_per_km) <= 1.0


@given(st.integers(min_value=100, max_value=60_000))
def test_distances_round_trip_within_five_metres(metres):
    assert abs(parse_distance(format_distance(metres).replace(" ", "")) - metres) <= 5.0


def executable_steps():
    duration = st.integers(min_value=30, max_value=7200).map(lambda s: f"{s}s")
    distance = st.integers(min_value=100, max_value=30_000).map(lambda m: f"{m}m")
    target = st.one_of(
        st.sampled_from(ZONES).map(lambda z: {"type": "pace", "zone": z}),
        st.integers(min_value=1, max_value=5).map(lambda z: {"type": "hr", "zone": z}),
        st.just({"type": "none"}),
    )
    return st.builds(
        lambda kind, extent, tgt: {"kind": kind, **extent, "target": tgt},
        st.sampled_from(["warmup", "run", "recover", "cooldown"]),
        st.one_of(duration.map(lambda d: {"duration": d}), distance.map(lambda d: {"distance": d})),
        target,
    )


def repeats():
    return st.builds(
        lambda reps, steps: {"kind": "repeat", "reps": reps, "steps": steps},
        st.integers(min_value=1, max_value=12),
        st.lists(executable_steps(), min_size=1, max_size=3),
    )


def workouts():
    return st.builds(
        lambda steps, day: {
            "name": "Session",
            "date": (dt.date(2026, 9, 21) + dt.timedelta(days=day)).isoformat(),
            "steps": steps,
        },
        st.lists(st.one_of(executable_steps(), repeats()), min_size=1, max_size=6),
        st.integers(min_value=0, max_value=60),
    )


def _executables(steps):
    total = 0
    for s in steps:
        total += _executables(s["steps"]) if s["kind"] == "repeat" else 1
    return total


@settings(max_examples=60)
@given(workouts())
def test_compiled_workouts_are_well_formed(data):
    workout = Workout.from_dict(data)
    compiled = compile_workout(workout, PROFILE, "prop")
    steps = compiled.payload["workoutSegments"][0]["workoutSteps"]

    def orders(items):
        for item in items:
            yield item["stepOrder"]
            if item.get("type") == "RepeatGroupDTO":
                yield from orders(item["workoutSteps"])

    seen = list(orders(steps))
    assert seen == sorted(seen) and len(seen) == len(set(seen))
    executables = _executables(data["steps"])
    assert len(seen) == executables + sum(1 for s in data["steps"] if s["kind"] == "repeat")
    assert compiled.tag == content_tag("prop", compiled.payload)
    for item in seen and steps:
        if item.get("type") != "RepeatGroupDTO" and item["targetType"]["workoutTargetTypeKey"] == "pace.zone":
            assert item["targetValueOne"] <= item["targetValueTwo"]  # slower first, faster second


@settings(max_examples=40)
@given(workouts())
def test_fit_round_trips_the_step_count(data):
    workout = Workout.from_dict(data)
    back = fit.workout_from_fit(fit.encode_workout(workout, PROFILE), workout.date, PROFILE)
    assert _executables([s.to_dict() for s in back.steps]) == _executables(data["steps"])


@settings(max_examples=30)
@given(st.lists(workouts(), min_size=1, max_size=8))
def test_checks_never_raise(items):
    # Same-day duplicates get distinct names so the plan validates.
    for index, item in enumerate(items):
        item["name"] = f"Session {index}"
    plan = Plan.from_dict({"plan": "Prop", "workouts": items})
    report = checks.check(plan, PROFILE, today=dt.date(2026, 9, 21))
    assert isinstance(report.to_dict()["findings"], list)
