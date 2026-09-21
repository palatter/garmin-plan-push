"""The sanity report: every rule fires on the failure it names, and not otherwise."""

import datetime as dt

from gpp import checks
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}})


def run(duration, zone="easy", **extra):
    step = {"kind": "run", "duration": duration, "target": {"type": "pace", "zone": zone}}
    return dict(step, **extra)


def workout(date, name, steps, **extra):
    base = {"name": name, "date": date, "steps": steps}
    base.update(extra)
    return base


def plan(workouts, **extra):
    data = {"plan": "p", "workouts": workouts}
    data.update(extra)
    return Plan.from_dict(data)


def codes(report):
    return {f.code for f in report.findings}


def easy_week(monday: dt.date, km_per_run=8, runs=(0, 2, 4), long_km=None, phase=None):
    out = []
    for offset in runs:
        day = (monday + dt.timedelta(days=offset)).isoformat()
        steps = [
            {
                "kind": "run",
                "distance": f"{km_per_run}km",
                "target": {"type": "pace", "zone": "easy"},
            }
        ]
        out.append(
            workout(day, f"Easy {day}", steps, role="easy", **({"phase": phase} if phase else {}))
        )
    if long_km:
        day = (monday + dt.timedelta(days=6)).isoformat()
        steps = [
            {"kind": "run", "distance": f"{long_km}km", "target": {"type": "pace", "zone": "easy"}}
        ]
        out.append(
            workout(day, f"Long {day}", steps, role="long", **({"phase": phase} if phase else {}))
        )
    return out


MON = dt.date(2026, 9, 21)


def test_clean_plan_has_nothing_to_flag():
    p = plan(easy_week(MON, long_km=14) + easy_week(MON + dt.timedelta(days=7), long_km=15))
    report = checks.check(p, PROFILE)
    assert report.ok
    assert not report.warns, report.text()


# --- what the athlete said --------------------------------------------------


def test_sessions_on_forbidden_days_block():
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:00/km"}, "availability": {"days": ["tue", "thu", "sat"]}}
    )
    p = plan([workout("2026-09-21", "Mon run", [run("30m")])])  # a Monday
    report = checks.check(p, profile)
    assert "availability-day" in codes(report)
    assert not report.ok


def test_sessions_over_the_time_limit_block():
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:00/km"}, "availability": {"weekday_max_minutes": 45}}
    )
    p = plan([workout("2026-09-22", "Long weekday", [run("75m")])])
    assert "availability-length" in codes(checks.check(p, profile))


def test_literal_constraints_block():
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:00/km"}, "athlete": {"constraints": ["no hills for 6 weeks"]}}
    )
    hilly = plan([workout("2026-09-22", "Hill repeats", [run("30m")])])
    graded = plan([workout("2026-09-22", "Session", [dict(run("30m"), grade=6)])])
    clean = plan([workout("2026-09-22", "Flat easy", [run("30m")])])
    assert "constraint" in codes(checks.check(hilly, profile))
    assert "constraint" in codes(checks.check(graded, profile))
    assert "constraint" not in codes(checks.check(clean, profile))


def test_b_race_inside_the_taper_window_blocks():
    p = plan(
        [workout("2026-10-10", "Easy", [run("30m")])],
        races=[
            {"name": "Goal", "date": "2026-10-25", "priority": "A"},
            {"name": "Parkrun", "date": "2026-10-18", "priority": "B"},
        ],
    )
    assert "race-window" in codes(checks.check(p, PROFILE))


# --- progression ------------------------------------------------------------


def test_long_run_spike_against_profile_baseline():
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:00/km"}, "athlete": {"longest_recent_run_km": 12}}
    )
    p = plan(
        [workout("2026-09-27", "Big long run", [{"kind": "run", "distance": "18km"}], role="long")]
    )
    report = checks.check(p, profile)
    assert "long-run-spike" in codes(report)
    assert "18.0 km vs 12.0 km" in report.warns[0].dates[0]


def test_long_run_spike_within_the_plan():
    p = plan(easy_week(MON, long_km=12) + easy_week(MON + dt.timedelta(days=7), long_km=16))
    assert "long-run-spike" in codes(checks.check(p, PROFILE))


def test_ten_percent_long_run_growth_is_fine():
    p = plan(easy_week(MON, long_km=12) + easy_week(MON + dt.timedelta(days=7), long_km=13))
    assert "long-run-spike" not in codes(checks.check(p, PROFILE))


def test_weekly_ramp_over_25_percent():
    p = plan(easy_week(MON, km_per_run=8) + easy_week(MON + dt.timedelta(days=7), km_per_run=12))
    assert "weekly-ramp" in codes(checks.check(p, PROFILE))


def test_five_rising_weeks_without_a_deload():
    weeks = []
    for i, km in enumerate((8, 9, 10, 11, 12)):
        weeks += easy_week(MON + dt.timedelta(days=7 * i), km_per_run=km)
    assert "no-deload" in codes(checks.check(plan(weeks), PROFILE))


def test_a_lighter_week_resets_the_deload_streak():
    weeks = []
    for i, km in enumerate((8, 9, 10, 6, 10)):
        weeks += easy_week(MON + dt.timedelta(days=7 * i), km_per_run=km)
    assert "no-deload" not in codes(checks.check(plan(weeks), PROFILE))


# --- intensity --------------------------------------------------------------


def test_too_much_hard_running():
    p = plan([workout("2026-09-22", "All threshold", [run("60m", "threshold")], role="quality")])
    assert "hard-share" in codes(checks.check(p, PROFILE))


