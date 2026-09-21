import pytest

from gpp.plan import Plan
from gpp.profile import Profile
from gpp.timeline import (
    MAX_BLOCKS,
    OPEN_ENDED_NOMINAL_SECONDS,
    ZONE_INTENSITY,
    workout_summary,
    workout_timeline,
)

PROFILE = Profile.from_dict(
    {"pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}}
)


def build(steps):
    plan = Plan.from_dict(
        {
            "plan": "p",
            "workouts": [{"name": "W", "date": "2026-09-24", "steps": steps}],
        }
    )
    return plan.workouts[0]


def test_repeats_are_expanded_into_real_blocks():
    workout = build(
        [
            {"kind": "warmup", "duration": "10m"},
            {
                "kind": "repeat",
                "reps": 3,
                "steps": [
                    {"kind": "run", "duration": "2m"},
                    {"kind": "recover", "duration": "1m"},
                ],
            },
        ]
    )
    blocks = workout_timeline(workout, PROFILE)
    assert len(blocks) == 1 + 3 * 2
    assert [b["rep"] for b in blocks] == [None, 1, 1, 2, 2, 3, 3]
    assert all(b["of"] == 3 for b in blocks[1:])


def test_intensity_tracks_the_zone():
    workout = build(
        [
            {"kind": "run", "duration": "5m", "target": {"type": "pace", "zone": "recovery"}},
            {"kind": "run", "duration": "5m", "target": {"type": "pace", "zone": "threshold"}},
            {"kind": "run", "duration": "5m", "target": {"type": "pace", "zone": "repetition"}},
        ]
    )
    intensities = [b["intensity"] for b in workout_timeline(workout, PROFILE)]
    assert intensities == sorted(intensities)
    assert intensities[0] == ZONE_INTENSITY["recovery"]
    assert intensities[-1] == ZONE_INTENSITY["repetition"]


def test_explicit_pace_is_placed_against_the_athletes_zones():
    """A raw pace should colour like the zone it actually falls in."""
    workout = build(
        [
            {
                "kind": "run",
                "duration": "10m",
                "target": {"type": "pace", "slow": "4:05/km", "fast": "3:58/km"},
            }
        ]
    )
    block = workout_timeline(workout, PROFILE)[0]
    assert block["intensity"] == ZONE_INTENSITY["threshold"]


def test_lap_button_step_is_flagged_not_hidden():
    workout = build([{"kind": "recover", "until": "lap"}])
    block = workout_timeline(workout, PROFILE)[0]
    assert block["open_ended"] is True
    assert block["seconds"] == OPEN_ENDED_NOMINAL_SECONDS
    assert workout_summary(workout, PROFILE)["open_ended"] is True


def test_summary_counts_hard_time_only():
    workout = build(
        [
            {"kind": "warmup", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "threshold"}},
        ]
    )
    summary = workout_summary(workout, PROFILE)
    assert summary["seconds"] == pytest.approx(1800)
    assert summary["hard_seconds"] == pytest.approx(1200)


def test_distance_totals_include_repeats():
    workout = build(
        [
            {
                "kind": "repeat",
                "reps": 4,
                "steps": [{"kind": "run", "distance": "1km"}],
            }
        ]
    )
    assert workout_summary(workout, PROFILE)["metres"] == pytest.approx(4000)


def test_time_based_steps_contribute_estimated_distance():
    workout = build(
        [{"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "threshold"}}]
    )
    metres = workout_summary(workout, PROFILE)["metres"]
    # 40 min around 4:00/km is roughly 10 km.
    assert 9000 < metres < 11000


def test_hr_zone_targets_still_get_an_intensity():
    workout = build(
        [{"kind": "run", "duration": "10m", "target": {"type": "hr", "zone": 5}}]
    )
    block = workout_timeline(workout, PROFILE)[0]
    assert 0 < block["intensity"] <= 1.0


def test_totals_stay_consistent_when_the_drawn_blocks_are_capped():
    """Regression: summary totals must cover the whole session, not the
    truncated block list, or time and distance contradict each other."""
    groups = 6                      # 6 x 50 x 2 = 600 blocks, past the 400 cap
    reps = 50
    workout = build(
        [
            {
                "kind": "repeat",
                "reps": reps,
                "steps": [
                    {"kind": "run", "distance": "400m",
                     "target": {"type": "pace", "zone": "interval"}},
                    {"kind": "recover", "duration": "60s"},
                ],
            }
        ]
        * groups
    )
    blocks = workout_timeline(workout, PROFILE)
    summary = workout_summary(workout, PROFILE)

    total_reps = groups * reps
    assert len(blocks) == MAX_BLOCKS    # drawing is capped
    assert summary["truncated"] is True

    # ...but the numbers printed beside the chart describe the whole session.
    # Distance covers every rep, plus ground covered on the jogged recoveries.
    assert summary["metres"] > total_reps * 400
    assert summary["seconds"] > sum(b["seconds"] for b in blocks)
    # The recoveries alone outlast everything the capped list could hold.
    assert summary["seconds"] > total_reps * 60


def test_untruncated_workouts_are_not_flagged():
    workout = build([{"kind": "run", "duration": "30m"}])
    assert workout_summary(workout, PROFILE)["truncated"] is False


def test_block_carries_display_strings():
    workout = build(
        [{"kind": "run", "distance": "1km", "target": {"type": "pace", "zone": "threshold"}}]
    )
    block = workout_timeline(workout, PROFILE)[0]
    assert block["extent"] == "1.00 km"
    assert "threshold" in block["target"]
