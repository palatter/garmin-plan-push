import pytest

from gpp.compile import CompileError, compile_plan
from gpp.plan import Plan, PlanError
from gpp.profile import Profile, ProfileError

PROFILE = Profile.from_dict({"pace": {"threshold": "4:00/km"}})


def plan_with(steps, **workout):
    base = {"name": "W", "date": "2026-09-24", "steps": steps}
    base.update(workout)
    return {"plan": "p", "workouts": [base]}


def test_valid_plan_round_trips():
    plan = Plan.from_dict(plan_with([{"kind": "run", "duration": "30m"}]))
    assert plan.workouts[0].name == "W"
    assert plan.workouts[0].date.isoformat() == "2026-09-24"


def test_rejects_two_end_conditions():
    with pytest.raises(PlanError, match="exactly one of duration"):
        Plan.from_dict(
            plan_with([{"kind": "run", "duration": "30m", "distance": "5km"}])
        )


def test_rejects_no_end_condition():
    with pytest.raises(PlanError, match="exactly one of duration"):
        Plan.from_dict(plan_with([{"kind": "run", "target": {"type": "none"}}]))


def test_rejects_inverted_pace_target():
    """The mistake a model makes most often."""
    with pytest.raises(PlanError, match="must be the quicker pace"):
        Plan.from_dict(
            plan_with(
                [
                    {
                        "kind": "run",
                        "duration": "10m",
                        "target": {"type": "pace", "slow": "4:00/km", "fast": "5:00/km"},
                    }
                ]
            )
        )


def test_rejects_inverted_hr_range():
    with pytest.raises(PlanError, match="must be greater than"):
        Plan.from_dict(
            plan_with(
                [
                    {
                        "kind": "run",
                        "duration": "10m",
                        "target": {"type": "hr", "low": 170, "high": 160},
                    }
                ]
            )
        )


def test_rejects_unknown_step_kind():
    with pytest.raises(PlanError):
        Plan.from_dict(plan_with([{"kind": "sprint", "duration": "10s"}]))


def test_rejects_unknown_field():
    with pytest.raises(PlanError):
        Plan.from_dict(plan_with([{"kind": "run", "duration": "10m", "pace": "fast"}]))


def test_rejects_empty_repeat():
    with pytest.raises(PlanError):
        Plan.from_dict(plan_with([{"kind": "repeat", "reps": 3, "steps": []}]))


def test_rejects_deeply_nested_repeats():
    inner = {"kind": "repeat", "reps": 2, "steps": [{"kind": "run", "duration": "1m"}]}
    middle = {"kind": "repeat", "reps": 2, "steps": [inner]}
    outer = {"kind": "repeat", "reps": 2, "steps": [middle]}
    with pytest.raises(PlanError, match="nested more than"):
        Plan.from_dict(plan_with([outer]))


def test_allows_one_level_of_nesting():
    inner = {"kind": "repeat", "reps": 2, "steps": [{"kind": "run", "duration": "1m"}]}
    outer = {
        "kind": "repeat",
        "reps": 2,
        "steps": [inner, {"kind": "recover", "duration": "2m"}],
    }
    Plan.from_dict(plan_with([outer]))


def test_rejects_duplicate_name_on_same_day():
    data = {
        "plan": "p",
        "workouts": [
            {"name": "W", "date": "2026-09-24", "steps": [{"kind": "run", "duration": "1m"}]},
            {"name": "w ", "date": "2026-09-24", "steps": [{"kind": "run", "duration": "1m"}]},
        ],
    }
    with pytest.raises(PlanError, match="share the name"):
        Plan.from_dict(data)


def test_allows_same_name_on_different_days():
    data = {
        "plan": "p",
        "workouts": [
            {"name": "Easy", "date": "2026-09-24", "steps": [{"kind": "run", "duration": "1m"}]},
            {"name": "Easy", "date": "2026-09-25", "steps": [{"kind": "run", "duration": "1m"}]},
        ],
    }
    Plan.from_dict(data)


def test_rejects_bad_date_format():
    with pytest.raises(PlanError):
        Plan.from_dict(plan_with([{"kind": "run", "duration": "1m"}], date="24/09/2026"))


def test_unknown_zone_fails_at_compile_not_schema():
    """Schema cannot know your zone names; compilation must catch it."""
    plan = Plan.from_dict(
        plan_with(
            [{"kind": "run", "duration": "10m", "target": {"type": "pace", "zone": "sprint"}}]
        )
    )
    with pytest.raises(ProfileError, match="unknown pace zone"):
        compile_plan(plan, PROFILE)


def test_hr_zone_without_lthr_fails_clearly():
    plan = Plan.from_dict(
        plan_with([{"kind": "run", "duration": "10m", "target": {"type": "hr", "zone": 3}}])
    )
    with pytest.raises(ProfileError, match="no `lthr`"):
        compile_plan(plan, PROFILE)
