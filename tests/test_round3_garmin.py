"""Round 3 batch C: FIT files, reverse compile, load focus, receipts, race
pacing, the live dry run, conflicts, unpush by receipt, body status."""

import datetime as dt

import pytest

from gpp import decompile, fit, loadfocus, race, receipts
from gpp.client import GarminClient, PushResult
from gpp.compile import compile_plan
from gpp.history import History
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}})

QUALITY = {
    "name": "Threshold 5 x 1k",
    "date": "2026-09-24",
    "role": "quality",
    "notes": "Controlled",
    "steps": [
        {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
        {
            "kind": "repeat",
            "reps": 5,
            "steps": [
                {
                    "kind": "run",
                    "distance": "1km",
                    "target": {"type": "pace", "zone": "threshold"},
                    "note": "tall",
                },
                {
                    "kind": "recover",
                    "duration": "2m",
                    "target": {"type": "pace", "zone": "recovery"},
                },
            ],
        },
        {"kind": "run", "duration": "5m", "target": {"type": "hr", "zone": 2}},
        {"kind": "cooldown", "duration": "10m", "target": {"type": "none"}},
    ],
}
EASY = {
    "name": "Easy",
    "date": "2026-09-22",
    "steps": [{"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}],
}


def plan(*workouts, **extra):
    return Plan.from_dict({"plan": "Block", "workouts": list(workouts), **extra})


# --- FIT -------------------------------------------------------------------------


def test_fit_round_trip_keeps_structure_zones_and_notes():
    workout = plan(QUALITY).workouts[0]
    data = fit.encode_workout(workout, PROFILE, created=dt.datetime(2026, 9, 21, tzinfo=dt.UTC))
    assert data[8:12] == b".FIT"
    parsed = fit.decode(data)
    assert parsed["file_id"][0] == fit.FILE_TYPE_WORKOUT
    assert (
        parsed["workout"][8] == "Threshold 5 x 1k" and parsed["workout"][6] == 6
    )  # the repeat is a step
    back = fit.workout_from_fit(data, dt.date(2026, 9, 24), PROFILE)
    kinds = [s.kind for s in back.steps]
    assert kinds == ["warmup", "repeat", "run", "cooldown"]
    repeat = back.steps[1]
    assert repeat.reps == 5 and [c.kind for c in repeat.steps] == ["run", "recover"]
    assert repeat.steps[0].target.zone == "threshold" and repeat.steps[0].note == "tall"
    assert repeat.steps[0].distance == "1000m"
    assert back.steps[2].target.type == "hr" and back.steps[2].target.zone == 2


def test_fit_checksum_is_verified():
    data = bytearray(fit.encode_workout(plan(EASY).workouts[0], PROFILE))
    data[40] ^= 0xFF
    with pytest.raises(fit.FitError, match="checksum"):
        fit.decode(bytes(data))


def test_fit_explicit_targets_survive():
    workout = plan(
        {
            "name": "Mixed",
            "date": "2026-09-22",
            "steps": [
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "hr", "low": 140, "high": 150},
                },
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "cadence", "low": 170, "high": 180},
                },
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "power", "low": 250, "high": 270},
                },
                {
                    "kind": "run",
                    "duration": "10m",
                    "target": {"type": "pace", "slow": "5:00/km", "fast": "4:50/km"},
                },
            ],
        }
    ).workouts[0]
    back = fit.workout_from_fit(fit.encode_workout(workout, PROFILE), dt.date(2026, 9, 22), PROFILE)
    targets = [s.target.to_dict() for s in back.steps]
    assert targets[0] == {"type": "hr", "low": 140, "high": 150}
    assert targets[1] == {"type": "cadence", "low": 170, "high": 180}
    assert targets[2] == {"type": "power", "low": 250, "high": 270}
    assert targets[3] == {"type": "pace", "slow": "5:00/km", "fast": "4:50/km"}


# --- reverse compile ----------------------------------------------------------------


def test_decompile_inverts_the_compiler():
    compiled = compile_plan(plan(QUALITY), PROFILE)[0]
    back = decompile.workout_from_garmin(compiled.payload, dt.date(2026, 9, 24), PROFILE)
    original = plan(QUALITY).workouts[0]
    assert back.name == original.name and back.notes == "Controlled"
    assert [s.to_dict() for s in back.steps] == [s.to_dict() for s in original.steps]


def test_plan_from_calendar_uses_the_fetcher_and_reports_skips():
    compiled = compile_plan(plan(QUALITY, EASY), PROFILE)
    payloads = {11: compiled[0].payload, 12: compiled[1].payload}
    items = [
        {"workoutId": 11, "date": "2026-09-24", "title": "Threshold 5 x 1k"},
        {"workoutId": 12, "date": "2026-09-22", "title": "Easy"},
        {"id": 99, "date": "2026-09-23", "title": "Race day", "itemType": "event"},
    ]
    result, skipped = decompile.plan_from_calendar(items, lambda i: payloads[i], PROFILE)
    assert [w.name for w in result.workouts] == ["Easy", "Threshold 5 x 1k"]
    assert skipped and "Race day" in skipped[0]


# --- load focus ---------------------------------------------------------------------


def test_load_focus_buckets_the_plan_like_the_watch():
    hard = {
        "name": "Reps",
        "date": "2026-09-26",
        "steps": [
            {
                "kind": "repeat",
                "reps": 8,
                "steps": [
                    {
                        "kind": "run",
                        "duration": "1m",
                        "target": {"type": "pace", "zone": "interval"},
                    },
                    {"kind": "recover", "duration": "1m"},
                ],
            }
        ],
    }
    focus = loadfocus.load_focus(plan(EASY, QUALITY, hard), PROFILE)
    assert set(focus["minutes"]) == set(loadfocus.BUCKETS)
    assert abs(sum(focus["shares"].values()) - 1.0) < 0.01
    assert focus["minutes"]["anaerobic"] == 8 and focus["minutes"]["high_aerobic"] > 0
    assert "balanced" in focus["verdict"] or "anaerobic" in focus["verdict"]
    assert "Load focus" in loadfocus.describe(focus)


