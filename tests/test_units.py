import pytest

from gpp.units import (
    UnitError,
    format_duration,
    format_pace,
    mps_to_pace,
    pace_to_mps,
    parse_distance,
    parse_duration,
    parse_pace,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90s", 90),
        ("15m", 900),
        ("1h", 3600),
        ("1h30m", 5400),
        ("1h05m30s", 3930),
        ("45:00", 2700),
        ("1:05:00", 3900),
        ("2:30", 150),
        (300, 300),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_rejects_junk():
    with pytest.raises(UnitError):
        parse_duration("15m banana")
    with pytest.raises(UnitError):
        parse_duration("")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1km", 1000),
        ("800m", 800),
        ("400", 400),
        ("5mi", 8046.72),
        ("3.1 miles", 4988.9664),
    ],
)
def test_parse_distance(text, expected):
    assert parse_distance(text) == pytest.approx(expected)


def test_parse_distance_rejects_junk():
    with pytest.raises(UnitError):
        parse_distance("a bit")


def test_parse_pace_metric():
    assert parse_pace("4:00/km") == 240
    assert parse_pace("4:30") == 270  # default unit km


def test_parse_pace_imperial_converts_to_per_km():
    # 8:00/mi is faster per km than 8:00/km
    assert parse_pace("8:00/mi") == pytest.approx(480 / 1.609344)


def test_parse_pace_rejects_junk():
    with pytest.raises(UnitError):
        parse_pace("fast")


def test_pace_mps_roundtrip():
    assert pace_to_mps(250) == pytest.approx(4.0)
    assert mps_to_pace(4.0) == pytest.approx(250)


def test_faster_pace_is_larger_mps():
    """The invariant the Garmin compiler depends on."""
    fast = pace_to_mps(parse_pace("3:30/km"))
    slow = pace_to_mps(parse_pace("6:00/km"))
    assert fast > slow


def test_formatting():
    assert format_duration(90) == "1:30"
    assert format_duration(3930) == "1:05:30"
    assert format_pace(250) == "4:10/km"
    assert format_pace(250, imperial=True) == "6:42/mi"
