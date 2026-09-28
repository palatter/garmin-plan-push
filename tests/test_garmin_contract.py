"""Contract tests against the real python-garminconnect, offline.

The other client tests replace the library with a fake, which is how every
Garmin call came to fail with garminconnect 0.3.16 while the suite stayed
green. These build a real `garminconnect.Garmin` and swap only its HTTP
session for a stub, so what is checked is the library's actual API surface:
the method and URL each call reaches the wire with.
"""

from __future__ import annotations

import json

import pytest

garminconnect = pytest.importorskip("garminconnect")

from gpp.client import GarminClient  # noqa: E402

API = "https://connectapi.garmin.com"


class StubResponse:
    def __init__(self, status: int, body: object):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self) -> object:
        return self._body


class StubSession:
    """Answers like Garmin Connect; records every request instead of sending it."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, url, headers=None, **kw):
        self.calls.append((method, url, kw.get("json")))
        path = url.removeprefix(API)
        if path.endswith("/socialProfile"):
            return StubResponse(200, {"displayName": "runner", "fullName": "A Runner"})
        if path.endswith("/user-settings"):
            return StubResponse(200, {"userData": {"measurementSystem": "metric"}})
        if method == "POST" and path == "/workout-service/workout":
            return StubResponse(200, {"workoutId": 777})
        if method == "GET" and path.startswith("/calendar-service/"):
            return StubResponse(200, {"calendarItems": []})
        if method == "GET" and path.startswith("/workout-service/workout/"):
            return StubResponse(200, {"workoutId": 777, "workoutSegments": []})
        if method == "DELETE":
            return StubResponse(204, {})
        return StubResponse(200, {})


@pytest.fixture
def stub(monkeypatch):
    """Route every Garmin HTTP session the library creates through one stub."""
    session = StubSession()
    real_init = garminconnect.client.Client.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._api_session = session

    monkeypatch.setattr(garminconnect.client.Client, "__init__", init)
    return session


@pytest.fixture
def connected(stub):
    api = garminconnect.Garmin("me@example.com", "pw")
    api.client.loads(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    client = GarminClient("me@example.com", "pw")
    client._api = api
    client._request = client._resolve_transport()
    return client, stub


def test_every_verb_reaches_the_wire_with_its_own_method(connected):
    client, stub = connected
    assert client.create_workout({"workoutName": "Q"}) == 777
    client.update_workout(777, {"workoutName": "Q"})
    client.schedule_workout(777, "2026-10-01")
    assert client.get_workout(777)["workoutId"] == 777
    client.delete_workout(777)
    assert [(m, u) for m, u, _ in stub.calls] == [
        ("POST", f"{API}/workout-service/workout"),
        ("PUT", f"{API}/workout-service/workout/777"),
        ("POST", f"{API}/workout-service/schedule/777"),
        ("GET", f"{API}/workout-service/workout/777"),
        ("DELETE", f"{API}/workout-service/workout/777"),
    ]
    assert stub.calls[1][2]["workoutId"] == 777
    assert stub.calls[2][2] == {"date": "2026-10-01"}


def test_the_calendar_month_is_zero_indexed_on_the_wire(connected):
    import datetime as dt

    client, stub = connected
    client.scheduled_between(dt.date(2026, 10, 5), dt.date(2026, 10, 6))
    assert stub.calls == [("GET", f"{API}/calendar-service/year/2026/month/9", None)]


def test_the_transport_is_the_library_client(connected):
    client, _ = connected
    assert client.transport == "Garmin.client.request"
