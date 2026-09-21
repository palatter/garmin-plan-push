"""Round 3 push-path fixes: plan-scoped matching, double days, ASCII tags,
a fuller verify, an MFA-safe connect, and read retries."""

import sys
import types

import pytest

from gpp.client import GarminClient, PushError, parse_tag
from gpp.compile import STEP_NOTE_LIMIT, compile_plan
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}})


def plan(name, workouts):
    return Plan.from_dict({"plan": name, "workouts": workouts})


def session(name, date, minutes=40, zone="easy"):
    return {
        "name": name,
        "date": date,
        "steps": [
            {"kind": "run", "duration": f"{minutes}m", "target": {"type": "pace", "zone": zone}}
        ],
    }


class FakeGarmin:
    """The four calendar/workout endpoints, in memory."""

    def __init__(self, items=None):
        self.workouts: dict[int, dict] = {}
        self.calendar: list[dict] = list(items or [])
        self.next_id = 1000
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path))
        if path.startswith("/calendar-service/"):
            return {"calendarItems": list(self.calendar)}
        if method == "POST" and path == "/workout-service/workout":
            self.next_id += 1
            self.workouts[self.next_id] = kw["json"]
            return {"workoutId": self.next_id}
        if method == "PUT":
            wid = int(path.rsplit("/", 1)[1])
            self.workouts[wid] = kw["json"]
            return {}
        if method == "GET":
            wid = int(path.rsplit("/", 1)[1])
            return self.workouts[wid]
        if method == "DELETE":
            self.workouts.pop(int(path.rsplit("/", 1)[1]), None)
            return {}
        if "/schedule/" in path:
            return {}
        raise AssertionError(path)


def connected(fake):
    client = GarminClient("me@example.com")
    client._request = fake
    client.transport = "fake"
    return client


def calendar_item(wid, compiled, hash_override=None):
    tag = compiled.tag if hash_override is None else compiled.tag[:-9] + hash_override + "]"
    return {"workoutId": wid, "title": compiled.name, "description": tag, "date": compiled.date}


def test_push_leaves_another_plans_session_alone_on_a_shared_day():
    a = compile_plan(plan("Plan A", [session("Easy A", "2026-09-22")]), PROFILE)
    b = compile_plan(plan("Plan B", [session("Easy B", "2026-09-22")]), PROFILE)
    fake = FakeGarmin([calendar_item(1, a[0])])
    fake.workouts[1] = a[0].payload
    results = connected(fake).push(b)
    assert results[0].action == "created"
    assert 1 in fake.workouts, "plan A's workout was deleted"
    assert not any(m == "DELETE" for m, _ in fake.calls)


def test_double_day_updates_each_session_into_its_own_stale_workout():
    old = compile_plan(
        plan(
            "Block",
            [
                session("Threshold", "2026-09-22", 30, "threshold"),
                session("Easy", "2026-09-22", 30),
            ],
        ),
        PROFILE,
    )
    new = compile_plan(
        plan(
            "Block",
            [
                session("Threshold", "2026-09-22", 35, "threshold"),
                session("Easy", "2026-09-22", 45),
            ],
        ),
        PROFILE,
    )
    fake = FakeGarmin([calendar_item(11, old[0]), calendar_item(12, old[1])])
    fake.workouts[11], fake.workouts[12] = old[0].payload, old[1].payload
    results = connected(fake).push(new)
    assert [r.action for r in results] == ["updated", "updated"]
    assert {r.workout_id for r in results} == {11, 12}
    assert fake.workouts[11]["workoutName"] == "Threshold"
    assert fake.workouts[12]["workoutName"] == "Easy"
    assert not any(m == "DELETE" for m, _ in fake.calls)