# --- receipts ---------------------------------------------------------------------------


def test_receipts_round_trip_and_list_removable_ids(tmp_path):
    results = [
        PushResult("Easy", "2026-09-22", "created", 101),
        PushResult("Q", "2026-09-24", "unchanged", 102),
        PushResult("Bad", "2026-09-25", "failed", None, "boom"),
    ]
    path = receipts.save_receipt(
        "Autumn block", results, root=tmp_path, now=dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.UTC)
    )
    assert path.name == "20260921T080000-autumn-block.json"
    listed = receipts.list_receipts(root=tmp_path)
    assert len(listed) == 1 and listed[0].plan == "Autumn block" and listed[0].ids == [101, 102]
    assert "2 created" not in listed[0].describe() and "1 created" in listed[0].describe()


# --- race pacing --------------------------------------------------------------------------


def test_goal_pace_and_splits():
    assert race.race_metres("half marathon") == 21097.5
    assert race.race_metres("15km") == 15000
    assert abs(race.goal_pace("3:30:00", "marathon") - 298.6) < 0.2
    rows = race.splits("10k", "45:00", "even")
    assert len(rows) == 10 and rows[-1]["elapsed"] == "45:00" and rows[0]["pace"] == "4:30/km"
    negative = race.splits("10k", "45:00", "negative")
    assert negative[0]["pace_spk"] > negative[-1]["pace_spk"]
    with pytest.raises(race.RaceError):
        race.splits("10k", "45:00", "sprint-then-die")


def test_race_workout_pushes_like_any_other():
    goal = {
        "name": "City marathon",
        "date": "2026-10-25",
        "distance": "marathon",
        "goal_time": "3:30:00",
    }
    workout = race.race_workout(goal, PROFILE, "10-10-10")
    assert workout.role == "race" and len(workout.steps) == 3
    assert sum(float(s.distance.rstrip("m")) for s in workout.steps) == pytest.approx(42195, abs=2)
    compiled = compile_plan(Plan(plan="R", workouts=[workout]), PROFILE)[0]
    steps = compiled.payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["targetValueOne"] < steps[2]["targetValueOne"]  # slower start, faster finish


# --- live dry run, conflicts, unpush by id ---------------------------------------------------


class FakeGarmin:
    def __init__(self, items):
        self.calendar = list(items)
        self.deleted = []
        self.writes = 0

    def __call__(self, method, path, **kw):
        if path.startswith("/calendar-service/"):
            return {"calendarItems": list(self.calendar)}
        if method == "DELETE":
            self.deleted.append(int(path.rsplit("/", 1)[1]))
            return {}
        self.writes += 1
        raise AssertionError(f"unexpected write {method} {path}")


def connected(fake):
    client = GarminClient("me@example.com")
    client._request = fake
    return client


def test_preview_classifies_without_writing_and_conflicts_lists_the_rest():
    compiled = compile_plan(plan(QUALITY, EASY), PROFILE)
    stale_tag = compiled[0].tag[:-9] + "deadbeef]"
    fake = FakeGarmin(
        [
            {
                "workoutId": 1,
                "date": "2026-09-24",
                "title": "Threshold 5 x 1k",
                "description": stale_tag,
            },
            {"workoutId": 2, "date": "2026-09-22", "title": "Club run", "description": "hand made"},
            {"workoutId": 3, "date": "2026-09-22", "title": "Coach", "trainingPlanId": 7},
        ]
    )
    client = connected(fake)
    preview = {r.name: r for r in client.preview(compiled)}
    assert (
        preview["Threshold 5 x 1k"].action == "would-update"
        and preview["Threshold 5 x 1k"].workout_id == 1
    )
    assert preview["Easy"].action == "would-create"
    others = client.conflicts(compiled)
    assert [(o["title"], o["source"]) for o in others] == [
        ("Club run", "hand-made or synced"),
        ("Coach", "a Garmin training plan"),
    ]
    assert fake.writes == 0 and fake.deleted == []
    removed = client.unpush_ids([1, 2])
    assert [r.action for r in removed] == ["removed", "removed"] and fake.deleted == [1, 2]


# --- body status -----------------------------------------------------------------------------


def test_status_log_feeds_recent_status():
    store = History(":memory:")
    store.log_status("2026-09-20", "illness", "cold")
    store.log_status("2026-09-10", "injury", "calf")
    assert [r["kind"] for r in store.status_log()] == ["injury", "illness"]
    recent = store.recent_status(dt.date(2026, 9, 21))
    assert len(recent) == 1 and recent[0]["note"] == "cold"
    with pytest.raises(ValueError):
        store.log_status("2026-09-20", "mood")


def test_garmin_hr_zones_become_fractions_of_lthr():
    zones = [(100, 130), (131, 145), (146, 158), (159, 170), (171, 190)]
    updated = Profile.from_dict(
        {"name": "T", "pace": {"threshold": "4:00/km"}}
    ).with_garmin_hr_zones(zones)
    assert updated.lthr == 170
    assert updated.hr_zone(4) == (159, 170) and updated.hr_zone(1) == (100, 130)
    assert updated.raw["hr"]["zones"]["5"][1] == pytest.approx(190 / 170, abs=0.001)
