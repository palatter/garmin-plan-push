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


def test_a_decimal_heart_rate_is_rounded_and_the_page_is_told(app):
    saved = app.save_profile({"lthr": "165.5", "hr_max": "186"})
    assert (
        saved["note"] == "Saved Threshold HR 165.5 as 166: heart rates are whole beats per minute."
    )
    profile = Profile.load(app.profile_path)
    assert (profile.lthr, profile.hr_max) == (166, 186)
    assert "note" not in app.save_profile({"lthr": "166"})
    assert app.estimate_lthr({"hr_max": 186.4}) == app.estimate_lthr({"hr_max": "186"})
    with pytest.raises(AppError, match="Threshold HR must be a number"):
        app.save_profile({"lthr": "fast"})


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
    # A few days off: advice only, no empty plan to replace the one on the page.
    out = app.adapt_plan({"action": "return", "days_off": 4, "start": "2026-10-05"})
    assert out == {"advice": "Skip what was missed and carry on; a week off costs little."}

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
    # The page names library templates only: a path elsewhere on disk is not read.
    elsewhere = app.profile_path.parent / "elsewhere.json"
    elsewhere.write_text(json.dumps(PLAN), encoding="utf-8")
    with pytest.raises(AppError, match="no plan template named"):
        app.library_action({"action": "plan", "name": str(elsewhere), "start": "2026-10-05"})
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


def test_the_page_cannot_change_provider_settings(app):
    # Provider settings name an environment variable and a URL; a request
    # that could set them could send a key to any server. The page never
    # sends them, so a request that does is refused and nothing is written.
    before = app.profile_path.read_text(encoding="utf-8")
    entry = {"kind": "openai-compatible", "base_url": "https://evil.example/v1"}
    with pytest.raises(AppError, match=r"profile\.toml"):
        app.save_profile({"name": "X", "providers": {"providers": {"x": entry}}})
    assert app.profile_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "body",
    [
        {"goal_race": {"name": "R", "date": "2026-11-01", "priority": 'A"\n[ai.providers.x]'}},
        {"availability": {"days": ["sat"], "long_run_day": 'x"\n[ai]'}},
    ],
)
def test_settings_that_are_not_a_priority_or_a_weekday_are_refused_before_writing(app, body):
    before = app.profile_path.read_text(encoding="utf-8")
    with pytest.raises(AppError):
        app.save_profile(body)
    assert app.profile_path.read_text(encoding="utf-8") == before


def test_every_response_carries_a_content_policy_that_blocks_inline_script(app):
    import http.client

    httpd = server.QuietServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for path in ("/", "/static/app.js", "/nope"):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", path)
            policy = conn.getresponse().getheader("Content-Security-Policy") or ""
            conn.close()
            assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
            assert "unsafe-inline" not in policy
    finally:
        httpd.shutdown()
        httpd.server_close()


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


def _upcoming():
    """PLAN moved to start tomorrow, and a free date inside it, since only
    sessions from today on are offered for removal."""
    import datetime as dt

    start = dt.date.today() + dt.timedelta(days=1)
    plan = deepcopy(PLAN)
    for n, workout in enumerate(plan["workouts"]):
        workout["date"] = (start + dt.timedelta(days=2 * n)).isoformat()
    return plan, (start + dt.timedelta(days=1)).isoformat()


def _old_copy(date, title="Old"):
    from gpp.compile import plan_slug

    return {
        "workoutId": 9,
        "date": date,
        "title": title,
        "description": f"[gpp:{plan_slug(PLAN['plan'])}:0000abcd]",
    }


def test_check_first_reads_the_calendar_and_writes_nothing(app, monkeypatch):
    from gpp import client as client_module

    plan, free_day = _upcoming()
    cal = _Calendar([_old_copy(free_day)])
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    job = app.push({"plan": plan, "email": "me@example.com", "mode": "preview", "prune": True})
    snap = _finish(app, job["job"])
    assert snap["status"] == "done" and snap["result"]["preview"] is True
    rows = [(r["action"], r["date"], r["name"]) for r in snap["result"]["results"]]
    assert [a for a, _, _ in rows] == ["would-create"] * 3 + ["would-remove"]
    assert rows[-1][1:] == (free_day, "Old")
    assert cal.writes == []


