"""The DSL additions: power, RPE, strides, strength, grade, roles, races."""

import pytest

from gpp.compile import compile_plan, compile_workout, garmin_exercise_name
from gpp.plan import Plan, PlanError
from gpp.profile import Profile

PROFILE = Profile.from_dict(
    {
        "name": "t",
        "pace": {"threshold": "4:00/km"},
        "hr": {"lthr": 170},
        "power": {"cp": 300, "pace_at_cp": "4:05/km"},
    }
)


def plan(steps, **workout):
    base = {"name": "W", "date": "2026-09-24", "steps": steps}
    base.update(workout)
    return {"plan": "p", "workouts": [base]}


def compiled_steps(steps, **workout):
    p = Plan.from_dict(plan(steps, **workout))
    return compile_workout(p.workouts[0], PROFILE, p.plan).payload["workoutSegments"][0][
        "workoutSteps"
    ]


# --- power ------------------------------------------------------------------


def test_power_zone_target():
    (step,) = compiled_steps(
        [{"kind": "run", "duration": "10m", "target": {"type": "power", "zone": 4}}]
    )
    assert step["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert step["zoneNumber"] == 4
    assert step["targetValueOne"] is None


def test_power_range_target_in_watts():
    (step,) = compiled_steps(
        [{"kind": "run", "duration": "10m", "target": {"type": "power", "low": 280, "high": 310}}]
    )
    assert step["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert (step["targetValueOne"], step["targetValueTwo"]) == (280.0, 310.0)


def test_inverted_power_range_is_rejected():
    with pytest.raises(PlanError, match="must be greater than"):
        Plan.from_dict(
            plan(
                [
                    {
                        "kind": "run",
                        "duration": "10m",
                        "target": {"type": "power", "low": 310, "high": 280},
                    }
                ]
            )
        )


def test_distance_at_power_estimates_time_through_the_power_model():
    p = Plan.from_dict(
        plan(
            [
                {
                    "kind": "run",
                    "distance": "1km",
                    "target": {"type": "power", "low": 295, "high": 305},
                }
            ]
        )
    )
    result = compile_workout(p.workouts[0], PROFILE, p.plan)
    assert result.estimated_seconds == pytest.approx(245.0, abs=1.0)  # CP pace 4:05/km


# --- RPE --------------------------------------------------------------------


def test_rpe_compiles_to_no_target_with_a_note():
    (step,) = compiled_steps(
        [
            {
                "kind": "run",
                "duration": "10m",
                "target": {"type": "rpe", "value": 7},
                "note": "smooth",
            }
        ]
    )
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert step["description"] == "RPE 7/10 - smooth"


def test_rpe_out_of_range_is_rejected():
    with pytest.raises(PlanError):
        Plan.from_dict(
            plan([{"kind": "run", "duration": "10m", "target": {"type": "rpe", "value": 11}}])
        )


# --- strides ----------------------------------------------------------------


def test_stride_is_an_interval_step():
    (step,) = compiled_steps([{"kind": "stride", "duration": "20s", "target": {"type": "none"}}])
    assert step["stepType"]["stepTypeKey"] == "interval"
    assert step["endConditionValue"] == 20.0


# --- strength ---------------------------------------------------------------


def test_exercise_step_carries_garmin_catalog_fields():
    (step,) = compiled_steps(
        [
            {
                "kind": "exercise",
                "exercise": "goblet squat",
                "category": "squat",
                "count": 10,
                "weight": 16,
            }
        ],
        sport="strength",
    )
    assert step["exerciseName"] == "GOBLET_SQUAT"
    assert step["category"] == "SQUAT"
    assert step["endCondition"]["conditionTypeKey"] == "reps"
    assert step["endConditionValue"] == 10.0
    assert step["weightValue"] == 16.0
    assert step["weightUnit"]["unitKey"] == "kilogram"


def test_exercise_category_defaults_to_last_word_of_name():
    (step,) = compiled_steps([{"kind": "exercise", "exercise": "single leg deadlift", "count": 8}])
    assert step["category"] == "DEADLIFT"


def test_exercise_needs_a_name_and_one_extent():
    with pytest.raises(PlanError, match="needs an `exercise` name"):
        Plan.from_dict(plan([{"kind": "exercise", "count": 10}]))
    with pytest.raises(PlanError, match="exactly one of count"):
        Plan.from_dict(
            plan([{"kind": "exercise", "exercise": "plank", "count": 10, "duration": "30s"}])
        )


def test_count_is_only_for_exercises():
    with pytest.raises(PlanError, match="only belongs on an exercise"):
        Plan.from_dict(plan([{"kind": "run", "count": 10}]))


def test_strength_workout_sport_compiles():
    p = Plan.from_dict(
        plan([{"kind": "exercise", "exercise": "plank", "duration": "45s"}], sport="strength")
    )
    payload = compile_workout(p.workouts[0], PROFILE, p.plan).payload
    assert payload["sportType"]["sportTypeKey"] == "strength_training"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("goblet squat", "GOBLET_SQUAT"),
        ("Bulgarian split-squat", "BULGARIAN_SPLIT_SQUAT"),
        (" plank ", "PLANK"),
    ],
)
def test_exercise_name_normalisation(raw, expected):
    assert garmin_exercise_name(raw) == expected


