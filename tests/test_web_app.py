"""The web app's endpoints, driven directly and once over HTTP."""

import json
import threading
import urllib.error
import urllib.request
from copy import deepcopy

import pytest

from gpp.profile import Profile
from gpp.providers import is_manual, load_providers
from gpp.web import server
from gpp.web.server import App, AppError

PLAN = {
    "plan": "Test block",
    "workouts": [
        {
            "name": "Threshold",
            "date": "2026-09-22",
            "role": "quality",
            "steps": [
                {"kind": "warmup", "duration": "10m"},
                {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "threshold"}},
                {"kind": "cooldown", "duration": "10m"},
            ],
        },
        {
            "name": "Easy",
            "date": "2026-09-24",
            "role": "easy",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
            ],
        },
        {
            "name": "Long",
            "date": "2026-09-27",
            "role": "long",
            "steps": [
                {"kind": "run", "duration": "90m", "target": {"type": "pace", "zone": "easy"}}
            ],
        },
    ],
}


@pytest.fixture
def app(tmp_path):
    profile = Profile.from_dict(
        {"name": "T", "pace": {"threshold": "4:00/km"}, "power": {"cp": 300}}
    )
    path = profile.save(tmp_path / "profile.toml")
    application = App(path)
    application.library_root = tmp_path / "library"
    application.recent_root = tmp_path / "recent"
    return application


def test_state_and_profile_round_trip_the_onboarding_answers(app):
    app.save_profile(
        {
            "name": "T",
            "threshold": "4:00/km",
            "injuries": "left achilles\n\n",
            "constraints": ["no hills"],
            "instructions": "keep Mondays easy",
            "longest_recent_run_km": "18",
            "recent_weekly_km": 40,
            "availability": {
                "days": ["mon", "wed", "sat"],
                "sessions_per_week": 4,
                "long_run_day": "sat",
                "weekday_max_minutes": None,
            },
            "goal_race": {
                "name": "City 10k",
                "date": "2026-11-15",
                "distance": "10k",
                "goal_time": "42:00",
            },
        }
    )
    athlete = app.state({})["athlete"]
    assert athlete["injuries"] == ["left achilles"]
    assert athlete["constraints"] == ["no hills"]
    assert athlete["instructions"] == "keep Mondays easy"
    assert athlete["longest_recent_run_km"] == 18.0
    assert athlete["availability"]["days"] == ["mon", "wed", "sat"]
    assert athlete["availability"]["long_run_day"] == "sat"
    assert athlete["goal_race"]["date"] == "2026-11-15"

    # Clearing the form removes the sections rather than leaving empty tables.
    app.save_profile({"availability": {"days": []}, "goal_race": {"date": ""}, "injuries": ""})
    athlete = app.state({})["athlete"]
    assert athlete["availability"] is None
    assert athlete["goal_race"] is None
    assert athlete["injuries"] == []
    assert athlete["constraints"] == ["no hills"]  # not in the body, so untouched


def test_save_profile_keeps_sections_the_form_does_not_own(app):
    app.save_profile({"name": "Renamed"})
    saved = Profile.load(app.profile_path)
    assert saved.name == "Renamed"
    assert saved.power_cp == 300
    assert saved.threshold_pace == 240.0


def test_save_profile_rejects_a_non_number(app):
    with pytest.raises(AppError, match="must be a number"):
        app.save_profile({"longest_recent_run_km": "far"})


def test_preview_carries_index_hard_time_and_step_notes(app):
    plan = deepcopy(PLAN)
    plan["workouts"][0]["steps"][1]["note"] = "tall posture"
    out = app.preview({"plan": plan})
    assert [w["index"] for w in out["workouts"]] == [0, 1, 2]
    assert out["workouts"][0]["hard_seconds"] > 0
    assert out["workouts"][0]["load"] > 0
    assert [b["note"] for b in out["workouts"][0]["timeline"]] == [None, "tall posture", None]
    assert out["report"]["ok"]
    assert out["dashboard"]["sessions"] == 3


def test_oneline_endpoint_parses_a_sentence(app):
    out = app.oneline({"text": "10m wu, 20m @ T, 5m cd", "date": "2026-09-22", "name": "Q"})
    assert [s["kind"] for s in out["workout"]["steps"]] == ["warmup", "run", "cooldown"]
    with pytest.raises(AppError):
        app.oneline({"text": ""})


