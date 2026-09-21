"""Round 3 batch B: rationale, envelope, intensity choice, continuation,
structured-output ladders, usage totals, enrichment, chunked generation,
post-race recovery, race seeds, and the eval harness."""

import datetime as dt
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gpp import chunked, enrich, evaluate, generate, library, providers
from gpp.checks import check
from gpp.plan import Plan
from gpp.profile import Profile, ProfileError
from gpp.prompt import build_prompt

TODAY = dt.date(2026, 9, 21)


def profile(**extra):
    data = {"name": "T", "pace": {"threshold": "4:00/km"}}
    data.update(extra)
    return Profile.from_dict(data)


def run(name, date, minutes, zone="easy", role=None, notes=None):
    out = {
        "name": name,
        "date": date,
        "steps": [
            {"kind": "run", "duration": f"{minutes}m", "target": {"type": "pace", "zone": zone}}
        ],
    }
    if role:
        out["role"] = role
    if notes:
        out["notes"] = notes
    return out


def plan_of(workouts, **extra):
    return Plan.from_dict({"plan": "Block", "workouts": workouts, **extra})


class Scripted:
    def __init__(self, answers):
        self.name = "scripted"
        self.answers = list(answers)
        self.config = SimpleNamespace(max_tokens=4000)
        self.last_usage = providers.Usage(100, 200, 0.01)
        self.last_stop = None
        self.calls = []

    def complete(self, system, user, schema, on_delta=None, history=None):
        self.calls.append({"user": user, "history": list(history or []), "schema": schema})
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


# --- summary, profile, prompt ---------------------------------------------------


def test_plan_summary_round_trips():
    p = plan_of([run("Easy", "2026-09-29", 40)], summary="Two easy weeks then a test.")
    assert Plan.from_dict(p.to_dict()).summary == "Two easy weeks then a test."


def test_intensity_distribution_is_parsed_written_and_validated(tmp_path):
    p = profile(athlete={"intensity_distribution": "singles"})
    assert p.intensity_distribution == "singles"
    path = p.save(tmp_path / "p.toml")
    assert Profile.load(path).intensity_distribution == "singles"
    assert 'intensity_distribution = "singles"' in path.read_text(encoding="utf-8")
    with pytest.raises(ProfileError, match="intensity_distribution"):
        profile(athlete={"intensity_distribution": "zigzag"})


def test_prompt_carries_numbers_for_the_athletes_level():
    p = profile(
        athlete={"longest_recent_run_km": 16, "recent_weekly_km": 40},
        goal_race={"name": "Half", "date": "2026-11-22", "distance": "half marathon"},
        availability={"sessions_per_week": 5},
    )
    text = build_prompt(p, today=TODAY)
    assert "LEVEL ENVELOPE" in text
    assert "start at 36-44 km" in text
    assert "no longer than 18 km" in text and "never past 26 km" in text
    assert "Sessions per week: 5" in text
    assert "Pyramidal distribution" in text
    assert '"summary"' in text


def test_prompt_states_the_chosen_intensity_model():
    assert "Sub-threshold singles" in build_prompt(
        profile(athlete={"intensity_distribution": "singles"}), today=TODAY
    )
    assert "Polarized distribution" in build_prompt(
        profile(athlete={"intensity_distribution": "polarized"}), today=TODAY
    )


def test_prompt_continues_from_the_previous_block():
    previous = plan_of(
        [
            run("Easy", "2026-09-01", 40),
            run("Long", "2026-09-06", 100, role="long"),
            run("Easy", "2026-09-08", 45),
        ]
    )
    text = build_prompt(profile(), today=TODAY, previous=previous)
    assert "PREVIOUS BLOCK" in text and '"Block"' in text and "2026-09-08" in text
    assert "do not reset" in text


def test_singles_and_polarized_change_the_checks():
    hard = [
        run(f"T{i}", d, 40, "threshold", role="quality")
        for i, d in enumerate(["2026-09-22", "2026-09-24"])
    ]
    easy = [
        run(f"E{i}", d, 60)
        for i, d in enumerate(["2026-09-23", "2026-09-26", "2026-09-29", "2026-10-01"])
    ]
    codes = {
        f.code
        for f in check(
            plan_of(hard + easy), profile(athlete={"intensity_distribution": "singles"})
        ).findings
    }
    assert "singles-too-hard" in codes
    codes = {
        f.code
        for f in check(
            plan_of(hard + easy), profile(athlete={"intensity_distribution": "polarized"})
        ).findings
    }
    assert "no-middle-gear" not in codes


# --- usage and structured outputs --------------------------------------------------


