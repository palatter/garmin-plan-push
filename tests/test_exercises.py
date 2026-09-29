"""Strength exercises against Garmin's catalog, which garminconnect ships offline.

The compiler used to guess the category from an exercise's last word, so
"push-ups" went out as PUSH_UPS/PUSH_UPS and "dead bug" as BUG/DEAD_BUG,
pairs Garmin's catalog does not have. `gpp exercises` called a library
method that does not exist and always failed.
"""

import datetime as dt
import re

import pytest
from garminconnect import exercises as garmin

from gpp import checks, cli, exercises
from gpp.compile import compile_workout
from gpp.decompile import workout_from_garmin
from gpp.library import SEED_WORKOUTS
from gpp.plan import Plan, Workout
from gpp.profile import Profile
from gpp.prompt import TEMPLATE

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
DAY = dt.date.today() + dt.timedelta(days=3)


def strength(*names_and_categories):
    steps = [
        {"kind": "exercise", "exercise": name, "count": 10, **({"category": cat} if cat else {})}
        for name, cat in names_and_categories
    ]
    return Plan.from_dict(
        {
            "plan": "Gym",
            "workouts": [
                {"name": "Strength", "date": DAY.isoformat(), "sport": "strength", "steps": steps}
            ],
        }
    )


def pairs(plan):
    compiled = compile_workout(plan.workouts[0], PROFILE, plan.plan).payload
    return [
        (s["category"], s["exerciseName"]) for s in compiled["workoutSegments"][0]["workoutSteps"]
    ]


def test_every_catalog_entry_is_found_by_its_name_and_by_its_own_pair():
    # This is also the contract with garminconnect: the rows gpp reads.
    assert len(garmin.EXERCISES) > 1000
    for entry in garmin.EXERCISES:
        assert exercises.lookup(entry["name"]) is entry
        assert exercises.lookup(entry["exercise"], entry["category"]) is entry


@pytest.mark.parametrize(
    ("written", "pair"),
    [
        ("push-ups", ("PUSH_UP", "PUSH_UP")),
        ("Push Up", ("PUSH_UP", "PUSH_UP")),
        ("calf raises", ("CALF_RAISE", "CALF_RAISE")),
        ("dead bug", ("HIP_STABILITY", "DEAD_BUG")),
        ("step ups", ("SQUAT", "STEP_UP")),
        ("GOBLET_SQUAT", ("SQUAT", "GOBLET_SQUAT")),
        ("farmer\u2019s carry", ("CARRY", "FARMERS_CARRY")),
        ("full plank passe twist", ("PLANK", "FULL_PLANK_PASSE_TWIST")),
    ],
)
def test_names_match_however_they_are_written(written, pair):
    found = exercises.lookup(written)
    assert (found["category"], found["exercise"]) == pair


def test_a_category_garmin_knows_must_hold_the_exercise():
    assert exercises.lookup("goblet squat", "lunge") is None
    assert exercises.lookup("goblet squat", "squats")["exercise"] == "GOBLET_SQUAT"
    # A category Garmin does not have is ignored.
    assert exercises.lookup("goblet squat", "legs")["category"] == "SQUAT"


def test_compiled_steps_carry_the_catalog_pair():
    plan = strength(("push-ups", None), ("dead bug", None), ("Hip Raise", None))
    assert pairs(plan) == [
        ("PUSH_UP", "PUSH_UP"),
        ("HIP_STABILITY", "DEAD_BUG"),
        ("HIP_RAISE", "HIP_RAISE"),
    ]


def test_an_exercise_garmin_does_not_list_keeps_the_guess_and_is_flagged():
    plan = strength(("copenhagen plank", None), ("glute bridge", None), ("plank", None))
    assert pairs(plan)[:2] == [("PLANK", "COPENHAGEN_PLANK"), ("BRIDGE", "GLUTE_BRIDGE")]
    report = checks.check_plan(plan, PROFILE)
    (finding,) = [f for f in report.findings if f.code == "exercise-name"]
    assert finding.severity == "warn"
    assert finding.dates == [
        f"{DAY.isoformat()} copenhagen plank",
        f"{DAY.isoformat()} glute bridge (try: Banded Glute Bridge / Suspension Glute Bridge)",
    ]


def test_the_built_in_strength_session_uses_only_catalog_names():
    workout = Workout.from_dict({**SEED_WORKOUTS["runner-strength"], "date": DAY.isoformat()})
    plan = Plan(plan="Seed", workouts=[workout])
    assert "exercise-name" not in {f.code for f in checks.check_plan(plan, PROFILE).findings}


def test_a_pulled_workout_compiles_back_to_the_same_pair():
    garmin_step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeKey": "interval"},
        "category": "CALF_RAISE",
        "exerciseName": "_3_WAY_SINGLE_LEG_CALF_RAISE",
        "endCondition": {"conditionTypeKey": "reps"},
        "endConditionValue": 12,
    }
    payload = {
        "workoutName": "Calves",
        "sportType": {"sportTypeKey": "strength_training"},
        "workoutSegments": [{"workoutSteps": [garmin_step]}],
    }
    workout = workout_from_garmin(payload, DAY)
    assert pairs(Plan(plan="Pulled", workouts=[workout])) == [
        ("CALF_RAISE", "_3_WAY_SINGLE_LEG_CALF_RAISE")
    ]


def test_gpp_exercises_searches_offline(monkeypatch, capsys):
    def no_sign_in(*a, **kw):
        raise AssertionError("gpp exercises must not sign in")

    monkeypatch.setattr("gpp.client.sign_in_at_terminal", no_sign_in)
    assert cli.main(["exercises", "goblet"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Goblet", "Squat", "SQUAT"]
    assert cli.main(["exercises", "bent over"]) == 0
    assert "Bent-over Row with Barbell" in capsys.readouterr().out
    assert cli.main(["exercises", "zzzz"]) == 1


def test_the_names_the_prompt_suggests_are_all_in_the_catalog():
    rule = TEMPLATE[TEMPLATE.index("11. Name strength") : TEMPLATE.index("in kilograms")]
    names = re.findall(r'"([^"]+)"', rule)
    assert len(names) >= 8
    assert [n for n in names if n != "weight" and exercises.lookup(n) is None] == []
