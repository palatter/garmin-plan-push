"""Plans the command line writes join the recent plans, as the web app's do.

`gpp next`, `gpp week` and `gpp recap` fall back on the most recent plan,
but only the web app recorded any, so after command-line work they said
there were no recent plans.
"""

import datetime as dt

from gpp import cli
from gpp.plan import Plan
from gpp.profile import Profile
from gpp.recent import list_recent

TOMORROW = dt.date.today() + dt.timedelta(days=1)
PLAN = {
    "plan": "Autumn base",
    "workouts": [
        {
            "name": f"Easy {n}",
            "date": (TOMORROW + dt.timedelta(days=n)).isoformat(),
            "steps": [{"kind": "run", "duration": "40m"}],
        }
        for n in (0, 2, 4)
    ],
}


def test_a_plan_a_command_writes_is_the_one_gpp_next_uses(tmp_path, capsys):
    profile = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
    profile_path = str(profile.save(tmp_path / "profile.toml"))
    plan = Plan.from_dict(PLAN).save(tmp_path / "plan.json")
    assert cli.main(["--profile", profile_path, "next"]) == 2
    assert "no recent plans" in capsys.readouterr().err

    start = (TOMORROW + dt.timedelta(days=1)).isoformat()
    pause = ["pause", str(plan), "--start", start, "--days", "3"]
    assert cli.main(["--profile", profile_path, *pause, "-o", str(tmp_path / "p.json")]) == 0
    (recent,) = list_recent()
    assert (recent.name, recent.label, recent.sessions) == ("Autumn base", "paused", 3)
    capsys.readouterr()
    assert cli.main(["--profile", profile_path, "--json", "next"]) == 0
    assert '"name": "Easy 0"' in capsys.readouterr().out
