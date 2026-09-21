"""Profile: zone models, aliases, coach context, and TOML round-trips."""

import datetime as dt

import pytest

from gpp.profile import Profile, ProfileError, default_save_path, find_profile


def test_cs_model_derives_zones_from_trials():
    profile = Profile.from_dict(
        {
            "pace": {"threshold": "4:00/km", "model": "cs"},
            "cs": {
                "trials": [
                    {"distance": "810m", "time": "3:00"},
                    {"distance": "2880m", "time": "12:00"},
                ]
            },
        }
    )
    assert profile.cs is not None
    slow, fast = profile.pace_zone("threshold")
    assert fast == pytest.approx(profile.cs.cs_pace)
    assert slow > fast


def test_cs_model_without_trials_is_an_error():
    with pytest.raises(ProfileError, match=r"\[cs\] trials"):
        Profile.from_dict({"pace": {"threshold": "4:00/km", "model": "cs"}})


def test_vdot_model_from_a_race():
    profile = Profile.from_dict(
        {
            "pace": {"threshold": "4:00/km", "model": "vdot"},
            "vdot": {"distance": "10k", "time": "40:00"},
        }
    )
    assert 51 < profile.vdot < 53
    _, easy_fast = profile.pace_zone("easy")
    thr_slow, _ = profile.pace_zone("threshold")
    assert easy_fast > thr_slow  # easy is slower than threshold


def test_daniels_letters_are_aliases():
    profile = Profile.from_dict({"pace": {"threshold": "4:00/km"}})
    assert profile.pace_zone("T") == profile.pace_zone("threshold")
    assert profile.pace_zone("e") == profile.pace_zone("easy")
    assert profile.canonical_zone("MP") == "marathon"


def test_unknown_model_rejected():
    with pytest.raises(ProfileError, match="pace model"):
        Profile.from_dict({"pace": {"threshold": "4:00/km", "model": "vibes"}})


def test_power_defaults_cp_pace_to_threshold():
    profile = Profile.from_dict({"pace": {"threshold": "4:00/km"}, "power": {"cp": 280}})
    assert profile.power_cp == 280
    assert profile.power_pace_at_cp == pytest.approx(240)


def test_availability_and_goal_race_parse():
    profile = Profile.from_dict(
        {
            "pace": {"threshold": "4:00/km"},
            "availability": {
                "days": ["Tue", "thursday", "sat"],
                "weekday_max_minutes": 45,
                "long_run_day": "Saturday",
            },
            "goal_race": {
                "name": "City 10k",
                "date": "2026-10-25",
                "distance": "10k",
                "goal_time": "42:00",
            },
        }
    )
    assert profile.availability.days == ["tue", "thu", "sat"]
    assert profile.availability.allows(dt.date(2026, 9, 22))  # a Tuesday
    assert not profile.availability.allows(dt.date(2026, 9, 21))  # a Monday
    assert profile.availability.max_minutes(dt.date(2026, 9, 22)) == 45
    assert profile.availability.long_run_day == "sat"
    assert profile.goal_race.date == dt.date(2026, 10, 25)
    assert "goal 42:00" in profile.goal_race.describe()


def test_bad_availability_day_rejected():
    with pytest.raises(ProfileError, match=r"mon..sun"):
        Profile.from_dict({"pace": {"threshold": "4:00/km"}, "availability": {"days": ["funday"]}})


def test_full_profile_round_trips_through_toml(tmp_path):
    data = {
        "name": "Pat",
        "pace": {"threshold": "4:00/km", "model": "cs"},
        "cs": {
            "trials": [{"distance": "810m", "time": "3:00"}, {"distance": "2880m", "time": "12:00"}]
        },
        "hr": {"lthr": 170},
        "power": {"cp": 300, "pace_at_cp": "4:05/km"},
        "athlete": {
            "instructions": "No track work, I hate it.",
            "injuries": ["left Achilles, spring 2026"],
            "constraints": ["no hills for 6 weeks"],
            "longest_recent_run_km": 18,
            "recent_weekly_km": 45,
            "language": "de",
        },
        "availability": {
            "days": ["tue", "thu", "sun"],
            "weekend_max_minutes": 120,
            "long_run_day": "sun",
        },
        "goal_race": {
            "name": "Half",
            "date": "2026-11-15",
            "distance": "half marathon",
            "priority": "A",
        },
        "location": {"latitude": 49.28, "longitude": -123.12},
        "ai": {"default": "paste", "providers": {"paste": {"kind": "manual"}}},
    }
    path = Profile.from_dict(data).save(tmp_path / "p.toml")
    loaded = Profile.load(path)

    assert loaded.zone_model == "cs" and loaded.cs is not None
    assert loaded.power_cp == 300
    assert loaded.instructions == "No track work, I hate it."
    assert loaded.injuries == ["left Achilles, spring 2026"]
    assert loaded.constraints == ["no hills for 6 weeks"]
    assert loaded.longest_recent_run_km == 18
    assert loaded.language == "de"
    assert loaded.availability.days == ["tue", "thu", "sun"]
    assert loaded.goal_race.name == "Half"
    assert loaded.latitude == pytest.approx(49.28)
    assert loaded.raw["ai"]["default"] == "paste"


def test_describe_mentions_the_coach_context():
    profile = Profile.from_dict(
        {
            "pace": {"threshold": "4:00/km"},
            "athlete": {"injuries": ["knee"], "instructions": "keep it fun"},
        }
    )
    text = profile.describe()
    assert "knee" in text and "keep it fun" in text


def test_named_profiles_have_their_own_home(monkeypatch, tmp_path):
    monkeypatch.setattr("gpp.profile.PROFILES_DIR", tmp_path / "profiles")
    assert find_profile("sam") is None
    path = default_save_path("sam")
    assert path == tmp_path / "profiles" / "sam.toml"
    Profile.from_dict({"name": "Sam", "pace": {"threshold": "5:00/km"}}).save(path)
    assert find_profile("sam") == path
