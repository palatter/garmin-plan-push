"""History store, the Garmin sync parsers, and the client's new operations.

The Garmin side is exercised with fakes shaped like the library's responses;
nothing here touches the network.
"""

import datetime as dt

import pytest

from gpp import sync
from gpp.client import GarminClient, parse_tag
from gpp.compile import compile_plan
from gpp.history import History
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"pace": {"threshold": "4:00/km"}})


@pytest.fixture
def store():
    h = History(":memory:")
    yield h
    h.close()


# --- history ----------------------------------------------------------------


def test_activities_round_trip_and_weekly(store):
    store.upsert_activity({"id": "a1", "date": "2026-09-22", "distance_m": 10000, "seconds": 3000})
    store.upsert_activity({"id": "a2", "date": "2026-09-24", "distance_m": 5000, "seconds": 1500})
    store.upsert_activity(
        {"id": "a1", "date": "2026-09-22", "distance_m": 10500, "seconds": 3100}
    )  # upsert
    acts = store.activities()
    assert len(acts) == 2 and acts[0]["distance_m"] == 10500
    weeks = store.weekly(dt.date(2026, 9, 1))
    assert weeks[0]["km"] == 15.5 and weeks[0]["runs"] == 2


def test_metrics_upsert_and_baseline(store):
    for i, rhr in enumerate((50, 51, 49, 50)):
        store.upsert_metrics(
            (dt.date(2026, 9, 18) + dt.timedelta(days=i)).isoformat(), resting_hr=rhr
        )
    store.upsert_metrics("2026-09-22", readiness=35, hrv_status="low", resting_hr=58)
    assert store.metrics(dt.date(2026, 9, 22))["readiness"] == 35
    assert store.resting_hr_baseline(dt.date(2026, 9, 22)) == 50
    advice = store.today_advice("quality", dt.date(2026, 9, 22))
    assert advice.downgrade
    assert any("resting HR is 8" in r for r in advice.reasons)


def test_planned_and_compliance(store):
    store.record_planned(
        "Block",
        [
            {"date": "2026-09-22", "name": "Q", "seconds": 3600},
            {"date": "2026-09-24", "name": "E", "seconds": 2400},
        ],
    )
    store.upsert_activity({"id": "x", "date": "2026-09-22", "distance_m": 11000, "seconds": 3500})
    rows = store.compliance(dt.date(2026, 9, 22), dt.date(2026, 9, 24))
    assert [r["status"] for r in rows] == ["green", "grey"]


def test_rpe_and_pain_logs(store):
    store.log_rpe("2026-09-22", "Q", 7, "felt strong")
    store.log_rpe("2026-09-22", "Q", 8)  # update
    assert store.rpe_log()[0]["rpe"] == 8
    with pytest.raises(ValueError):
        store.log_rpe("2026-09-22", "Q", 11)
    store.log_pain("2026-09-20", "Left Achilles", 3, "warms up")
    store.log_pain("2026-09-23", "left achilles", 5)
    trend = store.pain_trend(dt.date(2026, 9, 1))
    assert trend["left achilles"] == [("2026-09-20", 3), ("2026-09-23", 5)]


def test_reestimate_and_shape_over_store(store):
    store.upsert_activity(
        {"id": "hard", "date": "2026-09-10", "distance_m": 14000, "seconds": 3600}
    )
    est = store.reestimate_threshold(dt.date(2026, 9, 21))
    assert est and est.confidence == "high"
    for i in range(26):
        store.upsert_activity(
            {
                "id": f"w{i}",
                "date": (dt.date(2026, 3, 23) + dt.timedelta(weeks=i)).isoformat(),
                "distance_m": 30000,
                "seconds": 9000,
            }
        )
    shape = store.marathon_shape(42.195, dt.date(2026, 9, 21))
    assert 0 < shape.score < 100


# --- sync parsers -----------------------------------------------------------


def test_parse_activity_keeps_runs_only():
    run = {
        "activityId": 1,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-09-22 07:01:00",
        "distance": 10012.3,
        "duration": 3001.2,
        "averageHR": 152,
        "activityName": "Morning Run",
    }
    ride = dict(run, activityId=2, activityType={"typeKey": "cycling"})
    assert sync.parse_activity(run)["avg_hr"] == 152
    assert sync.parse_activity(run)["date"] == "2026-09-22"
    assert sync.parse_activity(ride) is None


class FakeAPI:
    """Shaped like python-garminconnect, with a deliberately missing method."""

    def __init__(self):
        self.calls = []

    def get_activities(self, start, limit):
        self.calls.append(("get_activities", start, limit))
        if start > 0:
            return []
        return [
            {
                "activityId": 1,
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2026-09-22 07:00:00",
                "distance": 10000,
                "duration": 3000,
            },
            {
                "activityId": 2,
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": "2026-09-21 07:00:00",
                "distance": 30000,
                "duration": 3600,
            },
            {
                "activityId": 3,
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2020-01-01 07:00:00",
                "distance": 5000,
                "duration": 1500,
            },
        ]

    def get_training_readiness(self, day):
        return [{"score": 62}]

    def get_hrv_data(self, day):
        return {"hrvSummary": {"status": "BALANCED", "weeklyAvg": 55}}

    def get_sleep_data(self, day):
        return {"dailySleepDTO": {"sleepScores": {"overall": {"value": 78}}}}

    def get_rhr_day(self, day):
        return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 49}]}}}

    def get_max_metrics(self, day):
        return [{"generic": {"vo2MaxPreciseValue": 52.3}}]

    # no get_training_status on purpose

    def get_race_predictions(self):
        return [{"time5K": 1260, "time10K": 2640, "timeHalfMarathon": 5900, "timeMarathon": 12600}]

    def get_devices(self):
        return [{"deviceId": 123, "displayName": "fenix 7S"}]

    def get_exercise_catalog(self):
        return {
            "exercises": [
                {"name": "GOBLET_SQUAT", "category": "SQUAT"},
                {"name": "PLANK", "category": "PLANK"},
            ]
        }


