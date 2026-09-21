"""Weather, daylight, and the history analyses."""

import datetime as dt

import pytest

from gpp import analysis
from gpp import environment as env
from gpp.units import parse_pace

# --- heat -------------------------------------------------------------------


def test_dew_point_from_temperature_and_humidity():
    assert env.dew_point_c(20, 100) == pytest.approx(20, abs=0.1)
    assert env.dew_point_c(30, 50) == pytest.approx(18.4, abs=0.5)


def test_no_slowdown_below_60f_dew_point():
    assert env.heat_slowdown_spk(10) == 0.0  # 50 F


def test_runners_connect_rule_in_si():
    # 70 F dew point = 10 F over -> 0.25 min/mile -> ~9.3 s/km
    dp = (70 - 32) * 5 / 9
    assert env.heat_slowdown_spk(dp) == pytest.approx(0.25 * 60 / 1.609344, abs=0.05)


def test_adjust_pace_slows_the_target():
    base = parse_pace("5:00/km")
    hot = env.adjust_pace(base, temp_c=30, humidity_pct=70)
    assert hot > base


def test_combined_bands():
    assert env.combined_band(12, 2) == "normal"
    assert env.combined_band(28, 20) == "adjust"
    assert env.combined_band(40, 28) == "no-hard-running"


def test_wbgt_bands_are_monotonic():
    assert env.wbgt_band(env.simplified_wbgt(15, 40)) == "low"
    assert env.wbgt_band(env.simplified_wbgt(20, 50)) == "moderate"
    assert env.wbgt_band(env.simplified_wbgt(33, 70)) == "extreme"


def test_conditions_describe_includes_everything():
    d = env.Conditions(temp_c=28, humidity_pct=65).describe(pace_spk=300)
    for key in (
        "dew_point_c",
        "wbgt_c",
        "wbgt_band",
        "band",
        "slowdown_s_per_km",
        "adjusted_pace_spk",
    ):
        assert key in d
    assert d["adjusted_pace_spk"] > 300


def test_parse_forecast_picks_the_hour():
    payload = {"hourly": {"temperature_2m": [10, 12, 14], "relative_humidity_2m": [80, 70, 60]}}
    c = env.parse_forecast(payload, 1)
    assert (c.temp_c, c.humidity_pct) == (12, 70)
    with pytest.raises(env.EnvironmentError_):
        env.parse_forecast({"hourly": {}}, 0)


def test_bad_humidity_rejected():
    with pytest.raises(env.EnvironmentError_):
        env.dew_point_c(20, 0)


# --- daylight ---------------------------------------------------------------


def test_vancouver_equinox_sunrise_is_about_0650_local():
    rise, sett = env.sun_times(49.28, -123.12, dt.date(2026, 9, 21))
    local_rise = rise - dt.timedelta(hours=7)  # PDT
    assert dt.time(6, 40) <= local_rise.time() <= dt.time(7, 5)
    assert (sett - rise) < dt.timedelta(hours=12, minutes=30)


def test_daylight_note_before_sunrise():
    note = env.daylight_note(49.28, -123.12, dt.date(2026, 9, 21), dt.time(5, 30), -7)
    assert note and "before sunrise" in note
    assert env.daylight_note(49.28, -123.12, dt.date(2026, 9, 21), dt.time(12, 0), -7) is None


def test_polar_day_raises():
    with pytest.raises(env.EnvironmentError_):
        env.sun_times(80.0, 0.0, dt.date(2026, 6, 21))


# --- compliance -------------------------------------------------------------


@pytest.mark.parametrize(
    "planned,actual,expected",
    [
        (3600, 3600, "green"),
        (3600, 3000, "green"),
        (3600, 2500, "yellow"),
        (3600, 5000, "yellow"),
        (3600, 1000, "red"),
        (3600, None, "grey"),
    ],
)
def test_compliance_bands(planned, actual, expected):
    assert analysis.compliance(planned, actual) == expected


def test_match_planned_to_actual_uses_the_longest_same_day_activity():
    planned = [{"date": "2026-09-22", "name": "Q", "seconds": 3600}]
    acts = [
        {"date": "2026-09-22", "seconds": 600, "distance_m": 2000},
        {"date": "2026-09-22", "seconds": 3500, "distance_m": 11000},
    ]
    rows = analysis.match_planned_to_actual(planned, acts)
    assert rows[0]["actual_seconds"] == 3500 and rows[0]["status"] == "green"


# --- threshold re-estimation ------------------------------------------------


def test_reestimate_threshold_from_a_hard_hour():
    acts = [
        {"date": "2026-09-10", "distance_m": 14000, "seconds": 3600},  # 4:17/km for an hour
        {"date": "2026-09-12", "distance_m": 8000, "seconds": 3000},  # easy
    ]
    est = analysis.reestimate_threshold(acts, as_of=dt.date(2026, 9, 21))
    assert est is not None
    assert est.threshold_pace == pytest.approx(3600 / 14, rel=0.01)
    assert est.confidence == "high"


def test_short_effort_is_converted_with_riegel_and_marked_medium():
    acts = [{"date": "2026-09-10", "distance_m": 6000, "seconds": 1440}]  # 4:00/km for 24 min
    est = analysis.reestimate_threshold(acts, as_of=dt.date(2026, 9, 21))
    assert est.confidence == "medium"
    assert est.threshold_pace > 240  # slower than the 24-minute pace


def test_reestimate_ignores_old_and_out_of_window_runs():
    acts = [
        {"date": "2026-01-01", "distance_m": 14000, "seconds": 3600},
        {"date": "2026-09-20", "distance_m": 2000, "seconds": 400},
    ]
    assert analysis.reestimate_threshold(acts, as_of=dt.date(2026, 9, 21)) is None


# --- marathon shape ---------------------------------------------------------


def test_marathon_shape_scores():
    ready = analysis.marathon_shape([65] * 26, [30, 32, 34, 28], 42.195)
    assert ready.score >= 95
    thin = analysis.marathon_shape([20] * 26, [12, 14], 42.195)
    assert thin.score < 50
    assert thin.mileage_score < ready.mileage_score


# --- today's advice ---------------------------------------------------------


def test_downgrade_suggested_only_for_hard_days_with_a_reason():
    a = analysis.today_advice("quality", readiness=30)
    assert a.downgrade and "readiness" in a.reasons[0]
    b = analysis.today_advice("easy", readiness=30)
    assert not b.downgrade and b.reasons
    c = analysis.today_advice("quality", readiness=80, hrv_status="balanced", sleep_score=85)
    assert not c.downgrade and not c.reasons
    d = analysis.today_advice("race", readiness=20)
    assert not d.downgrade  # never talk someone out of their race


def test_weekly_from_activities():
    acts = [
        {"date": "2026-09-22", "distance_m": 10000, "seconds": 3000},
        {"date": "2026-09-24", "distance_m": 5000, "seconds": 1500},
    ]
    weeks = analysis.weekly_from_activities(acts)
    assert weeks == [
        {"start": "2026-09-21", "km": 15.0, "seconds": 4500, "runs": 2, "longest_km": 10.0}
    ]
