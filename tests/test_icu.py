"""intervals.icu push (#179), everything up to the socket: the events, the
upsert call, the error mapping, removal by tag prefix, and the command."""

import json

import pytest

from gpp import icu
from gpp.cli import build_parser
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}})
PLAN = Plan.from_dict(
    {
        "plan": "Icu block",
        "workouts": [
            {
                "name": "Easy",
                "date": "2026-10-05",
                "role": "easy",
                "steps": [
                    {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
                ],
            },
            {
                "name": "Easy",
                "date": "2026-10-07",
                "role": "easy",
                "steps": [
                    {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
                ],
            },
            {
                "name": "Lift",
                "date": "2026-10-08",
                "sport": "strength",
                "role": "strength",
                "notes": "Heavy legs, 3 x 5.",
                "steps": [{"kind": "exercise", "duration": "30m", "exercise": "SQUAT"}],
            },
        ],
    }
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        return self.responses.pop(0)


def test_events_carry_text_type_and_a_unique_external_id():
    events = icu.events(PLAN, PROFILE)
    assert [e["type"] for e in events] == ["Run", "Run", "WeightTraining"]
    assert events[0]["start_date_local"] == "2026-10-05T00:00:00"
    assert "40m" in events[0]["description"] or "easy" in events[0]["description"].lower()
    # Two identical sessions on different days stay two events.
    assert events[0]["external_id"] != events[1]["external_id"]
    assert all(e["external_id"].startswith(icu.tag_prefix(PLAN)) for e in events)
    assert "Heavy legs, 3 x 5." in events[2]["description"]


def test_push_upserts_in_bulk_with_basic_auth_and_reports_ids():
    made = [{"id": 11}, {"id": 12}, {"id": 13}]
    t = FakeTransport([(200, json.dumps(made).encode())])
    results = icu.push(PLAN, PROFILE, "i12345", "secret", transport=t)
    method, url, body, headers = t.calls[0]
    assert method == "POST" and url.endswith("/athlete/i12345/events/bulk?upsert=true")
    assert len(body) == 3 and headers["Authorization"].startswith("Basic ")
    assert "secret" not in headers["Authorization"]  # encoded, not plain
    assert [r.event_id for r in results] == [11, 12, 13]
    assert results[0].action == "sent" and "Easy" in results[0].describe()


def test_dry_run_sends_nothing_and_needs_no_key():
    t = FakeTransport([])
    results = icu.push(PLAN, PROFILE, None, None, transport=t, dry_run=True)
    assert t.calls == [] and [r.action for r in results] == ["would-send"] * 3


@pytest.mark.parametrize(
    ("athlete", "key", "fragment"),
    [("12345", "k", "i12345"), ("i12345", "", "API key")],
)
def test_bad_athlete_or_missing_key_is_a_clear_error(athlete, key, fragment):
    with pytest.raises(icu.IcuError, match=fragment):
        icu.push(PLAN, PROFILE, athlete, key, transport=FakeTransport([]))


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "rejected the key"), (404, "athlete not found"), (500, "returned 500")],
)
def test_http_errors_are_mapped(status, fragment):
    t = FakeTransport([(status, b"boom")])
    with pytest.raises(icu.IcuError, match=fragment):
        icu.push(PLAN, PROFILE, "i1", "k", transport=t)


def test_remove_deletes_only_this_plans_events_in_the_date_range():
    prefix = icu.tag_prefix(PLAN)
    listed = [
        {
            "id": 1,
            "external_id": prefix + "abc 2026-10-05",
            "name": "Easy",
            "start_date_local": "2026-10-05T00:00:00",
        },
        {"id": 2, "external_id": "[gpp:other:abc] 2026-10-06", "name": "Other plan"},
        {"id": 3, "external_id": None, "name": "Hand-made"},
    ]
    t = FakeTransport([(200, json.dumps(listed).encode()), (200, b"")])
    results = icu.remove(PLAN, PROFILE, "i12345", "k", transport=t)
    assert [c[0] for c in t.calls] == ["GET", "DELETE"]
    assert "oldest=2026-10-05&newest=2026-10-08" in t.calls[0][1]
    assert t.calls[1][1].endswith("/events/1")
    assert [r.event_id for r in results] == [1] and results[0].action == "deleted"


def test_command_is_registered_with_a_dry_run(tmp_path, capsys, monkeypatch):
    path = tmp_path / "plan.json"
    path.write_text(PLAN.dumps(), encoding="utf-8")
    monkeypatch.setattr("gpp.cli_extra._profile", lambda args: PROFILE)
    args = build_parser().parse_args(["icu", str(path), "--dry-run"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "would-send" in out and "nothing was" in out
