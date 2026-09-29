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
        ("20min", 1200),
        ("20 mins", 1200),
        ("1hr", 3600),
        ("2hrs", 7200),
        ("30sec", 30),
        ("90 secs", 90),
        ("1h30min", 5400),
        ("1 hr 5 min", 3900),
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


def test_watch_reports_a_bad_duration_and_keeps_going(tmp_path, monkeypatch, capsys):
    import json

    from gpp import cli
    from gpp.profile import Profile

    profile = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
    profile_path = profile.save(tmp_path / "profile.toml")
    plan = tmp_path / "plan.json"
    workout = {"name": "W", "date": "2026-10-01", "steps": [{"kind": "run", "duration": "20 fur"}]}
    plan.write_text(json.dumps({"plan": "p", "workouts": [workout]}), encoding="utf-8")
    monkeypatch.setattr("gpp.watch.watch", lambda path, on_change: on_change(path))
    assert cli.main(["--profile", str(profile_path), "watch", str(plan)]) == 0
    assert "error: cannot parse duration '20 fur'" in capsys.readouterr().out