def test_diff_endpoint_reports_a_moved_session(app):
    after = deepcopy(PLAN)
    after["workouts"][1]["date"] = "2026-09-25"
    assert app.diff({"before": PLAN, "after": after})["changes"]
    assert app.diff({"before": PLAN, "after": PLAN}) == {"meta": [], "changes": []}


def test_adapt_endpoint_missed_pause_and_return(app):
    out = app.adapt_plan({"plan": PLAN, "action": "missed", "dates": ["2026-09-24"]})
    assert "adaptation" in out
    assert out["workouts"]

    out = app.adapt_plan({"plan": PLAN, "action": "pause", "start": "2026-09-23", "days": 7})
    assert out["json"]["workouts"][-1]["date"] == "2026-10-04"

    out = app.adapt_plan({"action": "return", "days_off": 21, "start": "2026-10-05"})
    assert out["adaptation"]["reasons"]
    assert out["workouts"]

    with pytest.raises(AppError, match="pause, missed or return"):
        app.adapt_plan({"plan": PLAN, "action": "shrug"})
    with pytest.raises(AppError, match="date like"):
        app.adapt_plan({"plan": PLAN, "action": "pause", "start": "someday", "days": 3})


def test_export_endpoint_formats(app):
    out = app.export({"plan": PLAN, "format": "json"})
    assert out["filename"] == "test-block.json"
    assert json.loads(out["text"])["plan"] == "Test block"

    out = app.export({"plan": PLAN, "format": "share"})
    assert out["filename"].endswith(".share.json")
    assert json.loads(out["text"])["gpp_share"]

    for fmt, ext in (("icu", "txt"), ("zwo", "zwo"), ("mrc", "mrc"), ("erg", "erg")):
        out = app.export({"plan": PLAN, "format": fmt, "index": 0})
        assert out["text"].strip()
        assert out["filename"] == f"threshold-2026-09-22.{ext}"

    with pytest.raises(AppError, match="index"):
        app.export({"plan": PLAN, "format": "zwo"})
    with pytest.raises(AppError, match="unknown format"):
        app.export({"plan": PLAN, "format": "tcx", "index": 0})


def test_library_endpoint_round_trip(app):
    listing = app.library_action({"action": "list"})
    assert any(w["source"] == "built-in" for w in listing["workouts"])
    assert listing["plans"] == []

    saved = app.library_action(
        {"action": "save_workout", "workout": PLAN["workouts"][0], "name": "My Q"}
    )
    assert any(w["name"] == "My Q" and w["source"] == "mine" for w in saved["workouts"])
    loaded = app.library_action({"action": "workout", "name": "my-q", "date": "2026-10-06"})
    assert loaded["workout"]["date"] == "2026-10-06"
    assert loaded["workout"]["name"] == "My Q"

    plans = app.library_action({"action": "save_plan", "plan": PLAN})["plans"]
    assert plans and plans[0]["workouts"] == 3
    opened = app.library_action({"action": "plan", "name": plans[0]["slug"], "start": "2026-10-05"})
    assert opened["workouts"][0]["date"] == "2026-10-06"  # the Tuesday of the week of Mon 5 Oct

    with pytest.raises(AppError, match="no workout named"):
        app.library_action({"action": "workout", "name": "nope", "date": "2026-10-06"})
    with pytest.raises(AppError, match="unknown library action"):
        app.library_action({"action": "dance"})


def test_regenerate_refuses_a_manual_provider(app):
    configs, _ = load_providers(Profile.load(app.profile_path).raw)
    manual = next(name for name, cfg in configs.items() if is_manual(cfg))
    with pytest.raises(AppError, match="API provider"):
        app.regenerate(
            {"plan": PLAN, "date": "2026-09-22", "instruction": "easier", "provider": manual}
        )
    with pytest.raises(AppError, match="unknown provider"):
        app.regenerate(
            {"plan": PLAN, "date": "2026-09-22", "instruction": "easier", "provider": "nope"}
        )