def test_usage_adds_up_and_prices_cache_reads():
    total = providers.Usage(100, 50, 0.01, 10, 0) + providers.Usage(200, 50, 0.02)
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens) == (300, 100, 10)
    assert total.cost_usd == pytest.approx(0.03)
    assert "from cache" in total.describe()
    full = providers.estimate_cost("claude-sonnet-5", 1_000_000, 0)
    cached = providers.estimate_cost("claude-sonnet-5", 0, 0, cache_read=1_000_000)
    assert cached == pytest.approx(full * 0.1)


def test_anthropic_ladder_falls_back_to_plain_when_schema_kwargs_are_rejected(monkeypatch):
    seen = []

    class Messages:
        def create(self, **req):
            seen.append(sorted(req))
            if "output_config" in req:
                raise TypeError("unexpected keyword argument 'output_config'")
            if "output_format" in req:
                raise RuntimeError("Error code: 400 - invalid_request_error: output_format")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"ok": true}')],
                usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=3),
                stop_reason="end_turn",
            )

    class Anthropic:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = providers.AnthropicProvider(
        providers.ProviderConfig(name="claude", kind="anthropic")
    )
    assert provider.complete("sys", "user", {"type": "object"}) == '{"ok": true}'
    assert provider.last_mode == "plain" and len(seen) == 3
    assert provider.last_usage.cache_read_tokens == 3
    assert isinstance(seen[0], list) and "system" in seen[-1]


def test_openai_ladder_steps_down_from_json_schema_to_json_object(monkeypatch):
    seen = []

    class Completions:
        def create(self, **req):
            seen.append((req.get("stream", False), (req.get("response_format") or {}).get("type")))
            fmt = (req.get("response_format") or {}).get("type")
            if req.get("stream") or fmt == "json_schema":
                raise RuntimeError("Error code: 400 - unsupported")
            choice = SimpleNamespace(
                message=SimpleNamespace(content='{"ok": 1}'), finish_reason="stop"
            )
            return SimpleNamespace(
                choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)
            )

    class OpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    provider = providers.OpenAICompatibleProvider(
        providers.ProviderConfig(name="chatgpt", kind="openai")
    )
    assert (
        provider.complete("sys", "user", {"type": "object"}, on_delta=lambda _: None) == '{"ok": 1}'
    )
    assert provider.last_mode == "json_object"
    assert seen == [
        (True, "json_schema"),
        (False, "json_schema"),
        (True, "json_object"),
        (False, "json_object"),
    ]


def test_generation_sums_usage_over_attempts():
    provider = Scripted(["{bad", {"plan": "P", "workouts": [run("Easy", "2026-09-29", 40)]}])
    result = generate.generate_plan(provider, profile(), "one week", attempts=2, today=TODAY)
    assert result.usage.input_tokens == 200 and result.usage.output_tokens == 400


# --- enrichment ----------------------------------------------------------------------


def long_block():
    return plan_of(
        [
            run("Easy", "2026-09-22", 40),
            run("Long 1", "2026-09-27", 95, role="long"),
            run("Threshold", "2026-09-29", 40, "threshold", role="quality"),
            run("Easy 2", "2026-10-01", 40),
            run("Long 2", "2026-10-04", 110, role="long"),
            run("Long 3", "2026-10-11", 120, role="long", notes="Fuel as on race day"),
            run("Long 4", "2026-10-18", 130, role="long"),
        ],
        races=[{"name": "M", "date": "2026-11-01", "priority": "A", "distance": "marathon"}],
    )


def test_fuelling_cues_progress_and_respect_existing_notes():
    result = enrich.add_fuelling_cues(long_block(), profile())
    notes = {w.name: w.notes or "" for w in result.plan.workouts}
    assert "30 g/h" in notes["Long 1"] and "45 g/h" in notes["Long 2"]
    assert notes["Long 3"] == "Fuel as on race day"
    assert "75 g/h" in notes["Long 4"]


def test_heat_block_marks_easy_sessions_in_the_last_two_weeks():
    result = enrich.add_heat_block(long_block(), dt.date(2026, 11, 1), profile())
    heated = [w.name for w in result.plan.workouts if w.notes and "Heat:" in w.notes]
    assert heated == ["Long 4"]
    assert any("checklist" in r for r in result.reasons)


def test_strength_lands_on_easy_days_away_from_key_sessions():
    result = enrich.place_strength(long_block(), profile(), per_week=2)
    strength = [w for w in result.plan.workouts if w.sport == "strength"]
    assert strength
    keys = {w.date for w in long_block().workouts if (w.role or "") in ("quality", "long")}
    for w in strength:
        assert w.date not in keys and (w.date + dt.timedelta(days=1)) not in keys


def test_durability_notes_on_alternate_marathon_long_runs():
    result = enrich.add_durability_notes(long_block(), profile())
    noted = [w.name for w in result.plan.workouts if w.notes and "Durability" in w.notes]
    assert noted == ["Long 2"]  # Long 4 is inside the final 14 days
    assert (
        "Not a marathon"
        in enrich.add_durability_notes(plan_of([run("E", "2026-09-22", 40)]), profile()).reasons[0]
    )


