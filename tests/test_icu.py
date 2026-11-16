"""intervals.icu push (#179), everything up to the socket: the events, the
upsert call, the error mapping, removal by tag prefix, and the command."""

import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

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


def test_find_matches_the_prefix_over_the_plan_and_four_weeks_ahead():
    prefix = icu.tag_prefix(PLAN)

    def event(event_id, day, external_id=None):
        return {
            "id": event_id,
            "external_id": external_id or f"{prefix}{day}:1",
            "name": "Easy",
            "start_date_local": f"{day}T00:00:00",
        }

    listed = [
        event(1, "2026-10-06"),  # inside the plan
        event(2, "2026-10-12"),  # after the plan, but already past: the record
        event(3, "2026-10-25"),  # after the plan and still ahead: moved there?
        event(4, "2026-10-07", "[gpp:other:2026-10-07:1"),  # another plan
        {"id": 5, "external_id": None, "name": "Hand-made"},
    ]
    t = FakeTransport([(200, json.dumps(listed).encode())])
    found = icu.find(PLAN, "i12345", "k", transport=t, today=dt.date(2026, 10, 20))
    assert "oldest=2026-10-05&newest=2026-11-05" in t.calls[0][1]
    assert [e["id"] for e in found] == [1, 3]
    t = FakeTransport([(200, b"[]")])
    icu.find(PLAN, "i12345", "k", transport=t, today=dt.date(2026, 9, 29))
    # Before the plan, only from today on.
    assert "oldest=2026-09-29&newest=2026-11-05" in t.calls[0][1]


def test_stale_is_what_the_plan_no_longer_has():
    ids = [e["external_id"] for e in icu.events(PLAN, PROFILE)]
    moved = {"id": 9, "external_id": f"{icu.tag_prefix(PLAN)}2026-10-06:1"}
    found = [{"id": 1, "external_id": ids[0]}, moved]
    assert icu.stale(PLAN, PROFILE, found) == [moved]


class FakeCalendar:
    """intervals.icu's events API in memory, behind the real urllib transport."""

    def __init__(self):
        self.events: dict[int, dict] = {}

    def urlopen(self, request, timeout):
        url, method = request.full_url, request.get_method()
        if method == "POST":
            out = []
            for sent in json.loads(request.data):
                same = [e for e in self.events.values() if e["external_id"] == sent["external_id"]]
                if same:
                    same[0].update(sent)
                    out.append(same[0])
                else:
                    event = {**sent, "id": len(self.events) + 100}
                    self.events[event["id"]] = event
                    out.append(event)
            return _Response(out)
        if method == "GET":
            query = parse_qs(urlparse(url).query)
            oldest, newest = query["oldest"][0], query["newest"][0]
            return _Response(
                [e for e in self.events.values() if oldest <= e["start_date_local"][:10] <= newest]
            )
        del self.events[int(url.rsplit("/", 1)[1])]
        return _Response(None)


class _Response:
    def __init__(self, body):
        self.status = 200
        self._raw = b"" if body is None else json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _week(moved: bool = False) -> Plan:
    start = dt.date.today() + dt.timedelta(days=7)
    offsets = (0, 3 if moved else 2, 4, 6)
    return Plan.from_dict(
        {
            "plan": "Moving week",
            "workouts": [
                {
                    "name": f"Run {n}",
                    "date": (start + dt.timedelta(days=d)).isoformat(),
                    "steps": [{"kind": "run", "duration": "30m"}],
                }
                for n, d in enumerate(offsets)
            ],
        }
    )


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr("gpp.icu.urllib.request.urlopen", fake.urlopen)
    monkeypatch.setattr("gpp.cli_extra._profile", lambda args: PROFILE)
    monkeypatch.setenv(icu.ATHLETE_ENV, "i12345")
    monkeypatch.setenv(icu.KEY_ENV, "k")
    path = tmp_path / "plan.json"

    def run(plan, *flags):
        path.write_text(plan.dumps(), encoding="utf-8")
        args = build_parser().parse_args(["icu", str(path), *flags])
        return args.func(args)

    return fake, run