def test_unchanged_session_is_claimed_once_on_a_double_day():
    twice = compile_plan(
        plan("Block", [session("Easy", "2026-09-22", 30), session("Easy 2", "2026-09-22", 30)]),
        PROFILE,
    )
    fake = FakeGarmin([calendar_item(21, twice[0])])
    fake.workouts[21] = twice[0].payload
    results = connected(fake).push(twice)
    assert [r.action for r in results] == ["unchanged", "created"]


def test_non_ascii_plan_names_still_match_their_own_tag():
    compiled = compile_plan(plan("Höst 10k bygg", [session("Lugnt", "2026-09-22")]), PROFILE)
    assert parse_tag(compiled[0].tag) == ("hst10kbygg", compiled[0].tag[-9:-1])
    fake = FakeGarmin([calendar_item(31, compiled[0])])
    fake.workouts[31] = compiled[0].payload
    assert connected(fake).push(compiled)[0].action == "unchanged"


def test_verify_reports_end_conditions_second_bound_and_repeat_counts():
    compiled = compile_plan(
        plan(
            "Block",
            [
                {
                    "name": "Q",
                    "date": "2026-09-22",
                    "steps": [
                        {
                            "kind": "repeat",
                            "reps": 5,
                            "steps": [
                                {
                                    "kind": "run",
                                    "distance": "1km",
                                    "target": {"type": "pace", "zone": "threshold"},
                                },
                                {"kind": "recover", "duration": "2m"},
                            ],
                        }
                    ],
                }
            ],
        ),
        PROFILE,
    )[0]
    fake = FakeGarmin()
    client = connected(fake)
    stored = __import__("copy").deepcopy(compiled.payload)
    group = stored["workoutSegments"][0]["workoutSteps"][0]
    group["numberOfIterations"] = 4
    group["workoutSteps"][0]["endConditionValue"] = 800.0
    group["workoutSteps"][0]["targetValueTwo"] = 9.9
    fake.workouts[5] = stored
    detail = client._verify(5, compiled)
    assert "repeat count sent 5, stored 4" in detail
    assert "end value changed 1000.0 -> 800.0" in detail
    assert "second target value changed" in detail


def test_connect_keeps_the_mfa_prompt_when_a_kwarg_is_unsupported(monkeypatch):
    seen = {}

    class Garmin:
        def __init__(self, email, password, prompt_mfa=None):
            seen["prompt_mfa"] = prompt_mfa

        def login(self):
            seen["logged_in"] = True

        def connectapi(self, path, method="GET", **kw):
            return {}

    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=Garmin))
    client = GarminClient("me@example.com", "pw", token_dir="C:/tmp/tokens")
    client.connect(prompt_mfa=lambda: "123456")
    assert seen["logged_in"] and callable(seen["prompt_mfa"])


def test_login_failure_names_the_token_cache(monkeypatch):
    class Garmin:
        def __init__(self, *a, **kw):
            pass

        def login(self):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=Garmin))
    with pytest.raises(PushError, match="stale"):
        GarminClient("me@example.com", "pw").connect()


def test_reads_retry_on_transient_errors_but_writes_do_not():
    attempts = {"n": 0}

    def flaky(method, path, **kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("HTTP 503 Service Unavailable")
        return {"ok": True}

    client = connected(flaky)
    slept = []
    client._sleep = slept.append
    assert client._call("GET", "/x") == {"ok": True}
    assert slept == [1.0, 2.0]
    attempts["n"] = 0
    with pytest.raises(PushError, match="503"):
        client._call("POST", "/x", json={})
    assert attempts["n"] == 1


def test_step_notes_are_cut_to_what_the_watch_keeps():
    long_note = "x" * 300
    compiled = compile_plan(
        plan(
            "Block",
            [
                {
                    "name": "Q",
                    "date": "2026-09-22",
                    "steps": [{"kind": "run", "duration": "20m", "note": long_note[:212]}],
                }
            ],
        ),
        PROFILE,
    )[0]
    step = compiled.payload["workoutSegments"][0]["workoutSteps"][0]
    assert len(step["description"]) == STEP_NOTE_LIMIT