def test_cadence_cue_only_with_an_overuse_history():
    p = profile(athlete={"injuries": ["left shin splints last spring"]})
    result = enrich.add_cadence_cues(long_block(), p)
    cued = [w.name for w in result.plan.workouts if w.steps[0].note == enrich.CADENCE_CUE]
    assert cued == ["Easy", "Easy 2"]
    assert "No overuse" in enrich.add_cadence_cues(long_block(), profile()).reasons[0]


def test_enrich_composes():
    result = enrich.enrich(
        long_block(), profile(), strength_per_week=1, heat_race=dt.date(2026, 11, 1)
    )
    assert any(w.sport == "strength" for w in result.plan.workouts)
    assert len(result.reasons) >= 4
    Plan.from_dict(result.plan.to_dict())  # still a valid plan


# --- chunked generation -------------------------------------------------------------


def test_chunked_generation_asks_for_an_outline_then_each_phase():
    outline = {
        "plan": "Two phases",
        "summary": "Base then build.",
        "phases": [
            {"name": "Base", "phase": "base", "start": "2026-09-28", "weeks": 1, "weekly_km": [30]},
            {
                "name": "Build",
                "phase": "build",
                "start": "2026-10-05",
                "weeks": 1,
                "weekly_km": [34],
            },
        ],
    }
    base = {
        "plan": "Two phases",
        "workouts": [run("Easy", "2026-09-29", 40), run("Long", "2026-10-04", 80, role="long")],
    }
    build = {
        "plan": "Two phases",
        "workouts": [run("Easy", "2026-10-06", 40), run("Long", "2026-10-11", 85, role="long")],
    }
    provider = Scripted([outline, base, build])
    result = chunked.generate_plan_chunked(provider, profile(), "two weeks", today=TODAY)
    assert len(result.plan.workouts) == 4 and result.plan.summary == "Base then build."
    assert [w.phase for w in result.plan.weeks] == ["base", "build"]
    assert "PREVIOUS PHASE" in provider.calls[2]["user"]
    assert provider.calls[0]["schema"] is chunked.OUTLINE_SCHEMA


def test_chunked_generation_rejects_sessions_outside_the_phase():
    outline = {
        "plan": "One",
        "phases": [{"name": "Base", "phase": "base", "start": "2026-09-28", "weeks": 1}],
    }
    wrong = {"plan": "One", "workouts": [run("Easy", "2026-10-13", 40)]}
    right = {"plan": "One", "workouts": [run("Easy", "2026-09-29", 40)]}
    provider = Scripted([outline, wrong, right])
    result = chunked.generate_plan_chunked(provider, profile(), "one week", today=TODAY)
    assert len(result.plan.workouts) == 1
    assert any("outside" in c for c in result.corrections)


def test_outline_must_be_contiguous_mondays():
    with pytest.raises(Exception, match="Monday"):
        chunked._validate_outline(
            {
                "plan": "x",
                "phases": [{"name": "a", "phase": "base", "start": "2026-09-29", "weeks": 1}],
            },
            TODAY,
        )


# --- library and recovery -----------------------------------------------------------


def test_race_specific_seeds_carry_their_race_and_origin():
    listing = {w["slug"]: w for w in library.list_workouts()}
    assert listing["10k-6x800"]["race"] == "10k" and "Daniels" in listing["10k-6x800"]["origin"]
    assert library.load_workout("marathon-mp-long", dt.date(2026, 10, 4)).role == "long"


def test_post_race_recovery_is_five_easy_weeks_after_a_marathon():
    plan = library.post_race_recovery(dt.date(2026, 10, 25), "marathon")
    dates = [w.date for w in plan.sorted_workouts()]
    assert dates[0] == dt.date(2026, 10, 29) and dates[-1] == dt.date(2026, 11, 29)
    assert all(w.phase == "recovery" for w in plan.workouts)
    assert sum(1 for w in plan.workouts if w.role == "quality") == 1
    short = library.post_race_recovery(dt.date(2026, 10, 25), "10k")
    assert len({w.date.isocalendar()[1] for w in short.workouts}) == 3


# --- eval harness ---------------------------------------------------------------------


def test_eval_runs_cases_and_reports_a_verdict():
    good = {
        "plan": "P",
        "workouts": [run("Easy", "2026-09-29", 40), run("Long", "2026-10-04", 60, role="long")],
    }
    results = evaluate.evaluate(
        lambda: Scripted([good]), evaluate.BENCH_CASES, attempts=2, today=TODAY
    )
    assert [r.ok for r in results] == [True, True]
    assert results[0].first_try_valid and results[0].tokens_in == 100
    assert evaluate.verdict(results) == "good"
    assert "2/2 plans passed" in evaluate.table(results)
    assert evaluate.load_cases(None)[0]["name"] == "beginner-10k"
