"""`gpp push` end to end, against an in-memory Garmin calendar."""

import datetime as dt
import json

import pytest
from test_round3_push import FakeGarmin, connected

from gpp import cli
from gpp.client import PushError
from gpp.compile import plan_slug
from gpp.plan import Plan
from gpp.profile import Profile
from gpp.recent import list_recent

START = dt.date.today() + dt.timedelta(days=1)
PLAN = Plan.from_dict(
    {
        "plan": "Autumn 10k",
        "workouts": [
            {
                "name": "Easy",
                "date": (START + dt.timedelta(days=n)).isoformat(),
                "steps": [{"kind": "run", "duration": "40m"}],
            }
            for n in (0, 2)
        ],
    }
)
FREE_DAY = (START + dt.timedelta(days=1)).isoformat()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profile = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
    profile.save(tmp_path / "profile.toml")
    PLAN.save(tmp_path / "plan.json")
    monkeypatch.setattr("gpp.receipts.RECEIPTS_DIR", tmp_path / "pushes")
    fake = FakeGarmin(
        [
            {
                "workoutId": 77,
                "date": FREE_DAY,
                "title": "Tempo",
                "description": f"[gpp:{plan_slug(PLAN.plan)}:0123abcd]",
            }
        ]
    )
    fake.workouts[77] = {}
    monkeypatch.setattr("gpp.client.sign_in_at_terminal", lambda *a, **kw: connected(fake))
    argv = ["--profile", str(tmp_path / "profile.toml"), "push", str(tmp_path / "plan.json")]
    return argv, fake, tmp_path


def test_prune_lists_what_it_would_remove_and_asks_first(setup, monkeypatch, capsys):
    argv, fake, _ = setup
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli.main([*argv, "--prune"]) == 0
    out = capsys.readouterr().out
    assert f"{FREE_DAY}  Tempo" in out
    assert 77 in fake.workouts, "removed without asking"


def test_prune_with_yes_removes_and_reports_the_date_and_title(setup, capsys):
    argv, fake, tmp_path = setup
    assert cli.main([*argv, "--prune", "--yes"]) == 0
    assert 77 not in fake.workouts
    assert f"removed    {FREE_DAY}  Tempo" in capsys.readouterr().out
    (receipt,) = (tmp_path / "pushes").iterdir()
    rows = json.loads(receipt.read_text(encoding="utf-8"))["results"]
    assert {"removed"} <= {r["action"] for r in rows}


def test_a_failed_calendar_read_after_the_push_still_saves_the_receipt(setup, capsys, monkeypatch):
    # The read of the calendar for leftover sessions used to run between the
    # writes and the receipt: a 503 there lost the receipt and ended in a
    # traceback, with every workout already created.
    argv, fake, tmp_path = setup

    def flaky(method, path, **kw):
        wrote = any(m == "POST" for m, _ in fake.calls)
        if wrote and path.startswith("/calendar-service/"):
            raise PushError(f"GET {path} failed: 503")
        return fake(method, path, **kw)

    monkeypatch.setattr("gpp.client.sign_in_at_terminal", lambda *a, **kw: connected(flaky))
    monkeypatch.setattr("gpp.client.GarminClient._sleep", staticmethod(lambda _: None))
    assert cli.main([*argv, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "could not look for sessions" in out and "2 pushed" in out
    (receipt,) = (tmp_path / "pushes").iterdir()
    assert len(json.loads(receipt.read_text(encoding="utf-8"))["results"]) == 2


def test_a_pushed_plan_joins_the_recent_plans(setup, capsys):
    argv, _, _ = setup
    assert cli.main([*argv, "--yes"]) == 0
    (recent,) = list_recent()
    assert (recent.name, recent.label) == ("Autumn 10k", "pushed")


def test_watch_push_sends_to_the_default_device_and_saves_a_receipt(tmp_path, monkeypatch):
    from gpp.client import PushResult

    path = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}}).save(
        tmp_path / "profile.toml"
    )
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[defaults]\ndevice = 3456789012\n", encoding="utf-8"
    )
    PLAN.save(tmp_path / "plan.json")
    monkeypatch.setattr("gpp.receipts.RECEIPTS_DIR", tmp_path / "pushes")
    sent = {}

    class Client:
        def push(self, compiled, device_id=None, log=print):
            sent["device"] = device_id
            return [PushResult(c.name, c.date, "created", 100 + n) for n, c in enumerate(compiled)]

    monkeypatch.setattr("gpp.client.sign_in_at_terminal", lambda *a, **kw: Client())
    monkeypatch.setattr("gpp.watch.watch", lambda plan, on_change: on_change(plan))
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    assert cli.main(["--profile", str(path), "watch", str(tmp_path / "plan.json"), "--push"]) == 0
    assert sent["device"] == 3456789012
    (receipt,) = (tmp_path / "pushes").iterdir()
    rows = json.loads(receipt.read_text(encoding="utf-8"))["results"]
    assert [r["workout_id"] for r in rows] == [100, 101]
    assert list_recent()[0].label == "pushed"