def test_check_first_says_what_send_does_with_replace_unticked(app, monkeypatch):
    # Check first used to ignore the box: "would update in place", then Send
    # failed with "re-run with --replace", command-line wording.
    from gpp import client as client_module

    plan, _ = _upcoming()
    first_day = plan["workouts"][0]["date"]
    cal = _Calendar([_old_copy(first_day, plan["workouts"][0]["name"])])
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    monkeypatch.setattr(server, "save_receipt", lambda *a: type("R", (), {"name": "r"})())
    body = {"plan": plan, "email": "me@example.com", "replace": False}
    checked = _finish(app, app.push({**body, "mode": "preview"})["job"])["result"]["results"]
    sent = _finish(app, app.push(body)["job"])["result"]["results"]
    assert (checked[0]["action"], sent[0]["action"]) == ("would-fail", "failed")
    assert checked[0]["detail"] == sent[0]["detail"]
    assert "replacing is turned off" in sent[0]["detail"] and "--" not in sent[0]["detail"]


def test_send_with_prune_removes_orphans_and_says_which(app, monkeypatch):
    from gpp import client as client_module

    plan, free_day = _upcoming()
    cal = _Calendar([_old_copy(free_day)])
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    monkeypatch.setattr(server, "save_receipt", lambda *a: type("R", (), {"name": "r"})())
    job = app.push({"plan": plan, "email": "me@example.com", "prune": True})
    snap = _finish(app, job["job"])
    assert snap["status"] == "done"
    assert ("DELETE", "/workout-service/workout/9") in cal.writes
    removed = [r for r in snap["result"]["results"] if r["action"] == "removed"]
    assert [(r["date"], r["name"]) for r in removed] == [(free_day, "Old")]


def test_a_session_of_another_plan_with_the_same_first_letters_is_left_alone(app, monkeypatch):
    # Both plans used to share the slug "testblock": a send rewrote the other
    # plan's session on a shared day, and prune deleted its sessions nearby.
    from gpp import client as client_module
    from gpp.compile import plan_slug

    plan, free_day = _upcoming()
    other = f"[gpp:{plan_slug(PLAN['plan'] + ' - strength')}:0000abcd]"
    first_day = plan["workouts"][0]["date"]
    cal = _Calendar(
        [
            {"workoutId": 7, "date": first_day, "title": "Legs", "description": other},
            {"workoutId": 8, "date": free_day, "title": "Core", "description": other},
        ]
    )
    monkeypatch.setattr(client_module, "sign_in", lambda *a, **kw: cal.client)
    monkeypatch.setattr(server, "save_receipt", lambda *a: type("R", (), {"name": "r"})())
    snap = _finish(app, app.push({"plan": plan, "email": "me@example.com", "prune": True})["job"])
    assert snap["status"] == "done"
    assert [r["action"] for r in snap["result"]["results"]] == ["created"] * 3
    assert not [w for w in cal.writes if w[0] in ("PUT", "DELETE")]


def test_one_send_at_a_time(app, monkeypatch):
    # The dialog could be closed and reopened mid-send, and a second Send
    # pushed every session again alongside the first.
    import threading

    from gpp import client as client_module

    cal = _Calendar([])
    release = threading.Event()

    def slow_sign_in(*a, **kw):
        release.wait(5)
        return cal.client

    monkeypatch.setattr(client_module, "sign_in", slow_sign_in)
    monkeypatch.setattr(server, "save_receipt", lambda *a: type("R", (), {"name": "r"})())
    first = app.push({"plan": PLAN, "email": "me@example.com"})
    for body in ({"mode": "preview"}, {}):
        with pytest.raises(AppError, match="already running") as err:
            app.push({"plan": PLAN, "email": "me@example.com", **body})
        assert err.value.status == 409
    release.set()
    assert _finish(app, first["job"])["status"] == "done"
    again = app.push({"plan": PLAN, "email": "me@example.com", "mode": "preview"})
    assert _finish(app, again["job"])["status"] == "done"


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


def test_a_profile_with_a_typo_is_reported_and_never_saved_over(app):
    # One unclosed quote used to read as "no profile": the first-run setup
    # showed, and its Save wrote a fresh file over every hand-edited section.
    path = app.profile_path
    path.write_text(path.read_text() + '\nnote = "hilly\n', encoding="utf-8")
    before = path.read_text()
    with pytest.raises(AppError, match="could not be read") as err:
        app.state({})
    assert err.value.status == 409 and str(path) in str(err.value)
    with pytest.raises(AppError, match="could not be read"):
        app.save_profile({"name": "Pat", "threshold": "4:00/km"})
    assert path.read_text() == before


