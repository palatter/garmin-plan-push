"""Every duration and distance says its unit, and sizes nobody means are
findings: `"distance": 5` was a five-metre step, `"duration": "400m"` six
hours forty, and "999999km" passed without a word."""

import datetime as dt

import pytest

from gpp import checks
from gpp.adapt import scale_workout
from gpp.compile import compile_plan
from gpp.decompile import _extent
from gpp.oneline import parse_extent
from gpp.plan import PLAN_SCHEMA, Plan, PlanError
from gpp.profile import Profile
from gpp.prompt import build_prompt
from gpp.units import duration_text, parse_duration

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})


def plan(*steps, sport="running", role=None, name="Session"):
    workout = {"name": name, "date": "2026-12-01", "sport": sport, "steps": list(steps)}
    if role:
        workout["role"] = role
    return Plan.from_dict({"plan": "P", "workouts": [workout]})


def codes(p):
    report = checks.check(p, PROFILE)
    return [(f.code, f.severity) for f in report.findings if f.code.endswith("-size")]


@pytest.mark.parametrize(
    ("step", "says"),
    [
        ({"kind": "run", "distance": 5}, "distance 5 has no unit"),
        ({"kind": "run", "distance": "8"}, "distance '8' has no unit"),
        ({"kind": "run", "duration": 45}, "duration 45 has no unit"),
        ({"kind": "run", "duration": "45"}, "duration '45' has no unit"),
        ({"kind": "run", "distance": 5.5}, "distance 5.5 has no unit"),
        ({"kind": "exercise", "exercise": "Plank", "duration": 60}, "duration 60 has no unit"),
    ],
)
def test_a_number_without_a_unit_is_refused_and_the_message_says_what_to_write(step, says):
    with pytest.raises(PlanError) as error:
        plan(step)
    assert "Session step 1: " + says in str(error.value)
    assert '"5km"' in str(error.value) or '"45m"' in str(error.value)


def test_a_bare_number_inside_a_repeat_is_located():
    with pytest.raises(PlanError, match=r"Session step 2\.1: distance 400 has no unit"):
        plan(
            {"kind": "warmup", "duration": "10m"},
            {"kind": "repeat", "reps": 6, "steps": [{"kind": "run", "distance": 400}]},
        )


def test_metres_written_in_the_duration_field_are_refused():
    with pytest.raises(PlanError) as error:
        plan({"kind": "run", "duration": "400m"})
    message = str(error.value)
    assert 'duration "400m" means 400 minutes' in message
    assert '"distance": "400m"' in message and '"400min"' in message
    with pytest.raises(PlanError, match="means 100 minutes"):
        plan({"kind": "repeat", "reps": 8, "steps": [{"kind": "run", "duration": "100m"}]})


@pytest.mark.parametrize("text", ["120m", "150m", "400min", "6h40m", "3h20m", "90s", "12:30"])
def test_durations_that_say_what_they_mean_are_accepted(text):
    (compiled,) = compile_plan(plan({"kind": "run", "duration": text}, role="race"), PROFILE)
    assert compiled.estimated_seconds == parse_duration(text)


def test_ninety_minutes_inside_a_repeat_is_still_minutes():
    p = plan({"kind": "repeat", "reps": 2, "steps": [{"kind": "run", "duration": "90m"}]})
    assert compile_plan(p, PROFILE)[0].estimated_seconds == 2 * 90 * 60


def test_the_schema_the_model_is_given_says_string_with_a_unit():
    step = PLAN_SCHEMA["$defs"]["step"]["oneOf"][0]["properties"]
    assert step["duration"]["type"] == "string" and step["distance"]["type"] == "string"
    assert "minutes" in step["duration"]["description"]


def test_the_prompt_states_the_unit_rule():
    text = build_prompt(PROFILE, today=dt.date(2026, 11, 10))
    assert "ALWAYS carry their unit" in text
    assert 'never "duration": "400m"' in text
    assert "A number with no unit" in text


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "run", "distance": "5m"},
        {"kind": "run", "duration": "5s"},
        {"kind": "recover", "distance": "8m"},
        {"kind": "run", "distance": "999999km"},
        {"kind": "run", "duration": "99999h"},
        {"kind": "run", "distance": "250km"},
    ],
)
def test_a_step_of_a_size_nobody_means_blocks(step):
    assert ("step-size", "block") in codes(plan({"kind": "warmup", "duration": "10m"}, step))


def test_what_is_short_or_long_on_purpose_is_left_alone():
    session = plan(
        {"kind": "warmup", "duration": "15m"},
        {
            "kind": "repeat",
            "reps": 6,
            "steps": [{"kind": "stride", "duration": "8s"}, {"kind": "rest", "duration": "5s"}],
        },
        {"kind": "run", "distance": "400m"},
        {"kind": "cooldown", "duration": "10m"},
    )
    assert codes(session) == []
    ride = plan({"kind": "run", "distance": "250km"}, sport="cycling", role="race")
    assert codes(ride) == []
    hundred_miler = plan({"kind": "run", "distance": "100mi"}, role="race")
    assert codes(hundred_miler) == []
    lengths = plan(
        {"kind": "run", "distance": "25m"}, {"kind": "run", "until": "lap"}, sport="swimming"
    )
    assert codes(lengths) == []


def test_a_session_that_adds_up_to_ninety_seconds_or_seven_hours_warns():
    short = checks.check(plan({"kind": "run", "duration": "1:30"}), PROFILE)
    (finding,) = [f for f in short.findings if f.code == "session-size"]
    assert finding.severity == "warn" and "1h30m" in finding.message
    assert finding.dates == ["2026-12-01 Session (1:30)"]
    assert codes(plan({"kind": "run", "duration": "7h"})) == [("session-size", "warn")]
    assert codes(plan({"kind": "run", "duration": "7h"}, sport="cycling")) == []
    assert codes(plan({"kind": "run", "until": "lap"})) == []


def test_gpp_never_writes_a_duration_its_own_validator_refuses():
    assert [duration_text(s) for s in (90, 2700, 5940, 6000, 7200, 12000, 14400)] == [
        "90s",
        "45m",
        "99m",
        "1h40m",
        "2h",
        "3h20m",
        "4h",
    ]
    for seconds in (90, 2700, 6000, 12000, 14400, 86400):
        assert parse_duration(duration_text(seconds)) == seconds
    # A four-hour ride read back from Garmin, and typed as one line.
    back = _extent({"endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 14400.0})
    assert back == {"duration": "4h"}
    assert parse_extent("4h") == {"duration": "4h"}
    assert parse_extent("240min") == {"duration": "4h"}
    plan({"kind": "run", **back}, sport="cycling")
    # And a long run made longer.
    longer = scale_workout(plan({"kind": "run", "duration": "150m"}).workouts[0], 1.4)
    assert longer.steps[0].duration == "3h30m"
    Plan.from_dict({"plan": "P", "workouts": [longer.to_dict()]})