def test_http_layer_requires_the_token_and_serves_the_assets(app):
    httpd = server.QuietServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        html = urllib.request.urlopen(f"{base}/").read().decode()  # noqa: S310
        assert app.token in html
        assert "review.js" in html
        for asset in ("app.js", "review.js", "review.css", "style.css"):
            assert urllib.request.urlopen(f"{base}/static/{asset}").status == 200  # noqa: S310

        anonymous = urllib.request.Request(  # noqa: S310
            f"{base}/api/state",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(anonymous)  # noqa: S310
        assert denied.value.code == 403

        signed = urllib.request.Request(  # noqa: S310
            f"{base}/api/state",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-GPP-Token": app.token},
        )
        assert json.loads(urllib.request.urlopen(signed).read())["configured"] is True  # noqa: S310
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pages_refuse_to_be_framed_and_bad_lengths_are_rejected(app):
    import http.client

    httpd = server.QuietServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        assert page.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/state")
        conn.putheader("X-GPP-Token", app.token)
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        assert conn.getresponse().status == 400
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"kind": "openai-compatible", "api_key_env": "AWS_SECRET_ACCESS_KEY"}, "api_key_env"),
        ({"kind": "openai-compatible", "base_url": "http://evil.example/v1"}, "base_url"),
    ],
)
def test_profile_providers_cannot_aim_a_secret_at_another_host(app, entry, message):
    from gpp.providers import ProviderError

    with pytest.raises(ProviderError, match=message):
        app.save_profile({"providers": {"providers": {"x": entry}}})


def test_profile_providers_accept_https_and_local_models(app):
    app.save_profile(
        {
            "providers": {
                "providers": {
                    "hosted": {
                        "kind": "openai-compatible",
                        "base_url": "https://api.example.com/v1",
                        "api_key_env": "EXAMPLE_API_KEY",
                    },
                    "local": {"kind": "ollama", "base_url": "http://localhost:11434/v1"},
                }
            }
        }
    )


def _finish(app, job_id):
    import time

    for _ in range(200):
        snap = app.jobs.get(job_id).snapshot()
        if snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.02)
    raise AssertionError("job did not finish")


class _Calendar:
    """A signed-in client over a fake calendar, recording writes."""

    def __init__(self, items):
        from gpp.client import GarminClient

        self.writes = []
        self.client = GarminClient("me@example.com")
        self.client.transport = "fake"
        self.client._request = self._request
        self.items = items

    def _request(self, method, path, **kw):
        if method == "GET" and "calendar-service" in path:
            return {"calendarItems": self.items}
        self.writes.append((method, path))
        return {"workoutId": 1} if method == "POST" else {}


def test_check_first_reads_the_calendar_and_writes_nothing(app, monkeypatch):
    from gpp import client as client_module

    cal = _Calendar(
        [
            {
                "workoutId": 9,
                "date": "2026-09-30",
                "title": "Old",
                "description": "[gpp:testblock:0000abcd]",
            }
        ]
    )
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    job = app.push({"plan": PLAN, "email": "me@example.com", "mode": "preview", "prune": True})
    snap = _finish(app, job["job"])
    assert snap["status"] == "done" and snap["result"]["preview"] is True
    actions = [r["action"] for r in snap["result"]["results"]]
    assert actions == ["would-create", "would-create", "would-create", "would-remove"]
    assert cal.writes == []


def test_send_with_prune_removes_orphans(app, monkeypatch):
    from gpp import client as client_module

    cal = _Calendar(
        [
            {
                "workoutId": 9,
                "date": "2026-09-30",
                "title": "Old",
                "description": "[gpp:testblock:0000abcd]",
            }
        ]
    )
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    monkeypatch.setattr(server, "save_receipt", lambda *a: type("R", (), {"name": "r"})())
    job = app.push({"plan": PLAN, "email": "me@example.com", "prune": True})
    snap = _finish(app, job["job"])
    assert snap["status"] == "done"
    assert ("DELETE", "/workout-service/workout/9") in cal.writes


def test_signout_forgets_every_saved_login(app, monkeypatch, tmp_path):
    from gpp.client import token_dir

    monkeypatch.setattr("gpp.client.DEFAULT_TOKEN_DIR", str(tmp_path))
    for email in ("a@example.com", "b@example.com"):
        folder = token_dir(email=email)
        folder.mkdir(parents=True)
        (folder / "garmin_tokens.json").write_text("{}", encoding="utf-8")
    (tmp_path / "garmin_tokens.json").write_text("{}", encoding="utf-8")  # where 0.2.1 kept it
    assert app.state({})["garmin_saved_login"] is True
    assert app.garmin_signout({}) == {"signed_out": True}
    assert app.state({})["garmin_saved_login"] is False
    assert not list(tmp_path.rglob("garmin_tokens.json"))
