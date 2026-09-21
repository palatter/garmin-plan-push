"""Session load, the dashboard, and the adaptation rules."""

import datetime as dt

import pytest

from gpp import adapt, load
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"pace": {"threshold": "4:00/km"}})


def W(date, name, steps, **extra):
    base = {"name": name, "date": date, "steps": steps}
    base.update(extra)
    return base


def run(duration, zone="easy"):
    return {"kind": "run", "duration": duration, "target": {"type": "pace", "zone": zone}}


def P(workouts, **extra):
    return Plan.from_dict(dict({"plan": "p", "workouts": workouts}, **extra))


# --- load -------------------------------------------------------------------


def test_load_orders_sessions_like_a_coach_would():
    easy = P([W("2026-09-22", "E", [run("60m")])]).workouts[0]
    thr = P([W("2026-09-22", "T", [run("40m", "threshold")])]).workouts[0]
    long = P([W("2026-09-22", "L", [run("120m")])]).workouts[0]
    e, t, lo = (load.session_load(w, PROFILE) for w in (easy, thr, long))
    assert e < t < lo


def test_weekly_stats_fill_empty_weeks_and_infer_roles():
    p = P(
        [
            W("2026-09-22", "Q", [run("30m", "threshold")]),
            W("2026-10-06", "Long", [run("150m")]),
        ]
    )
    weeks = load.weekly_stats(p, PROFILE)
    assert [w.start.isoformat() for w in weeks] == ["2026-09-21", "2026-09-28", "2026-10-05"]
    assert weeks[0].quality_sessions == 1
    assert weeks[1].sessions == 0
    assert weeks[2].longest_run_metres > 0


def test_monotony_needs_three_days_and_flags_flat_weeks():
    p = P(
        [
            W((dt.date(2026, 9, 21) + dt.timedelta(days=i)).isoformat(), f"S{i}", [run("45m")])
            for i in range(7)
        ]
    )
    week = load.weekly_stats(p, PROFILE)[0]
    assert week.monotony() == load.MONOTONY_CAP
    sparse = P([W("2026-09-22", "A", [run("45m")])])
    assert load.weekly_stats(sparse, PROFILE)[0].monotony() is None


def test_dashboard_totals():
    p = P(
        [
            W("2026-09-22", "A", [{"kind": "run", "distance": "10km"}]),
            W("2026-09-24", "B", [{"kind": "run", "distance": "5km"}]),
        ]
    )
    dash = load.plan_dashboard(p, PROFILE)
    assert dash["total_km"] == 15.0
    assert dash["sessions"] == 2
    assert dash["longest_run_km"] == 10.0
    assert dash["weeks"][0]["km"] == 15.0


# --- layoffs and pauses -----------------------------------------------------


@pytest.mark.parametrize(
    "days,tier",
    [(3, "resume"), (7, "resume"), (10, "restart-phase"), (20, "back-to-base"), (40, "foundation")],
)
def test_layoff_tiers(days, tier):
    assert adapt.layoff_tier(days)[0] == tier


def test_scale_workout_shortens_work_not_recoveries():
    w = P(
        [
            W(
                "2026-09-22",
                "Q",
                [
                    {"kind": "warmup", "duration": "10m"},
                    {
                        "kind": "repeat",
                        "reps": 4,
                        "steps": [run("5m", "threshold"), {"kind": "recover", "duration": "2m"}],
                    },
                    {"kind": "run", "distance": "4km"},
                ],
            )
        ]
    ).workouts[0]
    scaled = adapt.scale_workout(w, 0.5)
    assert scaled.steps[0].duration == "5m"
    assert (
        scaled.steps[1].steps[0].duration == "3m"
    )  # rounded 2.5 -> 3? no: 5*0.5=2.5 -> round -> 2
    assert scaled.steps[1].steps[1].duration == "2m"  # recovery untouched
    assert scaled.steps[2].distance == "2km"


