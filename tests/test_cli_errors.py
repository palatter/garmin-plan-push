"""Everyday mistakes end in one line, not a traceback, and Windows text files load.

PowerShell 5.1's `>` writes UTF-16 and Notepad can add a byte-order mark;
both used to stop `gpp` with a UnicodeDecodeError or "not valid TOML".
"""

import json

import pytest

from gpp import cli
from gpp.client import PushError
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:30/km"}})
PLAN = {
    "plan": "Block",
    "workouts": [
        {"name": "Easy", "date": "2026-10-01", "steps": [{"kind": "run", "duration": "40m"}]}
    ],
}


@pytest.fixture
def profile_path(tmp_path):
    return PROFILE.save(tmp_path / "profile.toml")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig"])
def test_plans_saved_by_windows_tools_load(tmp_path, profile_path, capsys, encoding):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding=encoding)
    assert cli.main(["--profile", str(profile_path), "check", str(plan)]) == 0
    assert "OK: Block - 1 workout(s) valid" in capsys.readouterr().out


def test_a_profile_with_a_byte_order_mark_loads(tmp_path, capsys):
    path = tmp_path / "profile.toml"
    path.write_text(PROFILE.to_toml(), encoding="utf-8-sig")
    assert cli.main(["--profile", str(path), "zones"]) == 0
    assert "4:30" in capsys.readouterr().out


def test_saving_over_a_utf16_plan_keeps_it_as_a_backup(tmp_path):
    from gpp.plan import Plan

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**PLAN, "plan": "Old"}), encoding="utf-16")
    Plan.from_dict(PLAN).save(plan)
    assert Plan.load(plan).plan == "Block"
    assert Plan.load(tmp_path / "plan.json.1").plan == "Old"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda tmp: tmp / "nope.json", "nope.json: No such file or directory"),
        (lambda tmp: tmp, "Is a directory"),
        (
            lambda tmp: _write(tmp / "junk.json", b"\x80\x81 not text"),
            "not a text file gpp can read",
        ),
        (
            lambda tmp: _write(tmp / "unit.json", _plan_with("20 fur")),
            "cannot parse duration '20 fur'",
        ),
    ],
)
def test_everyday_mistakes_print_one_line(tmp_path, profile_path, capsys, make, message):
    target = make(tmp_path)
    assert cli.main(["--profile", str(profile_path), "check", str(target)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and message in err and "Traceback" not in err


def test_a_garmin_failure_prints_one_line(monkeypatch, capsys):
    def fail(args):
        raise PushError("GET /device-service/deviceregistration/devices failed: 503")

    monkeypatch.setattr("gpp.cli_history._connect", fail)
    assert cli.main(["devices"]) == 1
    assert capsys.readouterr().err == (
        "error: GET /device-service/deviceregistration/devices failed: 503\n"
    )


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


def _plan_with(duration: str) -> bytes:
    step = {"kind": "run", "duration": duration}
    plan = {**PLAN, "workouts": [{**PLAN["workouts"][0], "steps": [step]}]}
    return json.dumps(plan).encode()
