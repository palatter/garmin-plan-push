"""A push that goes wrong part-way leaves a record, and the next one picks
up from it: a workout created but not scheduled, an unexpected error in one
session, an interruption, and a calendar that does not show descriptions.

All against an in-memory Garmin; nothing here reaches the network.
"""

import json

import pytest
from test_round3_push import PROFILE, FakeGarmin, connected, plan, session

from gpp import cli
from gpp.client import PushError, parse_tag
from gpp.compile import compile_plan
from gpp.profile import Profile
from gpp.receipts import earlier_pushes, list_receipts, load_receipt, save_receipt


class Garmin(FakeGarmin):
    """FakeGarmin with a calendar that follows the writes, as the real one does.

    `describe=False` leaves `description` out of the calendar listing: the
    case nobody has checked against the live service. `broken` holds
    (method, text in path) pairs that fail with the given error.
    """

    def __init__(self, describe=True):
        super().__init__()
        self.describe = describe
        self.broken: dict[tuple[str, str], BaseException] = {}

    def __call__(self, method, path, **kw):
        for (verb, part), error in self.broken.items():
            if verb == method and part in path:
                self.calls.append((method, path))
                raise error
        if method == "POST" and "/schedule/" in path:
            self.calls.append((method, path))
            wid = int(path.rsplit("/", 1)[1])
            item = {
                "id": 50000 + len(self.calendar),
                "workoutId": wid,
                "itemType": "workout",
                "title": self.workouts[wid]["workoutName"],
                "date": kw["json"]["date"],
            }
            if self.describe:
                item["description"] = self.workouts[wid]["description"]
            self.calendar.append(item)
            return {}
        if method == "DELETE":
            wid = int(path.rsplit("/", 1)[1])
            self.calendar = [i for i in self.calendar if i.get("workoutId") != wid]
        return super().__call__(method, path, **kw)

    def count(self, method, part):
        return sum(1 for m, p in self.calls if m == method and part in p)


def client_for(fake, plan_name=None):
    client = connected(fake)
    client._sleep = lambda _: None
    if plan_name:
        client.earlier = earlier_pushes(plan_name)
    return client


BLOCK = plan("Block", [session("Easy", "2026-12-01"), session("Tempo", "2026-12-03", zone="tempo")])


# --- created, then not scheduled ----------------------------------------------


def test_a_workout_that_could_not_be_scheduled_is_recorded_with_its_id():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin()
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    results = client_for(fake).push(compiled)
    first, second = results
    assert (first.action, first.workout_id, first.unscheduled) == ("failed", 1001, True)
    assert "1001" in first.detail and "push again" in first.detail
    assert second.action == "created", "the session after it was still sent"
    receipt = load_receipt(save_receipt("Block", results))
    assert receipt.results[0]["unscheduled"] is True
    assert 1001 in receipt.ids, "gpp unpush --receipt has to be able to remove it"


def test_the_next_push_schedules_that_workout_instead_of_creating_a_second():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin()
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    save_receipt("Block", client_for(fake).push(compiled))
    fake.broken.clear()

    again = client_for(fake, "Block").push(compiled)
    assert [(r.action, r.workout_id) for r in again] == [("created", 1001), ("unchanged", 1002)]
    assert sorted(fake.workouts) == [1001, 1002], "a second copy was created"
    assert [i["workoutId"] for i in fake.calendar] == [1002, 1001]
    assert not again[0].unscheduled


def test_once_it_is_on_the_calendar_the_receipts_stop_offering_it(tmp_path):
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin()
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    save_receipt("Block", client_for(fake).push(compiled), now=_at(1))
    assert earlier_pushes("Block").unscheduled == {("2026-12-01", "Easy"): 1001}
    fake.broken.clear()
    save_receipt("Block", client_for(fake, "Block").push(compiled), now=_at(2))
    earlier = earlier_pushes("Block")
    assert earlier.unscheduled == {} and earlier.ids == {1001, 1002}
    assert earlier_pushes("Another plan").ids == set()


def test_a_waiting_workout_whose_session_changed_is_updated_then_scheduled():
    fake = Garmin()
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    save_receipt("Block", client_for(fake).push(compile_plan(BLOCK, PROFILE)))
    fake.broken.clear()

    longer = plan("Block", [session("Easy", "2026-12-01", minutes=55)])
    (result,) = client_for(fake, "Block").push(compile_plan(longer, PROFILE))
    assert (result.action, result.workout_id) == ("created", 1001)
    assert fake.count("PUT", "/workout/1001") == 1
    assert fake.workouts[1001]["estimatedDurationInSecs"] == 55 * 60


