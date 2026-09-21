"""Physiological models, checked against the numbers in the papers."""

import pytest

from gpp import models
from gpp.models import ModelError

# --- VDOT -------------------------------------------------------------------


def test_vdot_of_a_known_race_matches_daniels_tables():
    # Daniels' table: VDOT 50 is a 41:21 10k, VDOT 52 about 39:50, so a
    # 40:00 10k lands between them.
    vdot = models.vdot_from_race(10_000, 40 * 60)
    assert 51.0 < vdot < 53.0


def test_vdot_round_trips_through_race_prediction():
    vdot = models.vdot_from_race(5_000, 20 * 60)
    seconds = models.race_time_for_vdot(vdot, 5_000)
    assert seconds == pytest.approx(20 * 60, rel=0.01)


def test_vdot_predicts_a_slower_pace_for_longer_races():
    vdot = models.vdot_from_race(5_000, 20 * 60)
    ten_k = models.race_time_for_vdot(vdot, 10_000)
    assert ten_k > 2 * 20 * 60  # pace slows over distance


def test_vdot_zones_are_ordered_fast_to_slow_by_intensity():
    zones = models.vdot_zones(50.0)
    assert zones["easy"][0] > zones["threshold"][0] > zones["interval"][0]
    for slow, fast in zones.values():
        assert slow > fast  # (slower, faster) convention held


def test_vdot_rejects_nonsense():
    with pytest.raises(ModelError):
        models.vdot_from_race(0, 100)


# --- critical speed ---------------------------------------------------------


def test_two_trial_critical_speed_fit():
    # 3 min at 4.5 m/s (810 m) and 12 min at 4.0 m/s (2880 m).
    cs = models.critical_speed([(810, 180), (2880, 720)])
    assert cs.cs_mps == pytest.approx((2880 - 810) / (720 - 180))
    assert cs.d_prime_m == pytest.approx(810 - cs.cs_mps * 180)
    assert cs.trials == 2
    assert cs.note == ""


def test_critical_speed_flags_trials_outside_the_calibrated_window():
    cs = models.critical_speed([(400, 60), (2880, 720)])
    assert "2-20 minute" in cs.note


def test_critical_speed_flags_negative_d_prime():
    # Shorter trial implausibly slow -> negative intercept.
    cs = models.critical_speed([(600, 180), (2880, 720)])
    assert "Negative D'" in cs.note


def test_critical_speed_needs_two_different_durations():
    with pytest.raises(ModelError):
        models.critical_speed([(800, 180), (820, 180)])
    with pytest.raises(ModelError):
        models.critical_speed([(800, 180)])


def test_cs_zones_bracket_critical_speed():
    zones = models.cs_zones(4.0)  # 4:10/km
    slow, fast = zones["threshold"]
    assert fast == pytest.approx(250.0)  # top of threshold is CS itself
    assert slow > fast


def test_cs_prediction_uses_d_prime():
    cs = models.critical_speed([(810, 180), (2880, 720)])
    assert models.predict_from_cs(cs, 5_000) == pytest.approx((5_000 - cs.d_prime_m) / cs.cs_mps)


# --- grade adjustment -------------------------------------------------------


def test_flat_is_the_reference():
    assert models.gap_factor(0) == pytest.approx(1.0)


def test_uphill_costs_more_downhill_less():
    assert models.gap_factor(5) > 1.2
    assert models.gap_factor(-5) < 1.0


def test_steep_downhill_costs_more_again():
    # Minetti's curve bottoms out near -20% and turns back up beyond it.
    assert models.gap_factor(-20) < models.gap_factor(-10)
    assert models.gap_factor(-30) > models.gap_factor(-20)


# --- pace <-> power ---------------------------------------------------------


def test_power_pace_round_trip():
    watts = models.power_for_pace(240, cp_watts=300, pace_at_cp_spk=250)
    assert watts > 300  # faster than CP pace -> more than CP
    assert models.pace_for_power(watts, 300, 250) == pytest.approx(240)


def test_power_model_rejects_nonpositive():
    with pytest.raises(ModelError):
        models.power_for_pace(0, 300, 250)


# --- Riegel -----------------------------------------------------------------


def test_riegel_doubles_distance_at_slightly_slower_pace():
    assert models.riegel_time(5_000, 1200, 10_000) == pytest.approx(1200 * 2**1.06)


def test_threshold_prediction_gives_a_band_around_likely():
    fast, likely, slow = models.predict_from_threshold(250, 10_000)
    assert fast < likely < slow
    assert slow / fast == pytest.approx((1 + 0.03) / (1 - 0.03))
