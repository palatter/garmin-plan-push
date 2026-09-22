"""Round 3 batch E, server side: education, ICS, heat, history, recap, the
manifest and service worker, and what the state carries for the UI."""

import datetime as dt
import json
import threading
import urllib.request

from gpp import analysis, education, formats
from gpp.history import History
from gpp.plan import Plan
from gpp.profile import Profile
from gpp.web import server
from gpp.web.server import App

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}, "hr": {"lthr": 170}})
PLAN = {
    "plan": "Block, with; punctuation",
    "workouts": [
        {
            "name": "Easy",
            "date": "2026-09-22",
            "role": "easy",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
            ],
        },
        {
            "name": "Threshold",
            "date": "2026-09-24",
            "role": "quality",
            "notes": "Controlled, not a race",
            "steps": [
                {"kind": "run", "duration": "30m", "target": {"type": "pace", "zone": "threshold"}}
            ],
        },
    ],
}


def test_education_lookup_aliases_and_topics():
    assert education.lookup("T")["title"] == "Threshold session"
    assert education.lookup("quality") is education.lookup("threshold")
    assert education.lookup("nope") is None
    assert education.topics_for("long", ["easy", "marathon"]) == ["long", "easy"]
    assert education.topics_for(None, ["repetition"]) == ["repetition"]
    text = education.render(education.lookup("taper"))
    assert "Bosquet" in text and "How it should feel" in text
    for entry in education.ENTRIES.values():
        assert {"title", "what", "feel", "evidence", "source"} <= set(entry)


def test_ics_export_has_one_event_per_session_and_folds_long_lines():
    text = formats.export_ics(Plan.from_dict(PLAN), PROFILE)
    assert text.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20260922" in text and "DTEND;VALUE=DATE:20260923" in text
    assert "X-WR-CALNAME:Block\\, with\\; punctuation" in text
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert "Controlled\\, not a race" in text.replace("\r\n ", "")


def test_recap_lines_compare_plan_and_run():
    planned = [
        {"date": "2026-09-22", "name": "Easy", "seconds": 2400, "metres": 8000},
        {"date": "2026-09-24", "name": "Threshold", "seconds": 1800, "metres": 7500},
    ]
    activities = [{"date": "2026-09-22", "seconds": 2520, "distance_m": 8400, "avg_hr": 145}]
    rows = analysis.recap_lines(planned, activities, lthr=170)
    assert rows[0]["status"] == "green" and "42 min vs 40 planned (105%)" in rows[0]["text"]
    assert "8.4 km at 5:00/km" in rows[0]["text"] and "on the planned pace" in rows[0]["text"]
    assert "85% of LTHR" in rows[0]["text"]
    assert rows[1]["status"] == "grey" and rows[1]["text"] == "No run recorded that day."


def app_with(tmp_path):
    path = PROFILE.save(tmp_path / "profile.toml")
    application = App(path)
    application.library_root = tmp_path / "library"
    application.recent_root = tmp_path / "recent"
    application.history_path = tmp_path / "history.sqlite"
    return application


def test_heat_endpoint_slows_every_zone(tmp_path):
    app = app_with(tmp_path)
    out = app.heat({"temp_c": 30, "humidity_pct": 70})
    assert out["band"] in ("adjust", "no-hard-running") and out["slowdown_spk"] > 0
    assert out["zones"]["threshold"]["fast"] > "4:00/km"  # slower than the cool-day bound
    cool = app.heat({"temp_c": 8, "humidity_pct": 50})
    assert cool["band"] == "normal" and cool["slowdown_spk"] == 0
    assert cool["zones"]["threshold"] != out["zones"]["threshold"]


def test_history_and_recap_endpoints_use_the_local_store(tmp_path):
    app = app_with(tmp_path)
    assert app.history({}) == {"weeks": []}
    assert app.recap({"plan": PLAN}) == {"recaps": []}
    store = History(app.history_path)
    day = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    store.upsert_activity(
        {
            "id": "1",
            "date": day,
            "name": "Run",
            "sport": "running",
            "distance_m": 8000,
            "seconds": 2400,
            "avg_hr": 140,
            "source": "test",
        }
    )
    store.close()
    assert app.history({"since_days": 30})["weeks"]
    past = dict(PLAN, workouts=[dict(PLAN["workouts"][0], date=day)])
    recaps = app.recap({"plan": past})["recaps"]
    assert recaps and recaps[0]["status"] == "green"


def test_state_carries_education_recent_and_describe_carries_agenda(tmp_path):
    app = app_with(tmp_path)
    state = app.state({})
    assert "threshold" in state["education"] and state["recent"] == []
    described = app.preview({"plan": PLAN})
    assert "this_week" in described["agenda"] and described["load_focus"]["shares"]
    assert app.state({})["recent"][0]["name"] == PLAN["plan"]
    ics = app.export({"plan": PLAN, "format": "ics"})
    assert ics["filename"].endswith(".ics") and ics["mime"] == "text/calendar"


def test_manifest_and_service_worker_are_served_from_the_root(tmp_path):
    app = app_with(tmp_path)
    httpd = server.QuietServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        manifest = urllib.request.urlopen(f"{base}/manifest.webmanifest")  # noqa: S310
        assert manifest.headers["Content-Type"].startswith("application/manifest+json")
        assert json.loads(manifest.read())["name"] == "Training Plans"
        worker = urllib.request.urlopen(f"{base}/sw.js")  # noqa: S310
        assert worker.headers["Service-Worker-Allowed"] == "/"
        assert b"CACHE" in worker.read()
    finally:
        httpd.shutdown()
        httpd.server_close()