def test_no_middle_gear():
    weeks = []
    for i in range(2):
        d = MON + dt.timedelta(days=7 * i)
        weeks.append(workout(d.isoformat(), f"Easy {i}", [run("60m", "recovery")], role="easy"))
        weeks.append(
            workout(
                (d + dt.timedelta(days=3)).isoformat(),
                f"Hard {i}",
                [run("30m", "interval")],
                role="quality",
            )
        )
        weeks.append(
            workout(
                (d + dt.timedelta(days=5)).isoformat(),
                f"Easy2 {i}",
                [run("60m", "recovery")],
                role="easy",
            )
        )
    assert "no-middle-gear" in codes(checks.check(plan(weeks), PROFILE))


def test_steady_running_satisfies_the_middle_gear():
    weeks = []
    for i in range(2):
        d = MON + dt.timedelta(days=7 * i)
        weeks.append(workout(d.isoformat(), f"Easy {i}", [run("60m", "recovery")], role="easy"))
        weeks.append(
            workout(
                (d + dt.timedelta(days=3)).isoformat(),
                f"Hard {i}",
                [run("30m", "interval")],
                role="quality",
            )
        )
        weeks.append(
            workout(
                (d + dt.timedelta(days=5)).isoformat(),
                f"Steady {i}",
                [run("40m", "steady")],
                role="easy",
            )
        )
    assert "no-middle-gear" not in codes(checks.check(plan(weeks), PROFILE))


def test_back_to_back_quality_days():
    p = plan(
        [
            workout("2026-09-22", "Threshold", [run("30m", "threshold")], role="quality"),
            workout("2026-09-23", "Intervals", [run("20m", "interval")], role="quality"),
        ]
    )
    assert "back-to-back-quality" in codes(checks.check(p, PROFILE))


def test_long_run_straight_after_quality():
    p = plan(
        [
            workout("2026-09-25", "Threshold", [run("30m", "threshold")], role="quality"),
            workout("2026-09-26", "Long", [run("100m")], role="long"),
        ]
    )
    assert "no-recovery-after-quality" in codes(checks.check(p, PROFILE))


def test_monotony_flags_identical_days():
    same = [
        workout((MON + dt.timedelta(days=i)).isoformat(), f"Same {i}", [run("45m")], role="easy")
        for i in range(7)
    ]
    report = checks.check(plan(same), PROFILE)
    assert "monotony" in codes(report)
    assert "no-rest-day" in codes(report)


def test_progression_cap_on_hard_minutes():
    p = plan(
        [
            workout("2026-09-22", "Q1", [run("20m", "threshold")], role="quality"),
            workout("2026-09-29", "Q2", [run("40m", "threshold")], role="quality"),
        ]
    )
    assert "progression-cap" in codes(checks.check(p, PROFILE))


# --- taper ------------------------------------------------------------------


def taper_plan(peak_km, taper_km, sessions_taper=3, hard_in_taper=True):
    race = dt.date(2026, 11, 8)  # a Sunday
    workouts = []
    for i in range(3):  # three build weeks
        monday = race - dt.timedelta(days=7 * (5 - i))
        workouts += easy_week(monday, km_per_run=peak_km / 3, runs=(0, 2, 4))
        workouts.append(
            workout(
                (monday + dt.timedelta(days=1)).isoformat(),
                f"Q{i}",
                [run("20m", "threshold")],
                role="quality",
            )
        )
    for i in range(2):  # two taper weeks
        monday = race - dt.timedelta(days=7 * (2 - i))
        offsets = (0, 2, 4)[:sessions_taper]
        workouts += easy_week(monday, km_per_run=taper_km / 3, runs=offsets)
        if hard_in_taper:
            workouts.append(
                workout(
                    (monday + dt.timedelta(days=1)).isoformat(),
                    f"TQ{i}",
                    [run("10m", "threshold")],
                    role="quality",
                )
            )
    return plan(workouts, race_date=race.isoformat())


def test_good_taper_passes():
    report = checks.check(taper_plan(peak_km=40, taper_km=20), PROFILE)
    assert not any(c.startswith("taper") for c in codes(report)), report.text()


def test_shallow_taper_warns():
    assert "taper-too-shallow" in codes(checks.check(taper_plan(peak_km=40, taper_km=36), PROFILE))


def test_over_deep_taper_warns():
    assert "taper-too-deep" in codes(checks.check(taper_plan(peak_km=40, taper_km=10), PROFILE))


def test_taper_that_drops_sessions_or_intensity_warns():
    fewer = checks.check(taper_plan(40, 20, sessions_taper=1), PROFILE)
    assert "taper-frequency" in codes(fewer)
    flat = checks.check(taper_plan(40, 20, hard_in_taper=False), PROFILE)
    assert "taper-intensity" in codes(flat)


# --- structure and reporting ------------------------------------------------


def test_base_weeks_without_strides_are_informational():
    p = plan(easy_week(MON, phase="base"))
    report = checks.check(p, PROFILE)
    f = next(f for f in report.findings if f.code == "no-strides")
    assert f.severity == "info"


def test_marathon_build_without_medium_long_is_informational():
    p = plan(
        easy_week(MON, phase="build"),
        races=[{"name": "M", "date": "2026-12-06", "priority": "A", "distance": "marathon"}],
    )
    assert "no-medium-long" in codes(checks.check(p, PROFILE))


def test_report_text_and_feedback():
    p = plan(
        [
            workout("2026-09-22", "Threshold", [run("30m", "threshold")], role="quality"),
            workout("2026-09-23", "Intervals", [run("20m", "interval")], role="quality"),
        ]
    )
    report = checks.check(p, PROFILE)
    assert "Sanity report" in report.text()
    assert "consecutive days" in report.feedback()
    assert report.to_dict()["warns"] >= 1