def test_sync_activities_stops_at_the_cutoff(store):
    api = FakeAPI()
    result = sync.sync_activities(api, store, days=3650)
    assert result.activities == 2  # the 2020 run is older than the cutoff? no: 3650 days keeps it
    assert len(api.calls) == 1  # stopped when the batch hit an old run


def test_sync_metrics_reads_every_shape_and_notes_missing(store):
    result = sync.sync_metrics(FakeAPI(), store, dt.date(2026, 9, 22))
    m = store.metrics(dt.date(2026, 9, 22))
    assert (
        m["readiness"],
        m["hrv_status"],
        m["hrv_weekly_avg"],
        m["sleep_score"],
        m["resting_hr"],
        m["vo2max"],
    ) == (62, "balanced", 55, 78, 49, 52.3)
    assert m["training_status"] is None
    assert any(s.startswith("get_training_status") for s in result.skipped)


def test_race_predictions_and_hr_zones():
    api = FakeAPI()
    assert sync.race_predictions(api)["10k"] == 2640
    assert sync.hr_zones(api) is None  # method absent -> None, not a crash


# --- client operations over a fake transport --------------------------------


class FakeTransport:
    def __init__(self, calendar_items):
        self.calendar_items = calendar_items
        self.log = []
        self.next_id = 500

    def __call__(self, method, path, **kw):
        self.log.append((method, path, kw.get("json")))
        if method == "GET" and "calendar-service" in path:
            return {"calendarItems": self.calendar_items}
        if method == "POST" and path.endswith("/workout-service/workout"):
            self.next_id += 1
            return {"workoutId": self.next_id}
        if method == "GET" and "/workout-service/workout/" in path:
            return kw.get("json") or {"workoutSegments": [{"workoutSteps": []}]}
        return {}


def client_with(items):
    c = GarminClient("x@y")
    c._request = FakeTransport(items)
    c._api = FakeAPI()
    return c


def compiled_one(name="Q", date="2026-09-22"):
    plan = Plan.from_dict(
        {
            "plan": "Block",
            "workouts": [
                {"name": name, "date": date, "steps": [{"kind": "run", "duration": "30m"}]}
            ],
        }
    )
    return compile_plan(plan, PROFILE)


def test_push_updates_a_stale_tagged_workout_in_place():
    item = compiled_one()[0]
    stale = {
        "id": 42,
        "workoutId": 42,
        "date": "2026-09-22",
        "title": "Q",
        "description": "[gpp:block:deadbeef]",
    }
    client = client_with([stale])
    results = client.push([item], verify=False)
    assert results[0].action == "updated" and results[0].workout_id == 42
    methods = [(m, p) for m, p, _ in client._request.log]
    assert ("PUT", "/workout-service/workout/42") in methods
    assert not any(m == "DELETE" for m, _ in methods)


def test_push_never_touches_hand_made_workouts():
    item = compiled_one()[0]
    hand_made = {
        "id": 7,
        "workoutId": 7,
        "date": "2026-09-22",
        "title": "Q",
        "description": "my own thing",
    }
    client = client_with([hand_made])
    results = client.push([item], verify=False)
    assert results[0].action == "created"
    assert not any(m in ("PUT", "DELETE") for m, _, _ in client._request.log)


def test_unpush_removes_only_this_plans_workouts():
    items = compiled_one()
    tag = items[0].tag
    mine = {"id": 1, "workoutId": 1, "date": "2026-09-22", "title": "Q", "description": tag}
    other_plan = {
        "id": 2,
        "workoutId": 2,
        "date": "2026-09-22",
        "title": "Q",
        "description": "[gpp:otherplan:12345678]",
    }
    hand = {"id": 3, "workoutId": 3, "date": "2026-09-22", "title": "Q", "description": ""}
    client = client_with([mine, other_plan, hand])
    results = client.unpush(items)
    assert [r.action for r in results] == ["removed"] and results[0].workout_id == 1
    deleted = [p for m, p, _ in client._request.log if m == "DELETE"]
    assert deleted == ["/workout-service/workout/1"]


def test_devices_and_exercise_search_go_through_the_library():
    client = client_with([])
    assert client.devices()[0].name == "fenix 7S"
    assert client.search_exercises("goblet")[0]["name"] == "GOBLET_SQUAT"


def test_parse_tag():
    assert parse_tag("notes\n\n[gpp:block:0123abcd]") == ("block", "0123abcd")
    assert parse_tag("nothing") is None
