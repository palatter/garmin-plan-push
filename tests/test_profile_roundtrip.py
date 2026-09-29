"""Saving a profile keeps everything in it and always writes valid TOML.

Every save goes through Profile.to_toml: `gpp profile set`, `reestimate
--apply`, the Garmin HR-zone sync and the web settings. It used to drop
[defaults] and any key it did not know, and to write names such as
"my easy" or "gpt-4.1" unquoted, which left a file gpp could not read.
"""

import datetime as dt
import os
import stat
import tomllib

from hypothesis import given
from hypothesis import strategies as st

from gpp import cli
from gpp.profile import Profile, _kv

FULL = """\
name = "Åsa \\"Quotes\\" Runner"
units = "metric"
nickname = "Ace"

[pace]
threshold = "4:05/km"
model = "threshold"
surface = "trail"

[pace.zones]
"my easy" = [1.3, 1.2]
"lätt" = [1.35, 1.25]

[hr]
lthr = 168
max = 190
resting = 48

[hr.zones]
"1" = [0.7, 0.8]

[cs]
trials = [{ distance = "1200m", time = "4:00" }, { distance = "3600m", time = "13:30" }]

[power]
cp = 280
pace_at_cp = "4:00/km"
stryd = true

[athlete]
instructions = "No track on Mondays.\\nKeep it fun."
injuries = ["left achilles"]
recent_weekly_km = 45
age = 41

[availability]
days = ["tue", "thu", "sat", "sun"]
long_run_day = "sun"
sessions_per_week = 4

[goal_race]
name = "City half"
date = 2026-11-22
distance = "half marathon"
priority = "A"
goal_time = "1:40:00"
bib = 1234

[location]
latitude = 49.28
longitude = -123.12
city = "Vancouver"

[defaults]
device = 3456789012
port = 8765
attempts = 3

[strava]
sync = false
since = 2026-01-01
clubs = ["a", "b"]
limits = { daily = 5, weekly = 20 }

[ai]
default = "gpt-4.1"
timeout = 90

[ai.providers.claude]
kind = "anthropic"
model = "claude-opus-5-5"

[ai.providers."gpt-4.1"]
kind = "openai"
model = "gpt-4.1"
stop = ["END", "STOP"]
headers = { X-Team = "me" }
"""


def _saved(tmp_path, text: str = FULL):
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    Profile.load(path).save(path)
    return path, tomllib.loads(path.read_text(encoding="utf-8"))


def test_every_table_and_unknown_key_survives_a_save(tmp_path):
    before = tomllib.loads(FULL)
    path, after = _saved(tmp_path)
    assert after["defaults"] == {"device": 3456789012, "port": 8765, "attempts": 3}
    assert after["strava"] == before["strava"]
    assert after["nickname"] == "Ace"
    assert after["pace"]["surface"] == "trail"
    assert after["pace"]["zones"]["my easy"] == [1.3, 1.2]
    assert after["pace"]["zones"]["lätt"] == [1.35, 1.25]
    assert after["hr"]["resting"] == 48
    assert after["power"]["stryd"] is True
    assert after["athlete"]["age"] == 41
    assert after["athlete"]["instructions"] == "No track on Mondays.\nKeep it fun."
    assert after["goal_race"]["bib"] == 1234
    assert after["location"]["city"] == "Vancouver"
    assert after["cs"]["trials"] == before["cs"]["trials"]
    assert after["ai"] == before["ai"]
    assert after["name"] == 'Åsa "Quotes" Runner'
    # And gpp reads its own output.
    profile = Profile.load(path)
    assert profile.lthr == 168 and profile.goal_race.date == dt.date(2026, 11, 22)


def test_a_second_save_changes_nothing(tmp_path):
    path, _ = _saved(tmp_path)
    first = path.read_text(encoding="utf-8")
    Profile.load(path).save(path)
    assert path.read_text(encoding="utf-8") == first


def test_unknown_keys_keep_a_table_that_gpp_would_otherwise_leave_out(tmp_path):
    text = 'name = "T"\n\n[pace]\nthreshold = "4:00/km"\n\n[hr]\nresting = 50\n'
    _, after = _saved(tmp_path, text)
    assert after["hr"] == {"resting": 50}


def test_profile_set_keeps_defaults_and_odd_zone_names(tmp_path, capsys):
    path = tmp_path / "profile.toml"
    path.write_text(FULL, encoding="utf-8")
    assert cli.main(["--profile", str(path), "profile", "set", "pace.threshold", "4:10/km"]) == 0
    assert cli.main(["--profile", str(path), "profile", "set", "defaults.port", "9000"]) == 0
    capsys.readouterr()
    assert cli.main(["--profile", str(path), "profile", "get", "defaults.device"]) == 0
    assert "3456789012" in capsys.readouterr().out
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved["defaults"]["port"] == 9000
    assert saved["pace"]["threshold"] == "4:10/km"
    assert "my easy" in saved["pace"]["zones"]


def test_save_keeps_the_file_mode_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(FULL, encoding="utf-8")
    path.chmod(0o600)
    Profile.load(path).save(path)
    if os.name != "nt":  # Windows' chmod sets only the read-only bit
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["profile.toml"]


_scalars = (
    st.text()
    | st.integers(min_value=-(2**63), max_value=2**63 - 1)
    | st.floats(allow_nan=False)
    | st.booleans()
    | st.dates()
)
_values = st.recursive(
    _scalars,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=10,
)


@given(key=st.text(), value=_values)
def test_any_key_and_value_round_trip_through_toml(key, value):
    assert tomllib.loads(_kv(key, value)) == {key: value}