def test_a_waiting_workout_deleted_by_hand_is_simply_created_again():
    compiled = compile_plan(BLOCK, PROFILE)[:1]
    fake = Garmin()
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    save_receipt("Block", client_for(fake).push(compiled))
    fake.broken.clear()
    del fake.workouts[1001]
    fake.broken[("GET", "/workout/1001")] = RuntimeError("API Error 404 - Not Found")

    (result,) = client_for(fake, "Block").push(compiled)
    assert (result.action, result.workout_id) == ("created", 1002)


def test_a_waiting_workout_that_cannot_be_read_holds_the_session_back():
    # Not a 404: Garmin may well still have it, so creating another is the
    # one thing not to do. (The id is chosen to contain "404".)
    compiled = compile_plan(BLOCK, PROFILE)[:1]
    fake = Garmin()
    fake.next_id = 14040
    fake.broken[("POST", "/schedule/14041")] = RuntimeError("API Error 503")
    save_receipt("Block", client_for(fake).push(compiled))
    fake.broken = {("GET", "/workout/14041"): RuntimeError("API Error 500")}

    (result,) = client_for(fake, "Block").push(compiled)
    assert result.action == "failed" and "push again" in result.detail
    assert sorted(fake.workouts) == [14041]


def test_a_receipt_never_makes_gpp_touch_a_workout_that_is_not_this_plans():
    compiled = compile_plan(BLOCK, PROFILE)[:1]
    fake = Garmin()
    fake.workouts[555] = {"workoutName": "Easy", "description": "my own notes"}
    save_receipt(
        "Block",
        [
            {
                "name": "Easy",
                "date": "2026-12-01",
                "action": "failed",
                "workout_id": 555,
                "unscheduled": True,
            }
        ],
    )
    (result,) = client_for(fake, "Block").push(compiled)
    assert (result.action, result.workout_id) == ("created", 1001)
    assert fake.workouts[555] == {"workoutName": "Easy", "description": "my own notes"}
    assert fake.count("POST", "/schedule/555") == 0


# --- an unexpected error, an interruption ------------------------------------


def test_an_empty_read_back_is_a_warning_and_the_push_goes_on():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin()
    real = fake.__call__

    def empty_read_back(method, path, **kw):
        if method == "GET" and path == "/workout-service/workout/1001":
            return None
        return real(method, path, **kw)

    results = client_for(empty_read_back).push(compiled)
    assert [(r.action, r.workout_id) for r in results] == [("created", 1001), ("created", 1002)]
    assert results[0].detail.startswith("could not verify")


def test_an_unexpected_error_in_one_session_does_not_stop_the_others():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin()
    real = fake.__call__

    def odd_reply(method, path, **kw):
        if method == "POST" and path == "/workout-service/workout" and not fake.workouts:
            fake.workouts[1] = kw["json"]
            return ["not", "a", "dict"]  # AttributeError inside create_workout
        return real(method, path, **kw)

    results = client_for(odd_reply).push(compiled)
    assert [r.action for r in results] == ["failed", "created"]
    assert "unexpected error (AttributeError" in results[0].detail


def test_an_interrupted_push_says_what_went_and_what_was_never_reached():
    three = plan(
        "Block",
        [
            session("One", "2026-12-01"),
            session("Two", "2026-12-02"),
            session("Three", "2026-12-03"),
        ],
    )
    fake = Garmin()
    fake.broken[("POST", "/schedule/1002")] = KeyboardInterrupt()
    results = []
    with pytest.raises(KeyboardInterrupt):
        client_for(fake).push(compile_plan(three, PROFILE), results=results)
    assert [(r.name, r.action) for r in results] == [
        ("One", "created"),
        ("Two", "failed"),
        ("Three", "not-sent"),
    ]
    assert "interrupted" in results[1].detail


@pytest.fixture
def at_the_terminal(tmp_path, monkeypatch):
    Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}}).save(
        tmp_path / "profile.toml"
    )
    BLOCK.save(tmp_path / "plan.json")
    fake = Garmin()
    monkeypatch.setattr("gpp.client.sign_in_at_terminal", lambda *a, **kw: client_for(fake))
    argv = ["--profile", str(tmp_path / "profile.toml"), "push", str(tmp_path / "plan.json")]
    return [*argv, "--yes"], fake


def test_ctrl_c_during_gpp_push_still_writes_the_receipt(at_the_terminal, capsys):
    argv, fake = at_the_terminal
    fake.broken[("POST", "/schedule/1002")] = KeyboardInterrupt()
    assert cli.main(argv) == 130
    (receipt,) = list_receipts()
    assert [(r["name"], r["action"], r["workout_id"]) for r in receipt.results] == [
        ("Easy", "created", 1001),
        ("Tempo", "failed", None),
    ]
    assert "receipt:" in capsys.readouterr().out


