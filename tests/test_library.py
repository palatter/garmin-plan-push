"""Library, templates, re-basing and the return-to-run ramps."""

import datetime as dt

import pytest

from gpp import library
from gpp.plan import Plan


def test_seed_workouts_are_valid_and_listed(tmp_path):
    names = {w["slug"] for w in library.list_workouts(root=tmp_path)}
    assert {"threshold-5x1k", "long-run-with-threshold", "medium-long", "runner-strength"} <= names
    for slug in names:
        w = library.load_workout(slug, dt.date(2026, 9, 22), root=tmp_path)
        assert w.date == dt.date(2026, 9, 22)


def test_save_and_load_a_workout(tmp_path):
    plan = Plan.from_dict(
        {
            "plan": "p",
            "workouts": [
                {
                    "name": "My 4x800",
                    "date": "2026-09-22",
                    "steps": [{"kind": "run", "distance": "800m"}],
                }
            ],
        }
    )
    path = library.save_workout(plan.workouts[0], root=tmp_path)
    assert path.name == "my-4x800.json"
    loaded = library.load_workout("My 4x800", dt.date(2026, 10, 1), root=tmp_path)
    assert loaded.date == dt.date(2026, 10, 1)
    assert loaded.steps[0].distance == "800m"
    assert library.list_workouts(root=tmp_path)[0]["source"] == "mine"


def test_missing_workout_says_so(tmp_path):
    with pytest.raises(library.LibraryError, match="no workout"):
        library.load_workout("nope", dt.date.today(), root=tmp_path)


def test_plan_template_rebases_to_a_monday(tmp_path):
    template = Plan.from_dict(
        {
            "plan": "Block",
            "race_date": "2026-03-15",
            "races": [{"name": "Goal", "date": "2026-03-15", "priority": "A"}],
            "weeks": [{"start": "2026-02-16", "phase": "build"}],
            "workouts": [
                {"name": "A", "date": "2026-02-18", "steps": [{"kind": "run", "duration": "30m"}]},
                {"name": "B", "date": "2026-02-21", "steps": [{"kind": "run", "duration": "60m"}]},
            ],
        }
    )
    library.save_plan(template, root=tmp_path)
    applied = library.apply_plan("Block", dt.date(2026, 9, 21), root=tmp_path)
    assert applied.workouts[0].date == dt.date(2026, 9, 23)  # same weekday offset
    assert applied.workouts[1].date == dt.date(2026, 9, 26)
    assert applied.a_race.date == dt.date(2026, 10, 18)
    assert applied.weeks[0].start == dt.date(2026, 9, 21)


def test_plan_template_can_be_anchored_to_a_race_date(tmp_path):
    template = Plan.from_dict(
        {
            "plan": "Block",
            "race_date": "2026-03-15",
            "workouts": [
                {"name": "A", "date": "2026-03-10", "steps": [{"kind": "run", "duration": "30m"}]}
            ],
        }
    )
    library.save_plan(template, root=tmp_path)
    applied = library.apply_plan(
        "Block", dt.date(2026, 9, 21), race_date=dt.date(2026, 11, 1), root=tmp_path
    )
    assert applied.race_date == dt.date(2026, 11, 1)
    assert applied.workouts[0].date == dt.date(2026, 10, 27)


def test_return_to_run_ramps():
    plan = library.return_to_run(dt.date(2026, 9, 23), "back-to-base")
    assert len(plan.workouts) == 9  # three weeks of three
    assert plan.workouts[0].date == dt.date(2026, 9, 21)
    assert plan.workouts[0].steps[0].duration == "26m"  # 65% of 40
    assert plan.workouts[-1].steps[0].duration == "36m"  # 90% of 40
    assert all(w.phase == "recovery" for w in plan.workouts)
    assert library.return_to_run(dt.date.today(), "resume").workouts == []
    with pytest.raises(library.LibraryError):
        library.return_to_run(dt.date.today(), "sideways")