def test_pause_drops_shifts_and_scales_the_return():
    p = P(
        [
            W("2026-09-22", "Before", [run("40m")]),
            W("2026-09-25", "During", [run("40m")]),
            W("2026-10-02", "After", [run("40m", "threshold")], role="quality"),
            W("2026-10-09", "Later", [run("60m")]),
        ]
    )
    result = adapt.pause_plan(p, dt.date(2026, 9, 24), 10, "flu")
    names = {w.name: w for w in result.plan.workouts}
    assert names["Before"].date == dt.date(2026, 9, 22)  # untouched
    assert names["During"].date == dt.date(2026, 10, 5)  # the whole tail shifts by the pause
    assert names["During"].steps[0].duration == "32m"  # 0.8 for an 8-14 day layoff
    assert names["After"].date == dt.date(2026, 10, 12)
    assert names["After"].steps[0].duration == "40m"  # beyond the first week back
    assert names["Later"].date == dt.date(2026, 10, 19)
    assert not result.dropped
    assert any("restart-phase" in r for r in result.reasons)


def test_pause_protects_the_race():
    p = P(
        [
            W("2026-10-20", "Sharpen", [run("30m")]),
            W("2026-10-25", "Race", [run("10km")], role="race"),
        ],
        race_date="2026-10-25",
    )
    result = adapt.pause_plan(p, dt.date(2026, 10, 18), 6)
    assert any("no room before the race" in d for d in result.dropped)
    assert any(w.name == "Race" and w.date == dt.date(2026, 10, 25) for w in result.plan.workouts)


# --- missed sessions --------------------------------------------------------


def test_missed_easy_is_simply_skipped():
    p = P(
        [
            W("2026-09-22", "Easy", [run("40m")], role="easy"),
            W("2026-09-24", "Q", [run("30m", "threshold")], role="quality"),
        ]
    )
    result = adapt.replan_missed(p, [dt.date(2026, 9, 22)], PROFILE)
    assert [w.name for w in result.plan.workouts] == ["Q"]
    assert "simply skipped" in result.reasons[0]


def test_missed_quality_with_another_soon_is_dropped():
    p = P(
        [
            W("2026-09-22", "Q1", [run("30m", "threshold")], role="quality"),
            W("2026-09-24", "Q2", [run("30m", "interval")], role="quality"),
        ]
    )
    result = adapt.replan_missed(p, [dt.date(2026, 9, 22)], PROFILE)
    assert [w.name for w in result.plan.workouts] == ["Q2"]
    assert "next quality session" in result.reasons[0]


def test_missed_quality_moves_to_a_free_day_and_replaces_an_easy_run():
    p = P(
        [
            W("2026-09-22", "Q", [run("30m", "threshold")], role="quality"),
            W("2026-09-23", "Easy", [run("40m")], role="easy"),
            W("2026-09-27", "Long", [run("100m")], role="long"),
        ]
    )
    result = adapt.replan_missed(p, [dt.date(2026, 9, 22)], PROFILE)
    by_name = {w.name: w.date for w in result.plan.workouts}
    assert by_name["Q"] == dt.date(2026, 9, 23)
    assert "Easy" not in by_name
    assert any("moved to 2026-09-23" in r for r in result.reasons)


def test_missed_long_run_prefers_the_weekend():
    p = P([W("2026-09-23", "Long", [run("100m")], role="long")])  # a Wednesday
    result = adapt.replan_missed(p, [dt.date(2026, 9, 23)], PROFILE)
    assert result.plan.workouts[0].date == dt.date(2026, 9, 26)  # Saturday


def test_replan_respects_availability():
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:00/km"}, "availability": {"days": ["tue", "sat"]}}
    )
    p = P([W("2026-09-22", "Q", [run("30m", "threshold")], role="quality")])  # Tuesday
    result = adapt.replan_missed(p, [dt.date(2026, 9, 22)], profile)
    # Wed/Thu/Fri are not allowed and Sat is 4 days away -> dropped, and it says why
    assert result.plan.workouts == []
    assert "no free day" in result.reasons[0]
