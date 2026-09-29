"""Contract tests against the real python-garminconnect, offline.

The other client tests replace the library with a fake, which is how every
Garmin call came to fail with garminconnect 0.3.16 while the suite stayed
green. These build a real `garminconnect.Garmin` and swap only its HTTP
session for a stub, so what is checked is the library's actual API surface:
the method and URL each call reaches the wire with.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

garminconnect = pytest.importorskip("garminconnect")

from gpp.client import NeedsPassword, PushError, saved_login, sign_in  # noqa: E402

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
def sso(monkeypatch):
    """Garmin's password sign-in, minus the network: hands out a token."""
    logins: list[str] = []

    def login(self, email, password, prompt_mfa=None, return_on_mfa=False):
        logins.append(email)
        self.di_token, self.di_refresh_token, self.di_client_id = "t", "r", "c"
        return None, None

    monkeypatch.setattr(garminconnect.client.Client, "login", login)
    return logins


@pytest.fixture
def connected(tmp_path, stub, sso):
    client = sign_in("me@example.com", "pw", str(tmp_path / "tokens"))
    stub.calls.clear()
    return client, stub


def test_a_password_sign_in_saves_the_login_owner_only(tmp_path, stub, sso):
    folder = str(tmp_path / "tokens")
    sign_in("me@example.com", "pw", folder)
    token = saved_login(folder)
    assert token is not None and sso == ["me@example.com"]
    if os.name == "posix":
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700


def test_the_next_sign_in_uses_the_saved_login_without_a_password(tmp_path, stub, sso):
    folder = str(tmp_path / "tokens")
    sign_in("me@example.com", "pw", folder)
    asked = []
    client = sign_in("me@example.com", None, folder, ask_password=lambda: asked.append(1) or "pw")
    assert client.transport == "Garmin.client.request"
    assert asked == [] and len(sso) == 1  # no second password sign-in


def test_an_unusable_saved_login_asks_for_the_password_once(tmp_path, stub, sso):
    folder = tmp_path / "tokens"
    folder.mkdir()
    (folder / "garmin_tokens.json").write_text("{}", encoding="utf-8")
    with pytest.raises(NeedsPassword, match="expired"):
        sign_in("me@example.com", None, str(folder))
    asked = []
    sign_in("me@example.com", None, str(folder), ask_password=lambda: asked.append(1) or "pw")
    assert asked == [1] and sso == ["me@example.com"]


def test_no_saved_login_asks_for_the_password_up_front(tmp_path, stub, sso):
    asked = []
    sign_in(
        "me@example.com", None, str(tmp_path / "t"), ask_password=lambda: asked.append(1) or "pw"
    )
    assert asked == [1]


@pytest.fixture
def saved(tmp_path, stub, sso, monkeypatch):
    """A login saved by an earlier push; the library's retry pauses skipped."""
    monkeypatch.setattr(garminconnect.time, "sleep", lambda seconds: None)
    folder = str(tmp_path / "tokens")
    sign_in("me@example.com", "pw", folder)
    return folder


def test_no_internet_is_not_mistaken_for_an_expired_login(saved, stub, sso):
    import requests

    def offline(method, url, headers=None, **kw):
        raise requests.ConnectionError("Max retries exceeded (Caused by NameResolutionError)")

    stub.request = offline
    asked = []
    with pytest.raises(PushError, match="could not reach Garmin Connect") as err:
        sign_in("me@example.com", None, saved, ask_password=lambda: asked.append(1) or "pw")
    assert not isinstance(err.value, NeedsPassword)
    assert asked == [] and len(sso) == 1 and saved_login(saved) is not None


def test_garmin_having_trouble_is_not_mistaken_for_an_expired_login(saved, stub):
    stub.request = lambda method, url, headers=None, **kw: StubResponse(503, {"message": "down"})
    with pytest.raises(PushError, match="having trouble") as err:
        sign_in("me@example.com", None, saved)
    assert not isinstance(err.value, NeedsPassword)


def test_a_saved_login_garmin_rejects_asks_for_the_password(saved, stub, monkeypatch):
    monkeypatch.setattr(garminconnect.client.Client, "_refresh_session", lambda self: None)
    stub.request = lambda method, url, headers=None, **kw: StubResponse(401, {"message": "no"})
    with pytest.raises(NeedsPassword, match="expired"):
        sign_in("me@example.com", None, saved)


def test_each_account_signs_in_with_its_own_saved_login(tmp_path, stub, sso, monkeypatch):
    # garminconnect signs in with any login it finds and ignores the email it
    # was given, so one athlete's saved login must never be offered for another.
    monkeypatch.setattr("gpp.client.DEFAULT_TOKEN_DIR", str(tmp_path))
    sign_in("alice@example.com", "alice-pw")
    with pytest.raises(NeedsPassword, match="no saved Garmin sign-in"):
        sign_in("bob@example.com", None)
    sign_in("bob@example.com", "bob-pw")
    assert sso == ["alice@example.com", "bob@example.com"]
    alice, bob = saved_login(email="alice@example.com"), saved_login(email="bob@example.com")
    assert alice is not None and bob is not None and alice != bob
    sign_in(" Alice@Example.com", None)  # her own login, however the address is typed
    assert len(sso) == 2


def test_the_account_name_comes_from_garmin(connected):
    client, _ = connected
    assert client.account == "A Runner"


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


def test_signout_deletes_the_saved_login(tmp_path, stub, sso, capsys):
    from gpp.cli import main

    folder = str(tmp_path / "tokens")
    sign_in("me@example.com", "pw", folder)
    assert main(["signout", "--token-dir", folder]) == 0
    assert saved_login(folder) is None
    assert "Signed out" in capsys.readouterr().out