def test_no_profile_yet_still_opens_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "find_profile", lambda *a: None)
    assert App(None).state({}) == {"configured": False}


def test_a_send_keeps_the_password_only_when_the_box_is_ticked(app, monkeypatch):
    from gpp import client as client_module

    plan, _ = _upcoming()
    cal = _Calendar([])
    seen = []

    def sign_in(email, password, **kw):
        seen.append((password, kw["remember"]))
        cal.client.remembered = True if kw["remember"] and password else None
        return cal.client

    monkeypatch.setattr(client_module, "sign_in", sign_in)
    body = {"plan": plan, "email": "me@example.com", "password": "pw", "mode": "preview"}
    kept = _finish(app, app.push({**body, "remember": True})["job"])
    not_kept = _finish(app, app.push(body)["job"])
    assert seen == [("pw", True), ("pw", False)]
    assert any("password is kept" in line for line in kept["log"])
    assert not any("password" in line for line in not_kept["log"])


def test_the_page_is_told_what_the_keychain_is_called(app, monkeypatch):
    import keyring
    from keyring.backends import fail

    from gpp import keychain

    state = app.state({})
    assert state["keychain"] == keychain.where() and state["garmin_saved_password"] is False
    keychain.save_garmin_password("me@example.com", "pw")
    assert app.state({})["garmin_saved_password"] is True
    keyring.set_keyring(fail.Keyring())
    assert app.state({})["keychain"] is None


def test_signout_forgets_the_kept_passwords(app, monkeypatch, tmp_path):
    from gpp import keychain

    monkeypatch.setattr("gpp.client.DEFAULT_TOKEN_DIR", str(tmp_path))
    keychain.save_garmin_password("me@example.com", "pw")
    assert app.garmin_signout({}) == {"signed_out": True}
    assert keychain.garmin_password("me@example.com") is None


def test_a_key_saved_from_the_page_goes_to_the_keychain_and_never_comes_back(
    app, monkeypatch, keychain_backend
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ICU_API_KEY", raising=False)
    before = {k["name"]: k for k in app.state({})["keys"]}
    assert set(before) >= {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ICU_API_KEY"}
    assert before["ICU_API_KEY"]["for"] == ["intervals.icu"]
    assert before["ANTHROPIC_API_KEY"]["source"] is None
    saved = app.save_key({"name": "ANTHROPIC_API_KEY", "value": "  sk-ant-pasted \n"})
    assert keychain_backend.entries[("garmin-plan-push", "ANTHROPIC_API_KEY")] == "sk-ant-pasted"
    assert "sk-ant-pasted" not in json.dumps(saved) + json.dumps(app.state({}))
    claude = next(p for p in app.state({})["providers"] if p["key_env"] == "ANTHROPIC_API_KEY")
    assert claude["key_present"] is True
    assert {k["name"]: k["source"] for k in saved["keys"]}["ANTHROPIC_API_KEY"] == "keychain"
    removed = app.save_key({"name": "ANTHROPIC_API_KEY", "remove": True})
    assert removed["removed"] is True and keychain_backend.entries == {}


@pytest.mark.parametrize(
    "body",
    [
        {"name": "PATH", "value": "x"},
        {"name": "garmin:me@example.com", "value": "x"},
        {"name": "ANTHROPIC_API_KEY", "value": ""},
        {"name": "ANTHROPIC_API_KEY", "value": "two words"},
        {"name": "ANTHROPIC_API_KEY", "value": "x" * 5000},
    ],
)
def test_the_page_can_only_save_a_key_the_app_reads(app, body, keychain_backend):
    with pytest.raises(AppError):
        app.save_key(body)
    assert keychain_backend.entries == {}


def test_saving_a_key_with_no_keychain_says_so(app):
    import keyring
    from keyring.backends import fail

    keyring.set_keyring(fail.Keyring())
    with pytest.raises(AppError, match="no keychain"):
        app.save_key({"name": "ANTHROPIC_API_KEY", "value": "sk"})
