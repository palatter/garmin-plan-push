import pytest

from gpp.estimate import EstimateError, lthr_from_max, threshold_from_race
from gpp.units import format_pace


def test_hour_race_returns_its_own_pace():
    """A race that already took an hour IS threshold pace, by definition."""
    result = threshold_from_race("15k", "1:00:00")
    assert result.threshold_pace == pytest.approx(3600 / 15, rel=1e-6)
    assert result.hour_distance_metres == pytest.approx(15000, rel=1e-6)


def test_faster_race_gives_faster_threshold():
    quick = threshold_from_race("10k", "38:00").threshold_pace
    slower = threshold_from_race("10k", "52:00").threshold_pace
    assert quick < slower


def test_shorter_race_implies_slower_threshold_than_the_race_itself():
    """A 5k is run faster than threshold, so threshold must be slower."""
    result = threshold_from_race("5k", "20:00")
    race_pace = 1200 / 5
    assert result.threshold_pace > race_pace


def test_marathon_implies_faster_threshold_than_race_pace():
    """A marathon is run slower than threshold."""
    result = threshold_from_race("marathon", "3:30:00")
    race_pace = 12600 / 42.195
    assert result.threshold_pace < race_pace


def test_realistic_10k_lands_in_a_sane_band():
    result = threshold_from_race("10k", "47:30")
    assert 280 <= result.threshold_pace <= 300  # ~4:40-5:00/km
    assert format_pace(result.threshold_pace).endswith("/km")


def test_named_and_numeric_distances_agree():
    a = threshold_from_race("10k", "40:00").threshold_pace
    b = threshold_from_race("10000m", "40:00").threshold_pace
    assert a == pytest.approx(b)


def test_accepts_miles():
    result = threshold_from_race("half marathon", "1:35:00")
    assert result.reliable


def test_flags_unreliable_outside_calibrated_range():
    assert not threshold_from_race("800m", "2:30").reliable
    assert threshold_from_race("5k", "22:00").reliable


def test_rejects_swapped_distance_and_time():
    """A 'marathon in 20 minutes' should not silently produce a number."""
    with pytest.raises(EstimateError, match="world record"):
        threshold_from_race("marathon", "20:00")


def test_rejects_absurdly_slow():
    with pytest.raises(EstimateError, match="typo"):
        threshold_from_race("5k", "20:00:00")


def test_rejects_unparseable_input():
    with pytest.raises(EstimateError):
        threshold_from_race("a jog", "ages")


def test_lthr_from_max():
    assert lthr_from_max(190) == 167


def test_lthr_rejects_implausible_max():
    with pytest.raises(EstimateError):
        lthr_from_max(400)