def test_gpp_push_again_schedules_what_the_first_run_left_behind(at_the_terminal, capsys):
    argv, fake = at_the_terminal
    fake.broken[("POST", "/schedule/1001")] = RuntimeError("API Error 503")
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert "1 pushed, 0 unchanged, 1 failed" in captured.out
    assert "workout 1001 was created but could not be put on the calendar" in captured.err
    fake.broken.clear()
    assert cli.main(argv) == 0
    assert "scheduling the workout an earlier push created (id 1001)" in capsys.readouterr().out
    assert sorted(fake.workouts) == [1001, 1002]


def test_the_web_send_writes_its_receipt_when_the_job_dies_after_the_push(tmp_path, monkeypatch):
    import time

    from gpp import client as client_module
    from gpp.web import server

    profile_path = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}}).save(
        tmp_path / "profile.toml"
    )
    app = server.App(profile_path=str(profile_path))
    fake = Garmin()
    left = [{"date": "2026-12-02", "title": "Old", "workout_id": 9}]
    monkeypatch.setattr(client_module.GarminClient, "orphans", lambda self, compiled: left)

    def dies(self, orphans, log=None):
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(client_module.GarminClient, "remove_orphans", dies)
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: client_for(fake))
    job = app.push({"plan": BLOCK.to_dict(), "email": "me@example.com", "prune": True})
    for _ in range(200):
        snap = app.jobs.get(job["job"]).snapshot()
        if snap["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert snap["status"] == "error"
    (receipt,) = list_receipts()
    assert [(r["name"], r["action"]) for r in receipt.results] == [
        ("Easy", "created"),
        ("Tempo", "created"),
    ]


# --- a calendar without descriptions ------------------------------------------


def test_the_tag_comes_first_so_a_shortened_description_keeps_it():
    noted = plan("Block", [{**session("Easy", "2026-12-01"), "notes": "Keep it relaxed. " * 60}])
    (compiled,) = compile_plan(noted, PROFILE)
    description = compiled.payload["description"]
    assert description.startswith(compiled.tag)
    assert parse_tag(description[:80]) == parse_tag(compiled.tag)
    assert description.endswith("Keep it relaxed.")


def test_pushing_twice_is_unchanged_when_the_calendar_shows_no_descriptions():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    client_for(fake).push(compiled)
    again = client_for(fake).push(compiled)
    assert [(r.action, r.workout_id) for r in again] == [("unchanged", 1001), ("unchanged", 1002)]
    assert sorted(fake.workouts) == [1001, 1002], "the plan is on the calendar twice"


def test_an_edited_session_is_updated_in_place_without_descriptions():
    fake = Garmin(describe=False)
    client_for(fake).push(compile_plan(BLOCK, PROFILE))
    edited = plan(
        "Block",
        [session("Easy", "2026-12-01", minutes=50), session("Tempo", "2026-12-03", zone="tempo")],
    )
    again = client_for(fake).push(compile_plan(edited, PROFILE))
    assert [(r.action, r.workout_id) for r in again] == [("updated", 1001), ("unchanged", 1002)]
    assert sorted(fake.workouts) == [1001, 1002]


def test_the_plans_own_sessions_are_not_called_hand_made_without_descriptions():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    fake.workouts[7] = {"workoutName": "Hill reps", "description": ""}
    fake.calendar.append(
        {"workoutId": 7, "itemType": "workout", "title": "Hill reps", "date": "2026-12-01"}
    )
    client = client_for(fake)
    client.push(compiled)
    assert client.conflicts(compiled) == [
        {"date": "2026-12-01", "title": "Hill reps", "source": "hand-made or synced", "id": 7}
    ]
    assert 7 in fake.workouts and fake.workouts[7]["workoutName"] == "Hill reps"


def test_unpush_and_prune_find_the_plans_sessions_without_descriptions():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    client_for(fake).push(compiled)
    moved = plan("Block", [session("Easy", "2026-12-02")])
    client = client_for(fake)
    import datetime as dt

    orphans = client.orphans(compile_plan(moved, PROFILE), today=dt.date(2026, 11, 1))
    assert [(o["date"], o["title"], o["workout_id"]) for o in orphans] == [
        ("2026-12-01", "Easy", 1001),
        ("2026-12-03", "Tempo", 1002),
    ]
    removed = client_for(fake).unpush(compiled)
    assert [r.action for r in removed] == ["removed", "removed"] and fake.workouts == {}


def test_a_listing_that_does_show_descriptions_costs_no_extra_reads():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=True)
    client_for(fake).push(compiled, verify=False)
    fake.workouts[7] = {"workoutName": "Hill reps"}
    fake.calendar.append(
        {"workoutId": 7, "itemType": "workout", "title": "Hill reps", "date": "2026-12-01"}
    )
    fake.calls.clear()
    again = client_for(fake).push(compiled, verify=False)
    assert [r.action for r in again] == ["unchanged", "unchanged"]
    assert fake.count("GET", "/workout-service/workout/") == 0


