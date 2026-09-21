"""Streaming and usage plumbing, local presets, the MCP tools, and the watcher."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from gpp import mcp_server, providers, watch
from gpp.client import PushError
from gpp.plan import Plan, PlanError
from gpp.profile import Profile

# --- usage and cost ---------------------------------------------------------


def test_cost_for_known_anthropic_models_and_none_otherwise():
    assert providers.estimate_cost("claude-opus-5", 1_000_000, 0) == 5.0
    assert providers.estimate_cost("claude-sonnet-5", 0, 1_000_000) == 10.0
    assert providers.estimate_cost("gpt-5.2", 1000, 1000) is None
    assert providers.estimate_cost(None, 1, 1) is None


def test_usage_describe():
    u = providers.Usage(1200, 3400, 0.0912)
    assert u.describe() == "1,200 in / 3,400 out tokens (~$0.091)"
    assert providers.Usage(5, 6).describe() == "5 in / 6 out tokens"


def test_usage_from_sdk_shapes():
    anth = SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    u = providers.usage_from_anthropic(anth, "claude-haiku-4-5")
    assert (u.input_tokens, u.output_tokens) == (10, 20) and u.cost_usd == pytest.approx(
        (10 * 1 + 20 * 5) / 1e6
    )
    oa = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
    assert providers.usage_from_openai(oa, "gpt-x").cost_usd is None
    assert providers.usage_from_anthropic(SimpleNamespace(), "m") is None


def test_openai_stream_collection_feeds_deltas_and_keeps_usage():
    seen = []
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content='{"a"'))], usage=None
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=": 1}"))], usage=None
        ),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=4)),
    ]
    text, usage, _finish = providers.collect_openai_stream(chunks, seen.append)
    assert text == '{"a": 1}' and seen == ['{"a"', ": 1}"]
    assert usage.completion_tokens == 4


def test_local_presets_have_verdicts():
    verdicts = {p["verdict"] for p in providers.LOCAL_MODEL_PRESETS}
    assert verdicts <= {"good", "usable", "weak"}
    assert any(p["verdict"] == "weak" for p in providers.LOCAL_MODEL_PRESETS)


# --- MCP tools --------------------------------------------------------------


@pytest.fixture
def profile_on_disk(tmp_path, monkeypatch):
    path = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}}).save(
        tmp_path / "profile.toml"
    )
    monkeypatch.setattr(mcp_server, "find_profile", lambda athlete=None: path)
    return path


PLAN = {
    "plan": "p",
    "workouts": [
        {
            "name": "Q",
            "date": "2026-09-22",
            "role": "quality",
            "steps": [
                {"kind": "run", "duration": "30m", "target": {"type": "pace", "zone": "threshold"}}
            ],
        },
        {
            "name": "E",
            "date": "2026-09-24",
            "role": "easy",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
            ],
        },
    ],
}


def test_tool_list_describes_every_tool():
    names = {t["name"] for t in mcp_server.describe_tools()}
    assert {
        "check_plan",
        "oneline_workout",
        "zones",
        "push_plan",
        "pause_plan",
        "replan_missed",
        "generation_prompt",
    } == names
    assert all(t["description"] for t in mcp_server.describe_tools())


def test_check_tool_returns_report_and_dashboard(profile_on_disk):
    out = mcp_server.tool_check(json.dumps(PLAN))
    assert out["ok"] and "findings" in out["report"] and out["dashboard"]["sessions"] == 2


def test_oneline_and_zones_tools(profile_on_disk):
    w = mcp_server.tool_oneline("10m wu, 20m @ T, 5m cd", date="2026-09-22")["workout"]
    assert [s["kind"] for s in w["steps"]] == ["warmup", "run", "cooldown"]
    z = mcp_server.tool_zones()
    assert "threshold" in z["zones"] and z["profile"] == "T"


def test_pause_and_missed_tools(profile_on_disk):
    paused = mcp_server.tool_pause(json.dumps(PLAN), "2026-09-23", 7, "flu")
    assert paused["plan"]["workouts"][-1]["date"] == "2026-10-01"
    missed = mcp_server.tool_missed(json.dumps(PLAN), ["2026-09-24"])
    assert [w["name"] for w in missed["plan"]["workouts"]] == ["Q"]


def test_push_tool_defaults_to_dry_run_and_refuses_without_credentials(
    profile_on_disk, monkeypatch
):
    out = mcp_server.tool_push(json.dumps(PLAN))
    assert out["dry_run"] and out["workouts"] == 2
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    with pytest.raises(PushError, match="GARMIN_EMAIL"):
        mcp_server.tool_push(json.dumps(PLAN), dry_run=False)


def test_invalid_plan_json_is_a_plan_error(profile_on_disk):
    with pytest.raises(PlanError):
        mcp_server.tool_check(json.dumps({"plan": "p", "workouts": []}))


# --- watcher ----------------------------------------------------------------


def test_watch_once_fires_for_an_existing_file(tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(Plan.from_dict(PLAN).dumps(), encoding="utf-8")
    seen = []
    watch.watch(f, seen.append, once=True)
    assert seen == [f]


def test_watch_once_skips_a_missing_file(tmp_path):
    seen = []
    watch.watch(tmp_path / "nope.json", seen.append, once=True)
    assert seen == []


def test_today_helper_exists():
    assert dt.date.today()
