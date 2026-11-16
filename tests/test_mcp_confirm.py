"""The MCP push tool cannot write to Garmin on an assistant's say-so: a real
push needs the code that only `gpp mcp --confirm`, run by a person, shows."""

import datetime as dt
import json

import pytest
from test_push_recovery import Garmin, client_for

from gpp import cli, mcp_server
from gpp.client import PushError
from gpp.profile import Profile
from gpp.receipts import list_receipts

PLAN = {
    "plan": "Autumn 10k",
    "workouts": [
        {"name": "Easy", "date": "2026-12-01", "steps": [{"kind": "run", "duration": "40m"}]},
        {"name": "Long", "date": "2026-12-05", "steps": [{"kind": "run", "duration": "80m"}]},
    ],
}
OTHER = {**PLAN, "workouts": PLAN["workouts"][:1]}


@pytest.fixture
def garmin(tmp_path, monkeypatch):
    Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}}).save(
        tmp_path / "profile.toml"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    fake = Garmin()
    seen = {}

    def sign_in(email, password, **kw):
        seen["sign_in"] = (email, password, kw)
        return client_for(fake)

    monkeypatch.setattr("gpp.client.sign_in", sign_in)
    fake.seen = seen
    return fake


def code():
    return json.loads(mcp_server.PENDING_PATH.read_text(encoding="utf-8"))["code"]


def push(plan=PLAN, **kw):
    return mcp_server.tool_push(json.dumps(plan), dry_run=False, **kw)


def test_a_push_with_no_confirmation_writes_nothing(garmin):
    with pytest.raises(PushError, match="nothing was sent") as error:
        push()
    assert "gpp mcp --confirm" in str(error.value)
    assert garmin.calls == [] and "sign_in" not in garmin.seen


def test_the_dry_run_holds_the_plan_and_never_shows_the_code(garmin):
    out = mcp_server.tool_push(json.dumps(PLAN))
    held = code()
    assert len(held) == 6 and held.isdigit()
    assert held not in json.dumps(out)
    assert "gpp mcp --confirm" in out["to_send"]
    for attempt in (None, "", "   "):
        with pytest.raises(PushError, match="code is missing") as error:
            push(confirm=attempt)
        assert held not in str(error.value)
    assert garmin.calls == []


def test_the_code_from_the_terminal_lets_that_plan_through_once(garmin, capsys):
    mcp_server.tool_push(json.dumps(PLAN))
    assert cli.main(["mcp", "--confirm"]) == 0
    shown = capsys.readouterr().out
    assert "Autumn 10k: 2 workout(s), 2026-12-01 to 2026-12-05" in shown and code() in shown
    assert "2026-12-01  Easy" in shown and "2026-12-05  Long" in shown

    out = push(confirm=f" {code()} ")
    assert [r["action"] for r in out["results"]] == ["created", "created"]
    assert sorted(garmin.workouts) == [1001, 1002]
    (receipt,) = list_receipts()
    assert out["receipt"] == str(receipt.path)
    # The saved sign-in or the keychain, as the app does; no prompt to hang on.
    email, password, kw = garmin.seen["sign_in"]
    assert (email, password) == ("me@example.com", None) and not kw.get("ask_password")

    assert not mcp_server.PENDING_PATH.exists(), "one confirmation, one push"
    with pytest.raises(PushError, match="nothing was sent"):
        push(confirm="000000")
    assert cli.main(["mcp", "--confirm"]) == 0


def test_a_code_does_not_release_a_different_plan(garmin):
    mcp_server.tool_push(json.dumps(PLAN))
    first = code()
    with pytest.raises(PushError, match="has changed since"):
        push(OTHER, confirm=first)
    assert garmin.calls == []
    # The other plan is now the one held, under a new code a person has to read.
    assert json.loads(mcp_server.PENDING_PATH.read_text(encoding="utf-8"))["workouts"] == 1


def test_wrong_codes_are_counted_and_the_third_cancels(garmin):
    mcp_server.tool_push(json.dumps(PLAN))
    right = code()
    wrong = "111111" if right != "111111" else "222222"
    for _ in range(2):
        with pytest.raises(PushError, match="not the code"):
            push(confirm=wrong)
    with pytest.raises(PushError, match="cancelled"):
        push(confirm=wrong)
    with pytest.raises(PushError, match="nothing was sent"):
        push(confirm=right)
    assert garmin.calls == []


def test_a_code_goes_stale_after_ten_minutes(garmin, monkeypatch, capsys):
    mcp_server.tool_push(json.dumps(PLAN))
    stale = code()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=11)
    monkeypatch.setattr(mcp_server, "_now", lambda: later)
    assert cli.main(["mcp", "--confirm"]) == 0
    assert "No push is waiting" in capsys.readouterr().out
    with pytest.raises(PushError, match="nothing was sent"):
        push(confirm=stale)
    assert garmin.calls == []


def test_a_damaged_pending_file_releases_nothing(garmin):
    mcp_server.PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{not json", "[]", '{"code": "123456"}', '{"code": 123456, "digest": "x"}'):
        mcp_server.PENDING_PATH.write_text(text, encoding="utf-8")
        assert mcp_server.pending_push() is None
        with pytest.raises(PushError, match="nothing was sent"):
            push(confirm="123456")
    assert garmin.calls == []


def test_the_tool_description_tells_the_assistant_it_cannot_confirm():
    text = mcp_server.tool_push.__doc__
    assert "gpp mcp --confirm" in text and "Only the user can supply the code" in text