def test_a_tag_cut_off_in_the_listing_is_read_from_the_workout_a_receipt_names():
    # Pushed by 0.2.2, tag after a long note; the listing shows the start only.
    compiled = compile_plan(BLOCK, PROFILE)[:1]
    fake = Garmin()
    save_receipt("Block", client_for(fake).push(compiled))
    fake.workouts[1001]["description"] = "A long note. " * 50 + compiled[0].tag
    fake.calendar[0]["description"] = ("A long note. " * 50)[:200]
    (result,) = client_for(fake, "Block").push(compiled)
    assert (result.action, result.workout_id) == ("unchanged", 1001)
    (without_receipt,) = client_for(fake).push(compiled)
    assert without_receipt.action == "created", "0.2.2's behaviour, for contrast"


def test_an_unreadable_workout_a_receipt_names_holds_its_session_back():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    save_receipt("Block", client_for(fake).push(compiled))
    fake.broken[("GET", "/workout/1001")] = RuntimeError("API Error 500")
    again = client_for(fake, "Block").push(compiled)
    assert [r.action for r in again] == ["failed", "unchanged"]
    assert "could not read workout 1001" in again[0].detail
    assert "not sent" in again[0].detail
    assert sorted(fake.workouts) == [1001, 1002], "a duplicate was created"


def test_an_unreadable_workout_in_no_receipt_is_sent_and_the_receipt_says_so():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    client_for(fake).push(compiled)  # no receipt kept: another computer, say
    fake.broken[("GET", "/workout/1001")] = RuntimeError("API Error 500")
    client = client_for(fake)
    preview = client.preview(compiled)
    assert preview[0].action == "would-create" and preview[0].duplicates_unchecked
    again = client_for(fake).push(compiled, verify=False)
    assert [(r.action, r.duplicates_unchecked) for r in again] == [
        ("created", True),
        ("unchanged", False),
    ]
    assert "was not ruled out" in again[0].detail and "2026-12-01" in again[0].detail
    receipt = load_receipt(save_receipt("Block", again))
    assert receipt.results[0]["duplicates_unchecked"] is True
    assert "duplicates_unchecked" not in receipt.results[1]
    assert "duplicates not ruled out for 1" in receipt.describe()


def test_a_same_titled_entry_with_no_workout_id_leaves_the_question_open():
    compiled = compile_plan(BLOCK, PROFILE)[:1]
    fake = Garmin()
    fake.calendar.append({"id": 9, "itemType": "workout", "title": "Easy", "date": "2026-12-01"})
    (result,) = client_for(fake).push(compiled)
    assert (result.action, result.duplicates_unchecked) == ("created", True)
    fake.calendar[0] = {"id": 9, "itemType": "activity", "title": "Easy", "date": "2026-12-01"}
    (other,) = client_for(fake).push(
        compile_plan(plan("Other", [session("Easy", "2026-12-01")]), PROFILE)
    )
    assert not other.duplicates_unchecked, "a finished run with the same name is no copy"


def test_unpush_leaves_what_it_cannot_read_and_says_so():
    compiled = compile_plan(BLOCK, PROFILE)
    fake = Garmin(describe=False)
    client_for(fake).push(compiled)
    fake.broken[("GET", "/workout/1001")] = RuntimeError("API Error 500")
    results = client_for(fake).unpush(compiled)
    assert [(r.name, r.action) for r in results] == [("Easy", "failed"), ("Tempo", "removed")]
    assert "could not be read" in results[0].detail and 1001 in fake.workouts


def test_push_error_is_still_what_a_failed_calendar_read_raises():
    fake = Garmin()
    fake.broken[("GET", "/calendar-service/")] = RuntimeError("API Error 503")
    results = []
    with pytest.raises(PushError):
        client_for(fake).push(compile_plan(BLOCK, PROFILE), results=results)
    assert results == [] and fake.workouts == {}


def _at(minute):
    import datetime as dt

    return dt.datetime(2026, 11, 10, 9, minute, tzinfo=dt.UTC)


def test_receipt_rows_stay_json(tmp_path):
    path = save_receipt("Block", client_for(Garmin()).push(compile_plan(BLOCK, PROFILE)))
    rows = json.loads(path.read_text(encoding="utf-8"))["results"]
    assert set(rows[0]) == {"name", "date", "action", "workout_id", "detail"}
