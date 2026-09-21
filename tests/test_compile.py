import pytest

from gpp.compile import compile_plan, compile_workout
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "test", "pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}})


def make_plan(steps, name="W", date="2026-09-24"):
    return Plan.from_dict(
        {
            "plan": "test plan",
            "workouts": [{"name": name, "date": date, "steps": steps}],
        }
    )


def compile_steps(steps):
    plan = make_plan(steps)
    return compile_workout(plan.workouts[0], PROFILE, plan.plan)


def flat(payload):
    out = []

    def walk(items):
        for item in items:
            out.append(item)
            if item.get("type") == "RepeatGroupDTO":
                walk(item["workoutSteps"])

    walk(payload["workoutSegments"][0]["workoutSteps"])
    return out


# --- structure --------------------------------------------------------------


def test_simple_time_step():
    result = compile_steps([{"kind": "run", "duration": "30m", "target": {"type": "none"}}])
    step = flat(result.payload)[0]
    assert step["type"] == "ExecutableStepDTO"
    assert step["stepType"]["stepTypeKey"] == "interval"
    assert step["endCondition"]["conditionTypeKey"] == "time"
    assert step["endConditionValue"] == 1800.0
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_distance_step_uses_metres():
    result = compile_steps([{"kind": "run", "distance": "800m"}])
    step = flat(result.payload)[0]
    assert step["endCondition"]["conditionTypeKey"] == "distance"
    assert step["endConditionValue"] == 800.0


def test_lap_button_step_has_no_value():
    result = compile_steps([{"kind": "recover", "until": "lap"}])
    step = flat(result.payload)[0]
    assert step["endCondition"]["conditionTypeKey"] == "lap.button"
    assert step["endConditionValue"] is None


def test_kind_maps_to_garmin_step_type():
    result = compile_steps(
        [
            {"kind": "warmup", "duration": "10m"},
            {"kind": "run", "duration": "5m"},
            {"kind": "recover", "duration": "2m"},
            {"kind": "rest", "duration": "1m"},
            {"kind": "cooldown", "duration": "10m"},
        ]
    )
    keys = [s["stepType"]["stepTypeKey"] for s in flat(result.payload)]
    assert keys == ["warmup", "interval", "recovery", "rest", "cooldown"]


# --- repeat groups ----------------------------------------------------------


def test_repeat_group_shape():
    result = compile_steps(
        [
            {"kind": "warmup", "duration": "10m"},
            {
                "kind": "repeat",
                "reps": 5,
                "steps": [
                    {"kind": "run", "distance": "1km"},
                    {"kind": "recover", "duration": "90s"},
                ],
            },
        ]
    )
    steps = flat(result.payload)
    group = steps[1]
    assert group["type"] == "RepeatGroupDTO"
    assert group["numberOfIterations"] == 5
    assert group["endCondition"]["conditionTypeKey"] == "iterations"
    assert group["endConditionValue"] == 5.0
    assert len(group["workoutSteps"]) == 2


def test_step_order_is_flat_and_counts_the_group():
    """The group itself consumes an order slot before its children."""
    result = compile_steps(
        [
            {"kind": "warmup", "duration": "10m"},
            {
                "kind": "repeat",
                "reps": 3,
                "steps": [
                    {"kind": "run", "duration": "1m"},
                    {"kind": "recover", "duration": "1m"},
                ],
            },
            {"kind": "cooldown", "duration": "10m"},
        ]
    )
    orders = [s["stepOrder"] for s in flat(result.payload)]
    assert orders == [1, 2, 3, 4, 5]
    ids = [s["stepId"] for s in flat(result.payload)]
    assert len(set(ids)) == len(ids)


def test_repeat_children_carry_the_group_child_id():
    result = compile_steps(
        [
            {
                "kind": "repeat",
                "reps": 2,
                "steps": [
                    {"kind": "run", "duration": "1m"},
                    {"kind": "recover", "duration": "1m"},
                ],
            }
        ]
    )
    steps = flat(result.payload)
    group = steps[0]
    assert all(c["childStepId"] == group["childStepId"] for c in group["workoutSteps"])