# --- grade ------------------------------------------------------------------


def test_grade_lengthens_the_time_estimate_but_not_the_garmin_step():
    flat = Plan.from_dict(
        plan([{"kind": "run", "distance": "1km", "target": {"type": "pace", "zone": "easy"}}])
    )
    hill = Plan.from_dict(
        plan(
            [
                {
                    "kind": "run",
                    "distance": "1km",
                    "grade": 6,
                    "target": {"type": "pace", "zone": "easy"},
                }
            ]
        )
    )
    flat_c = compile_workout(flat.workouts[0], PROFILE)
    hill_c = compile_workout(hill.workouts[0], PROFILE)
    assert hill_c.estimated_seconds > flat_c.estimated_seconds * 1.2
    # Garmin has no grade field; the step itself is identical.
    assert hill_c.payload["workoutSegments"][0]["workoutSteps"][0]["endConditionValue"] == 1000.0


# --- roles, phases, races, weeks --------------------------------------------


def test_role_phase_and_races_round_trip():
    data = {
        "plan": "block",
        "race_date": "2026-10-25",
        "races": [
            {"name": "Tune-up", "date": "2026-10-04", "priority": "B", "distance": "5k"},
            {"name": "Goal", "date": "2026-10-25", "priority": "A"},
        ],
        "weeks": [{"start": "2026-09-21", "phase": "build", "note": "second build week"}],
        "workouts": [
            {
                "name": "Long",
                "date": "2026-09-27",
                "sport": "running",
                "role": "long",
                "phase": "build",
                "steps": [
                    {"kind": "run", "duration": "90m", "target": {"type": "pace", "zone": "easy"}}
                ],
            }
        ],
    }
    p = Plan.from_dict(data)
    assert p.a_race.name == "Goal"
    assert p.workouts[0].role == "long"
    assert p.weeks[0].phase == "build"
    assert p.to_dict() == data


def test_race_date_alone_yields_a_synthetic_a_race():
    p = Plan.from_dict(
        {
            "plan": "p",
            "race_date": "2026-11-01",
            "workouts": plan([{"kind": "run", "duration": "1m"}])["workouts"],
        }
    )
    assert p.a_race.date.isoformat() == "2026-11-01"


def test_bad_role_and_priority_rejected():
    with pytest.raises(PlanError):
        Plan.from_dict(plan([{"kind": "run", "duration": "1m"}], role="sprinty"))
    with pytest.raises(PlanError):
        Plan.from_dict(
            {
                "plan": "p",
                "races": [{"name": "x", "date": "2026-10-01", "priority": "D"}],
                "workouts": plan([{"kind": "run", "duration": "1m"}])["workouts"],
            }
        )


def test_zone_aliases_resolve():
    (step,) = compiled_steps(
        [{"kind": "run", "duration": "10m", "target": {"type": "pace", "zone": "T"}}]
    )
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"


def test_workout_notes_become_the_first_step_cue():
    p = Plan.from_dict(
        plan(
            [
                {"kind": "warmup", "duration": "10m"},
                {"kind": "run", "duration": "20m", "note": "hold form"},
            ],
            notes="Steady aerobic work.",
        )
    )
    steps = compile_workout(p.workouts[0], PROFILE).payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["description"] == "Steady aerobic work."
    assert steps[1]["description"] == "hold form"


def test_changing_threshold_changes_the_content_tag():
    """#16: re-pushing after a threshold change must replace, not skip."""
    p = Plan.from_dict(
        plan([{"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "threshold"}}])
    )
    a = compile_plan(p, Profile.from_dict({"pace": {"threshold": "4:00/km"}}))[0].tag
    b = compile_plan(p, Profile.from_dict({"pace": {"threshold": "4:10/km"}}))[0].tag
    assert a != b
