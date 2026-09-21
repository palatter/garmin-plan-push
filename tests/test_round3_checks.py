"""Round 3: constraint parsing, race-day exclusion, the new checks, and the
adaptation rules that follow them."""

import datetime as dt

from gpp import adapt, checks
from gpp.plan import Plan
from gpp.profile import Profile


def profile(**extra):
    data = {"name": "T", "pace": {"threshold": "4:00/km"}, "hr": {"max": 185}}
    data.update(extra)
    return Profile.from_dict(data)


def session(name, date, minutes=40, zone="easy", role=None, sport=None, distance=None):
    step = {"kind": "run", "target": {"type": "pace", "zone": zone}}
    if distance:
        step["distance"] = distance
    else:
        step["duration"] = f"{minutes}m"
    out = {"name": name, "date": date, "steps": [step]}
    if role:
        out["role"] = role
    if sport:
        out["sport"] = sport
        out["steps"] = [{"kind": "exercise", "exercise": "goblet squat", "count": 10}]
    return out


def plan_of(workouts, **extra):
    return Plan.from_dict({"plan": "T", "workouts": workouts, **extra})


def codes(report):
    return {f.code for f in report.findings}


def week(start, days, minutes=40, zone="easy"):
    """Sessions on the given weekday offsets of the week starting `start` (a Monday)."""
    base = dt.date.fromisoformat(start)
    return [
        session(f"Run {start} {d}", (base + dt.timedelta(days=d)).isoformat(), minutes, zone)
        for d in days
    ]


# --- constraints -------------------------------------------------------------


def test_no_running_mondays_bans_the_day_not_the_running():
    p = profile(athlete={"constraints": ["No running Mondays"]})
    report = checks.check(
        plan_of([session("Easy running", "2026-09-21"), session("Easy running", "2026-09-22")]), p
    )
    hits = [f for f in report.findings if f.code == "constraint-day"]
    assert len(hits) == 1 and hits[0].dates == ["2026-09-21 Easy running"]
    assert "constraint" not in codes(report)


def test_no_long_runs_on_sundays_only_bans_long_runs_that_day():
    p = profile(athlete={"constraints": ["No long runs on Sundays"]})
    report = checks.check(
        plan_of(
            [
                session("Long run", "2026-09-27", 120),
                session("Easy", "2026-09-27", 30),
                session("Long run", "2026-09-26", 120),
            ]
        ),
        p,
    )
    hits = [f for f in report.findings if f.code == "constraint-day"]
    assert hits and hits[0].dates == ["2026-09-27 Long run"]


def test_time_bound_constraint_expires():
    p = profile(athlete={"injuries": ["no hills for 2 weeks"]})
    report = checks.check(
        plan_of(
            [session("Hill repeats", "2026-09-22", 40), session("Hill repeats", "2026-10-13", 40)]
        ),
        p,
    )
    hits = [f for f in report.findings if f.code == "constraint"]
    assert hits and hits[0].dates == ["2026-09-22 Hill repeats"]


def test_generic_words_are_not_keywords():
    assert checks.parse_constraint("no running") is None
    assert checks.parse_constraint("no more than 5 sessions") is None
    assert checks.parse_constraint("no track work")["keyword"] == "track"
    assert checks.parse_constraint("No running Mondays")["weekday"] == 0
    assert checks.parse_constraint("no monotony")["kind"] == "keyword"  # not a Monday rule


# --- race day and taper ------------------------------------------------------


def test_race_day_volume_is_not_taper_volume():
    workouts = []
    for i, start in enumerate(["2026-09-21", "2026-09-28", "2026-10-05"]):
        long_day = (dt.date.fromisoformat(start) + dt.timedelta(days=6)).isoformat()
        workouts += [*week(start, [1, 3, 5], 60), session(f"Long {i}", long_day, 120)]
    # Two taper weeks at roughly half volume, then the race on the Sunday.
    workouts += [*week("2026-10-12", [1, 3, 5], 30), session("Long taper", "2026-10-18", 60)]
    workouts += [
        *week("2026-10-19", [1, 3], 30),
        session("Marathon", "2026-10-25", role="race", zone="marathon", distance="42.2km"),
    ]
    races = [{"name": "Marathon", "date": "2026-10-25", "priority": "A", "distance": "marathon"}]
    report = checks.check(plan_of(workouts, races=races), profile())
    assert "taper-too-shallow" not in codes(report)


def test_rest_days_count_distinct_dates():
    workouts = [
        *week("2026-09-21", [0, 1, 2, 3, 4, 5], 30),
        session("Double A", "2026-09-22", 20),
        session("Double B", "2026-09-24", 20),
    ]
    report = checks.check(plan_of(workouts), profile())
    assert "no-rest-day" not in codes(report)


# --- the new checks ----------------------------------------------------------


def test_long_run_share_is_flagged():
    workouts = [*week("2026-09-21", [1, 3], 30), session("Long", "2026-09-27", 180)]
    assert "long-run-share" in codes(checks.check(plan_of(workouts), profile()))