def test_two_repeat_groups_get_distinct_child_ids():
    result = compile_steps(
        [
            {"kind": "repeat", "reps": 2, "steps": [{"kind": "run", "duration": "1m"}]},
            {"kind": "repeat", "reps": 3, "steps": [{"kind": "run", "duration": "1m"}]},
        ]
    )
    groups = [s for s in flat(result.payload) if s["type"] == "RepeatGroupDTO"]
    assert groups[0]["childStepId"] != groups[1]["childStepId"]


# --- targets ----------------------------------------------------------------


def test_pace_zone_target_is_mps_with_slower_first():
    result = compile_steps(
        [{"kind": "run", "distance": "1km", "target": {"type": "pace", "zone": "threshold"}}]
    )
    step = flat(result.payload)[0]
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    # One = slower = smaller m/s. This is the invariant that stops a recovery
    # jog being prescribed at 5k pace.
    assert step["targetValueOne"] < step["targetValueTwo"]
    # threshold zone around 4:00/km -> ~4.17 m/s
    assert step["targetValueTwo"] == pytest.approx(1000 / (240 * 0.99), rel=1e-3)


def test_explicit_pace_range():
    result = compile_steps(
        [
            {
                "kind": "run",
                "duration": "20m",
                "target": {"type": "pace", "slow": "4:10/km", "fast": "4:00/km"},
            }
        ]
    )
    step = flat(result.payload)[0]
    # Compiled values are rounded to 4dp; 0.0001 m/s is ~0.006 s/km.
    assert step["targetValueOne"] == pytest.approx(1000 / 250, abs=1e-3)
    assert step["targetValueTwo"] == pytest.approx(1000 / 240, abs=1e-3)


def test_hr_zone_target_resolves_from_lthr():
    result = compile_steps(
        [{"kind": "run", "duration": "20m", "target": {"type": "hr", "zone": 2}}]
    )
    step = flat(result.payload)[0]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["targetValueOne"] == pytest.approx(round(170 * 0.85))
    assert step["targetValueTwo"] == pytest.approx(round(170 * 0.89))


def test_cadence_target():
    result = compile_steps(
        [
            {
                "kind": "run",
                "distance": "100m",
                "target": {"type": "cadence", "low": 180, "high": 195},
            }
        ]
    )
    step = flat(result.payload)[0]
    assert step["targetType"]["workoutTargetTypeKey"] == "cadence"
    assert (step["targetValueOne"], step["targetValueTwo"]) == (180.0, 195.0)


# --- estimation and tagging -------------------------------------------------


def test_duration_estimate_includes_repeats():
    result = compile_steps(
        [
            {"kind": "warmup", "duration": "10m"},
            {
                "kind": "repeat",
                "reps": 4,
                "steps": [
                    {"kind": "run", "duration": "3m"},
                    {"kind": "recover", "duration": "2m"},
                ],
            },
        ]
    )
    assert result.estimated_seconds == pytest.approx(600 + 4 * 300)


def test_distance_estimate_uses_target_pace():
    result = compile_steps(
        [{"kind": "run", "distance": "1km", "target": {"type": "pace", "zone": "threshold"}}]
    )
    # threshold band midpoint around 4:00/km
    assert result.estimated_seconds == pytest.approx(240 * (1.05 + 0.99) / 2, rel=1e-3)


def test_tag_is_stable_and_content_sensitive():
    a = compile_steps([{"kind": "run", "duration": "30m"}])
    b = compile_steps([{"kind": "run", "duration": "30m"}])
    c = compile_steps([{"kind": "run", "duration": "31m"}])
    assert a.tag == b.tag
    assert a.tag != c.tag
    assert a.tag in a.payload["description"]


def test_notes_are_preserved_alongside_the_tag():
    plan = Plan.from_dict(
        {
            "plan": "p",
            "workouts": [
                {
                    "name": "W",
                    "date": "2026-09-24",
                    "notes": "Keep it honest.",
                    "steps": [{"kind": "run", "duration": "30m"}],
                }
            ],
        }
    )
    compiled = compile_plan(plan, PROFILE)[0]
    assert "Keep it honest." in compiled.payload["description"]
    assert compiled.tag in compiled.payload["description"]


def test_sport_type_is_set_on_workout_and_segment():
    result = compile_steps([{"kind": "run", "duration": "30m"}])
    assert result.payload["sportType"]["sportTypeKey"] == "running"
    assert result.payload["workoutSegments"][0]["sportType"]["sportTypeKey"] == "running"
