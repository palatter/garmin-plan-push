import tomllib

import pytest

from gpp.profile import Profile, ProfileError


def test_round_trips_through_a_file(tmp_path):
    original = Profile.from_dict(
        {
            "name": "Patrick",
            "units": "metric",
            "pace": {"threshold": "4:30/km"},
            "hr": {"lthr": 170, "max": 188},
        }
    )
    path = original.save(tmp_path / "profile.toml")
    loaded = Profile.load(path)

    assert loaded.name == "Patrick"
    assert loaded.lthr == 170
    assert loaded.hr_max == 188
    assert loaded.threshold_pace == pytest.approx(original.threshold_pace, abs=0.5)


@pytest.mark.parametrize(
    "name",
    [
        "Pat\nrick",       # a pasted value with a newline
        'Pat "The Bus"',   # quotes
        "Pat\\rick",       # a backslash
        "Pat\trick",       # a tab
        "Pat\x07rick",     # a stray control character
    ],
)
def test_awkward_names_still_produce_loadable_toml(tmp_path, name):
    """A name that breaks the file would lock the user out of their config."""
    profile = Profile.from_dict({"name": name, "pace": {"threshold": "4:30/km"}})
    path = profile.save(tmp_path / "profile.toml")

    tomllib.loads(path.read_text(encoding="utf-8"))  # must parse
    assert Profile.load(path).name == name           # and survive intact


def test_malformed_toml_raises_profile_error_not_a_decode_error(tmp_path):
    """The file invites hand-editing, so a typo must be recoverable."""
    path = tmp_path / "profile.toml"
    path.write_text('name = "unclosed\n[pace]\n', encoding="utf-8")
    with pytest.raises(ProfileError, match="not valid TOML"):
        Profile.load(path)


def test_missing_file_raises_profile_error(tmp_path):
    with pytest.raises(ProfileError, match="no profile at"):
        Profile.load(tmp_path / "nope.toml")


def test_saving_preserves_the_ai_block(tmp_path):
    """Editing paces must not silently drop the user's provider config."""
    profile = Profile.from_dict(
        {
            "name": "P",
            "pace": {"threshold": "4:30/km"},
            "ai": {
                "default": "local",
                "providers": {
                    "local": {"kind": "ollama", "model": "llama3.3", "max_tokens": 8000},
                    "paste": {"kind": "manual", "strict_schema": False},
                },
            },
        }
    )
    path = profile.save(tmp_path / "profile.toml")
    reloaded = Profile.load(path)

    ai = reloaded.raw["ai"]
    assert ai["default"] == "local"
    assert ai["providers"]["local"]["model"] == "llama3.3"
    assert ai["providers"]["local"]["max_tokens"] == 8000
    assert ai["providers"]["paste"]["strict_schema"] is False


def test_custom_zones_survive_but_defaults_are_not_written(tmp_path):
    profile = Profile.from_dict(
        {
            "name": "P",
            "pace": {"threshold": "4:30/km", "zones": {"easy": [1.30, 1.18]}},
        }
    )
    path = profile.save(tmp_path / "profile.toml")
    text = path.read_text(encoding="utf-8")

    assert "[pace.zones]" in text
    assert "easy" in text
    assert "repetition" not in text  # untouched defaults stay out of the file
    assert Profile.load(path).pace_zones["easy"] == (1.30, 1.18)


def test_imperial_profile_round_trips(tmp_path):
    profile = Profile.from_dict(
        {"name": "P", "units": "imperial", "pace": {"threshold": "7:15/mi"}}
    )
    path = profile.save(tmp_path / "profile.toml")
    loaded = Profile.load(path)
    assert loaded.imperial
    assert loaded.threshold_pace == pytest.approx(profile.threshold_pace, abs=0.5)
