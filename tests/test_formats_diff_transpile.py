"""Import/export formats, share bundles, plan diffs, and pace<->power."""

import datetime as dt

import pytest

from gpp import diff, formats, transpile
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict(
    {
        "name": "Pat",
        "pace": {"threshold": "4:00/km"},
        "hr": {"lthr": 170},
        "power": {"cp": 300, "pace_at_cp": "4:00/km"},
    }
)

PLAN = Plan.from_dict(
    {
        "plan": "Block",
        "race_date": "2026-10-25",
        "workouts": [
            {
                "name": "Threshold 4x8",
                "date": "2026-09-22",
                "notes": "Even effort.",
                "steps": [
                    {
                        "kind": "warmup",
                        "duration": "15m",
                        "target": {"type": "pace", "zone": "easy"},
                    },
                    {
                        "kind": "repeat",
                        "reps": 4,
                        "steps": [
                            {
                                "kind": "run",
                                "duration": "8m",
                                "target": {"type": "pace", "zone": "threshold"},
                                "note": "relax",
                            },
                            {
                                "kind": "recover",
                                "duration": "2m",
                                "target": {"type": "pace", "zone": "recovery"},
                            },
                        ],
                    },
                    {
                        "kind": "cooldown",
                        "duration": "10m",
                        "target": {"type": "pace", "zone": "easy"},
                    },
                ],
            }
        ],
    }
)
W = PLAN.workouts[0]


# --- intervals.icu ----------------------------------------------------------


def test_icu_export_round_trips():
    text = formats.export_icu(W, PROFILE)
    assert "Warmup" in text and "4x" in text and "Cooldown" in text
    back = formats.import_icu(text, dt.date(2026, 9, 22))
    assert [s.kind for s in back.steps] == ["warmup", "repeat", "cooldown"]
    assert back.steps[1].reps == 4
    assert back.steps[1].steps[0].target.type == "pace"
    assert back.steps[1].steps[0].note == "relax"


def test_icu_import_reads_percent_and_hr_zones():
    text = "Tempo\n\nWarmup\n- 10m 65%\n3x\n  - 8m 100%\n  - 2m Z2\nCooldown\n- 10m 60%\n"
    w = formats.import_icu(text, dt.date(2026, 9, 22))
    assert w.name == "Tempo"
    assert w.steps[0].kind == "warmup" and w.steps[0].target.zone == "recovery"
    assert w.steps[1].steps[0].target.zone == "threshold"
    assert w.steps[1].steps[1].kind == "recover"


def test_icu_import_rejects_garbage():
    with pytest.raises(formats.FormatError):
        formats.import_icu("- ??? bananas\n", dt.date.today())


# --- ZWO --------------------------------------------------------------------


def test_zwo_export_and_import():
    xml = formats.export_zwo(W, PROFILE)
    assert "<sportType>run</sportType>" in xml
    assert 'Repeat="4"' in xml and "IntervalsT" in xml
    back = formats.import_zwo(xml, dt.date(2026, 9, 22))
    assert back.name == "Threshold 4x8"
    assert [s.kind for s in back.steps] == ["warmup", "repeat", "cooldown"]
    assert back.steps[1].steps[0].target.zone == "threshold"


def test_zwo_power_fraction_tracks_pace():
    xml = formats.export_zwo(W, PROFILE)
    # threshold steps sit near 1.0, easy warm-up well below
    assert 'OnPower="0.9' in xml or 'OnPower="1.0' in xml
    assert 'Power="0.8' in xml


def test_zwo_import_rejects_non_xml():
    with pytest.raises(formats.FormatError):
        formats.import_zwo("not xml", dt.date.today())


# --- MRC / ERG --------------------------------------------------------------


def test_mrc_table_is_monotonic_in_time():
    text = formats.export_mrc(W, PROFILE)
    rows = [ln.split("\t") for ln in text.splitlines() if "\t" in ln]
    minutes = [float(r[0]) for r in rows]
    assert minutes == sorted(minutes)
    assert minutes[-1] == pytest.approx(65.0)  # 15 + 4*(8+2) + 10
    assert "MINUTES PERCENT" in text


def test_erg_needs_cp():
    no_cp = Profile.from_dict({"pace": {"threshold": "4:00/km"}})
    with pytest.raises(formats.FormatError):
        formats.export_mrc(W, no_cp, erg=True)
    assert "MINUTES WATTS" in formats.export_mrc(W, PROFILE, erg=True)


# --- share ------------------------------------------------------------------


def test_share_round_trip_and_rebase():
    text = formats.export_share(PLAN, PROFILE, note="try this")
    bundle = formats.import_share(text)
    assert bundle.shared_by == "Pat" and bundle.note == "try this"
    assert bundle.plan.to_dict() == PLAN.to_dict()
    moved = formats.import_share(text, start_monday=dt.date(2026, 11, 2))
    assert moved.plan.workouts[0].date == dt.date(2026, 11, 3)


def test_share_rejects_other_json():
    with pytest.raises(formats.FormatError):
        formats.import_share('{"hello": 1}')


def test_export_dispatch():
    assert formats.export_workout(W, PROFILE, "icu").startswith("Threshold")
    with pytest.raises(formats.FormatError):
        formats.export_workout(W, PROFILE, "fit")


# --- diff -------------------------------------------------------------------


def test_diff_reports_added_removed_and_changed():
    after = Plan.from_dict(PLAN.to_dict())
    after.workouts[0].steps[1].reps = 5
    after.workouts[0].notes = "Harder."
    after.workouts.append(
        Plan.from_dict(
            {
                "plan": "x",
                "workouts": [
                    {
                        "name": "Easy",
                        "date": "2026-09-24",
                        "steps": [{"kind": "run", "duration": "30m"}],
                    }
                ],
            }
        ).workouts[0]
    )
    after.race_date = dt.date(2026, 11, 1)
    result = diff.diff_plans(PLAN, after)
    kinds = {c.key: c.kind for c in result.changes}
    assert kinds["2026-09-22 Threshold 4x8"] == "changed"
    assert kinds["2026-09-24 Easy"] == "added"
    assert any("race_date" in m for m in result.meta)
    text = result.text()
    assert "~ 2026-09-22 Threshold 4x8" in text and "notes:" in text and "5x" in text


def test_diff_of_identical_plans_is_empty():
    assert diff.diff_plans(PLAN, Plan.from_dict(PLAN.to_dict())).empty


# --- transpile --------------------------------------------------------------


def test_pace_to_power_and_back():
    powered = transpile.to_power(PLAN, PROFILE)
    step = powered.workouts[0].steps[1].steps[0]
    assert step.target.type == "power"
    assert 280 <= step.target.low <= step.target.high <= 320  # threshold zone straddles CP
    assert step.note.startswith("threshold")
    back = transpile.to_pace(powered, PROFILE)
    t = back.workouts[0].steps[1].steps[0].target
    assert t.type == "pace" and t.slow.endswith("/km")


def test_transpile_needs_cp():
    with pytest.raises(Exception, match=r"\[power\] cp"):
        transpile.to_power(PLAN, Profile.from_dict({"pace": {"threshold": "4:00/km"}}))
