"""The one-line workout parser."""

import pytest

from gpp.oneline import OneLineError, parse_extent, parse_target, parse_workout


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20min", {"duration": "20m"}),
        ("20 min", {"duration": "20m"}),
        ("3m", {"duration": "3m"}),
        ("800m", {"distance": "800m"}),
        ("1km", {"distance": "1km"}),
        ("5k", {"distance": "5km"}),
        ("30s", {"duration": "30s"}),
        ("1h", {"duration": "60m"}),
        ("lap", {"until": "lap"}),
    ],
)
def test_extents(text, expected):
    assert parse_extent(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("threshold", {"type": "pace", "zone": "threshold"}),
        ("T", {"type": "pace", "zone": "t"}),
        ("10k pace", {"type": "pace", "zone": "threshold"}),
        ("5k", {"type": "pace", "zone": "interval"}),
        ("MP", {"type": "pace", "zone": "marathon"}),
        ("4:00/km", {"type": "pace", "slow": "4:00/km", "fast": "4:00/km"}),
        ("4:10-4:00/km", {"type": "pace", "slow": "4:10/km", "fast": "4:00/km"}),
        ("4:00-4:10", {"type": "pace", "slow": "4:10/km", "fast": "4:00/km"}),
        ("Z2", {"type": "hr", "zone": 2}),
        ("280w", {"type": "power", "low": 280, "high": 290}),
        ("270-290w", {"type": "power", "low": 270, "high": 290}),
        ("RPE 7", {"type": "rpe", "value": 7}),
        ("none", {"type": "none"}),
    ],
)
def test_targets(text, expected):
    assert parse_target(text) == expected


def test_the_trainingpeaks_example():
    w = parse_workout("20min warmup, 6x3m @ threshold w/ 2min recovery, 10 min cooldown")
    kinds = [s.kind for s in w.steps]
    assert kinds == ["warmup", "repeat", "cooldown"]
    rep = w.steps[1]
    assert rep.reps == 6
    assert rep.steps[0].kind == "run" and rep.steps[0].duration == "3m"
    assert rep.steps[0].target.zone == "threshold"
    assert rep.steps[1].kind == "recover" and rep.steps[1].duration == "2m"
    assert w.steps[0].target.zone == "easy"  # warm-ups default to easy


def test_distance_reps_and_race_paces():
    w = parse_workout("2km easy, 5x1km @ 10k pace w/ 90s jog, 4x100m strides w/ lap, 10min cd")
    assert w.steps[0].kind == "run" and w.steps[0].target.zone == "easy"
    assert w.steps[1].steps[0].distance == "1km" and w.steps[1].steps[0].target.zone == "threshold"
    assert w.steps[2].steps[0].kind == "stride"
    assert w.steps[2].steps[1].until == "lap"
    assert w.steps[3].kind == "cooldown"


def test_semicolons_and_then_also_separate():
    w = parse_workout("10m wu; 20m @ MP then 5m cd")
    assert [s.kind for s in w.steps] == ["warmup", "run", "cooldown"]
    assert w.steps[1].target.zone == "marathon"


def test_explicit_names_and_dates():
    w = parse_workout("30min easy", name="Recovery jog", date="2026-09-22")
    assert w.name == "Recovery jog"
    assert w.date.isoformat() == "2026-09-22"


def test_bad_input_is_an_error_not_a_traceback():
    with pytest.raises(OneLineError):
        parse_workout("banana @ purple")
    with pytest.raises(OneLineError):
        parse_workout("")