def test_two_long_runs_in_a_week():
    workouts = [
        session("Long A", "2026-09-23", 120, role="long"),
        session("Long B", "2026-09-27", 120, role="long"),
        session("Easy", "2026-09-25", 30),
    ]
    assert "two-long-runs" in codes(checks.check(plan_of(workouts), profile()))


def test_hard_sessions_around_races():
    workouts = [
        session("Parkrun", "2026-09-26", 20, "interval", role="race"),
        session("Threshold", "2026-09-27", 40, "threshold", role="quality"),
        session("Threshold late", "2026-10-09", 30, "threshold", role="quality"),
        session("Easy", "2026-10-10", 30),
    ]
    races = [{"name": "10k", "date": "2026-10-11", "priority": "A", "distance": "10k"}]
    report = checks.check(plan_of(workouts, races=races), profile())
    assert {"post-race-hard", "pre-race-hard"} <= codes(report)


def test_strength_the_day_before_a_key_session():
    workouts = [
        session("Gym", "2026-09-22", sport="strength"),
        session("Threshold", "2026-09-23", 40, "threshold", role="quality"),
    ]
    assert "strength-before-hard" in codes(checks.check(plan_of(workouts), profile()))


def test_goal_race_without_a_race_day_session():
    p = profile(goal_race={"name": "City 10k", "date": "2026-10-11", "distance": "10k"})
    report = checks.check(plan_of(week("2026-10-05", [1, 3, 5], 30)), p)
    assert "no-race-day" in codes(report)
    p = profile(goal_race={"name": "Big", "date": "2026-10-11", "distance": "marathon"})
    assert "marathon-long-run" in codes(checks.check(plan_of(week("2026-10-05", [1, 3, 5], 30)), p))


def test_weekly_minutes_against_the_availability_envelope():
    p = profile(availability={"weekday_max_minutes": 30, "weekend_max_minutes": 60})
    workouts = week("2026-09-21", [0, 1, 2, 3, 4, 5], 60)
    report = checks.check(plan_of(workouts), p)
    assert "availability-week" in codes(report)


def test_ten_days_without_rest():
    workouts = [*week("2026-09-21", list(range(7)), 30), *week("2026-09-28", [0, 1, 2, 4], 30)]
    assert "no-rest-streak" in codes(checks.check(plan_of(workouts), profile()))


def test_dates_in_the_past_only_when_today_is_known():
    workouts = [*week("2026-09-21", [1, 3], 30), *week("2026-10-05", [1, 3], 30)]
    assert "in-the-past" not in codes(checks.check(plan_of(workouts), profile()))
    report = checks.check(plan_of(workouts), profile(), today=dt.date(2026, 10, 1))
    assert "in-the-past" in codes(report)


def test_target_sanity():
    workouts = [
        {
            "name": "Bad",
            "date": "2026-09-22",
            "steps": [
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "hr", "low": 150, "high": 200},
                },
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "cadence", "low": 120, "high": 140},
                },
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "pace", "slow": "2:40/km", "fast": "2:30/km"},
                },
            ],
        }
    ]
    report = checks.check(plan_of(workouts), profile())
    assert {"target-hr", "target-cadence", "target-pace"} <= codes(report)
    assert not report.ok


# --- adaptation --------------------------------------------------------------


def test_moved_quality_session_is_not_stacked_next_to_another():
    p = profile(availability={"days": ["mon", "thu", "fri", "sat", "sun"]})
    workouts = [
        session("Quality Mon", "2026-09-21", 40, "threshold", role="quality"),
        session("Quality Fri", "2026-09-25", 40, "threshold", role="quality"),
        session("Easy Sat", "2026-09-26", 30),
    ]
    result = adapt.replan_missed(plan_of(workouts), [dt.date(2026, 9, 21)], p)
    assert result.dropped and "dropped rather than stacked" in " ".join(result.reasons)


def test_moved_long_run_avoids_the_day_after_quality():
    workouts = [
        session("Long", "2026-09-27", 120, role="long"),
        session("Quality", "2026-10-02", 40, "threshold", role="quality"),
        session("Easy", "2026-10-06", 30),
    ]
    result = adapt.replan_missed(plan_of(workouts), [dt.date(2026, 9, 27)], profile())
    moved = [w for w in result.plan.workouts if w.name == "Long"]
    assert moved and moved[0].date == dt.date(2026, 10, 4)


def test_clock_format_durations_are_scaled():
    w = plan_of(
        [
            {
                "name": "Long",
                "date": "2026-09-27",
                "steps": [
                    {
                        "kind": "run",
                        "duration": "1:05:00",
                        "target": {"type": "pace", "zone": "easy"},
                    }
                ],
            }
        ]
    ).workouts[0]
    assert adapt.scale_workout(w, 0.5).steps[0].duration == "32:30"


def test_pause_shifts_the_phase_scaffolding():
    p = plan_of(
        week("2026-09-21", [1, 3], 30) + week("2026-09-28", [1, 3], 30),
        weeks=[{"start": "2026-09-21", "phase": "base"}, {"start": "2026-09-28", "phase": "build"}],
    )
    result = adapt.pause_plan(p, dt.date(2026, 9, 24), 7)
    assert [wk.start.isoformat() for wk in result.plan.weeks] == ["2026-09-21", "2026-10-05"]