def test_a_moved_session_is_listed_and_pruned(calendar, capsys, monkeypatch):
    # Before: push, move one session a day, push again -> five events for four.
    fake, run = calendar
    assert run(_week()) == 0
    assert run(_week(moved=True)) == 0
    assert len(fake.events) == 5
    out = capsys.readouterr().out
    assert "moved or dropped" in out and "Run 1" in out and "--prune" in out
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert run(_week(moved=True), "--prune") == 0
    assert sorted(e["name"] for e in fake.events.values()) == ["Run 0", "Run 1", "Run 2", "Run 3"]
    assert len(fake.events) == 4 and "deleted" in capsys.readouterr().out


def test_remove_lists_and_asks_first(calendar, capsys, monkeypatch):
    fake, run = calendar
    run(_week())
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run(_week(), "--remove") == 1
    assert len(fake.events) == 4 and "Aborted" in capsys.readouterr().out
    assert run(_week(moved=True), "--remove", "--yes") == 0
    assert fake.events == {}


def test_command_is_registered_with_a_dry_run(tmp_path, capsys, monkeypatch):
    path = tmp_path / "plan.json"
    path.write_text(PLAN.dumps(), encoding="utf-8")
    monkeypatch.setattr("gpp.cli_extra._profile", lambda args: PROFILE)
    args = build_parser().parse_args(["icu", str(path), "--dry-run"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "would-send" in out and "nothing was" in out


def test_the_athlete_option_is_the_intervals_id_not_a_profile_name(tmp_path, capsys, monkeypatch):
    # README's own example: --athlete used to be looked up as a gpp profile
    # called i12345, so the command always stopped at "no profile yet".
    path = tmp_path / "plan.json"
    path.write_text(PLAN.dumps(), encoding="utf-8")
    profile = PROFILE.save(tmp_path / "profile.toml")
    monkeypatch.setattr("gpp.profile.DEFAULT_PROFILE_PATHS", (profile,))
    monkeypatch.setattr("gpp.profile.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.delenv(icu.ATHLETE_ENV, raising=False)
    args = build_parser().parse_args(["icu", str(path), "--athlete", "i12345", "--dry-run"])
    assert args.func(args) == 0
    assert "would-send" in capsys.readouterr().out
    assert args.icu_athlete == "i12345"


def test_an_edited_session_keeps_its_external_id_so_a_repush_updates_it():
    edited = Plan.from_dict(
        {
            **PLAN.to_dict(),
            "workouts": [
                {**PLAN.workouts[0].to_dict(), "steps": [{"kind": "run", "duration": "55m"}]},
                *[w.to_dict() for w in PLAN.workouts[1:]],
            ],
        }
    )
    before = [e["external_id"] for e in icu.events(PLAN, PROFILE)]
    after = [e["external_id"] for e in icu.events(edited, PROFILE)]
    assert before == after
    assert len(set(after)) == len(after)


def test_two_sessions_on_one_day_get_two_events():
    double = Plan.from_dict(
        {
            "plan": "Icu block",
            "workouts": [
                {"name": "AM", "date": "2026-10-05", "steps": [{"kind": "run", "duration": "30m"}]},
                {"name": "PM", "date": "2026-10-05", "steps": [{"kind": "run", "duration": "30m"}]},
            ],
        }
    )
    ids = [e["external_id"] for e in icu.events(double, PROFILE)]
    assert len(set(ids)) == 2


# --- plans whose names start alike (0.2.2 gave them the same ids) ------------

BLOCK_1 = Plan.from_dict({**PLAN.to_dict(), "plan": "Marathon Build Block 1"})
BLOCK_2 = Plan.from_dict({**PLAN.to_dict(), "plan": "Marathon Build Block 2"})


def _on_calendar(event_id, event):
    return {"id": event_id, **event}


def _old_events(plan):
    """What gpp 0.2.2 put on the calendar for `plan`."""
    old = icu.legacy_prefix(plan)
    count: dict[str, int] = {}
    out = []
    for n, w in enumerate(plan.workouts):
        day = w.date.isoformat()
        count[day] = count.get(day, 0) + 1
        out.append(
            {
                "id": 100 + n,
                "external_id": f"{old}{day}:{count[day]}",
                "name": w.name,
                "start_date_local": f"{day}T00:00:00",
            }
        )
    return out


def test_similarly_named_plans_get_ids_of_their_own():
    assert icu.legacy_prefix(BLOCK_1) == icu.legacy_prefix(BLOCK_2), "the 0.2.2 collision"
    one = {e["external_id"] for e in icu.events(BLOCK_1, PROFILE)}
    two = {e["external_id"] for e in icu.events(BLOCK_2, PROFILE)}
    assert len(one) == len(two) == 3 and not one & two


def test_one_plans_events_are_not_found_as_the_others():
    listed = [_on_calendar(n, e) for n, e in enumerate(icu.events(BLOCK_1, PROFILE), start=1)]
    t = FakeTransport([(200, json.dumps(listed).encode())] * 2)
    today = dt.date(2026, 10, 1)
    assert icu.find(BLOCK_2, "i12345", "k", transport=t, today=today) == []
    assert len(icu.find(BLOCK_1, "i12345", "k", transport=t, today=today)) == 3


def test_a_session_sent_by_0_2_2_keeps_its_id_and_is_not_doubled():
    listed = _old_events(PLAN)
    t = FakeTransport([(200, json.dumps(listed).encode()), (200, b"[]")])
    found = icu.find(PLAN, "i12345", "k", transport=t, today=dt.date(2026, 10, 1))
    assert [e["id"] for e in found] == [100, 101, 102]
    icu.push(PLAN, PROFILE, "i12345", "k", transport=t, found=found)
    sent = t.calls[1][2]
    assert [e["external_id"] for e in sent] == [e["external_id"] for e in listed]
    assert icu.stale(PLAN, PROFILE, found) == []


def test_an_old_style_event_of_another_plan_is_never_this_plans_to_prune():
    # Block 1 was sent by 0.2.2; Block 2 has a session of another name on a
    # shared date, and none on Block 1's other dates.
    theirs = _old_events(BLOCK_1)
    mine = Plan.from_dict(
        {
            "plan": "Marathon Build Block 2",
            "workouts": [
                {
                    "name": "Tempo",
                    "date": "2026-10-05",
                    "steps": [{"kind": "run", "duration": "30m"}],
                }
            ],
        }
    )
    t = FakeTransport([(200, json.dumps(theirs).encode())])
    found = icu.find(mine, "i12345", "k", transport=t, today=dt.date(2026, 10, 1))
    assert found == [], "0.2.2 listed all three for removal as an earlier version of this plan"
    (event,) = icu.events(mine, PROFILE, found)
    assert event["external_id"] not in {e["external_id"] for e in theirs}


def _named(name: str, moved: bool = False) -> Plan:
    return Plan.from_dict({**_week(moved).to_dict(), "plan": name})


def test_sending_and_pruning_one_plan_leaves_its_namesake_alone(calendar, capsys):
    fake, run = calendar
    assert run(_named("Marathon Build Block 1")) == 0
    assert run(_named("Marathon Build Block 2", moved=True), "--prune", "--yes") == 0
    assert len(fake.events) == 8, "one plan's events overwrote the other's"
    assert run(_named("Marathon Build Block 2", moved=True), "--remove", "--yes") == 0
    left = {e["external_id"] for e in fake.events.values()}
    assert left == {e["external_id"] for e in icu.events(_named("Marathon Build Block 1"), PROFILE)}


def test_gpp_icu_updates_what_0_2_2_sent_instead_of_adding_to_it(calendar, capsys):
    fake, run = calendar
    plan = _named("Marathon Build Block 1")
    for event in _old_events(plan):
        fake.events[event["id"]] = event
    assert run(plan) == 0
    assert sorted(fake.events) == [100, 101, 102, 103]
    assert all("description" in e for e in fake.events.values()), "updated in place"
