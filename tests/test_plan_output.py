"""A plan a command writes over an existing file keeps that file as .1.

`generate -o` and every other `-o` that writes a plan used to overwrite in
place, while the web app and `Plan.save` keep the last five versions.
"""

from types import SimpleNamespace

import pytest

from gpp import cli
from gpp.plan import Plan
from gpp.profile import Profile

OLD = {
    "plan": "Old block",
    "workouts": [
        {"name": "Easy", "date": "2026-10-05", "steps": [{"kind": "run", "duration": "40m"}]},
        {"name": "Long", "date": "2026-10-11", "steps": [{"kind": "run", "duration": "90m"}]},
    ],
}
NEW = {**OLD, "plan": "New block"}


@pytest.fixture
def files(tmp_path):
    profile = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
    plan = Plan.from_dict(OLD).save(tmp_path / "plan.json")
    return str(profile.save(tmp_path / "profile.toml")), plan


def test_pause_over_its_own_plan_keeps_the_old_one(files, capsys):
    profile, plan = files
    args = ["--profile", profile, "pause", str(plan), "--start", "2026-10-06", "--days", "3"]
    assert cli.main([*args, "-o", str(plan)]) == 0
    assert Plan.load(plan.with_name("plan.json.1")).to_dict() == Plan.from_dict(OLD).to_dict()
    assert Plan.load(plan).workouts[-1].date.isoformat() == "2026-10-14"


def test_generate_over_an_existing_plan_keeps_the_old_one(files, monkeypatch, capsys):
    profile, plan = files
    monkeypatch.setattr(cli, "build_provider", lambda config: object())
    monkeypatch.setattr(
        cli,
        "generate_plan",
        lambda *a, **k: SimpleNamespace(data=NEW, plan=Plan.from_dict(NEW)),
    )
    assert cli.main(["--profile", profile, "generate", "a block", "-o", str(plan)]) == 0
    assert Plan.load(plan).plan == "New block"
    assert Plan.load(plan.with_name("plan.json.1")).plan == "Old block"
