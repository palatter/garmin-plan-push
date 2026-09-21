"""Round 3 generation fixes: the prompt knows the date, corrections are real
conversation turns, truncation is detected and given more room."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from gpp import generate, providers
from gpp.profile import Profile
from gpp.prompt import build_prompt

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}})

GOOD = {
    "plan": "One week",
    "workouts": [
        {
            "name": "Easy",
            "date": "2026-09-29",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
            ],
        }
    ],
}


class Scripted:
    """Answers in order; records every call's history and user text."""

    def __init__(self, answers, stops=None, max_tokens=1000):
        self.name = "scripted"
        self.answers = list(answers)
        self.stops = list(stops or [])
        self.config = SimpleNamespace(max_tokens=max_tokens)
        self.last_usage = None
        self.last_stop = None
        self.calls = []

    def complete(self, system, user, schema, on_delta=None, history=None):
        self.calls.append({"user": user, "history": list(history or []), "system": system})
        self.last_stop = self.stops.pop(0) if self.stops else None
        return self.answers.pop(0)


def test_prompt_states_today_and_the_default_start():
    text = build_prompt(PROFILE, today=dt.date(2026, 9, 21))
    assert "Today is 2026-09-21 (Monday)" in text
    assert "the next Monday, 2026-09-28" in text
    # A Monday's "next Monday" is the following week, never today.
    assert "2026-09-21," not in text.split("next Monday")[1][:20]


def test_corrections_are_conversation_turns_with_the_full_previous_answer():
    provider = Scripted(["{not json", json.dumps(GOOD)])
    result = generate.generate_plan(
        provider, PROFILE, "one easy week", attempts=3, today=dt.date(2026, 9, 21)
    )
    assert result.attempts == 2
    second = provider.calls[1]
    assert [t["role"] for t in second["history"]] == ["user", "assistant"]
    assert second["history"][1]["content"] == "{not json"
    assert second["user"].startswith("Your previous answer was rejected")
    assert "Your previous answer was:" not in second["user"]


def test_truncated_answers_get_double_the_room_and_no_history():
    provider = Scripted(['{"plan": "cut', json.dumps(GOOD)], stops=["max_tokens", None])
    result = generate.generate_plan(
        provider, PROFILE, "one easy week", attempts=2, today=dt.date(2026, 9, 21)
    )
    assert result.attempts == 2
    assert provider.config.max_tokens == 2000
    assert provider.calls[1]["history"] == []
    assert "cut off at 1000 output tokens" in result.corrections[0]


def test_truncation_on_the_last_attempt_is_explained():
    provider = Scripted(['{"plan": "cut'], stops=["max_tokens"])
    with pytest.raises(providers.ProviderError, match="cut off at 1000 output tokens"):
        generate.generate_plan(provider, PROFILE, "x", attempts=1, today=dt.date(2026, 9, 21))


def test_manual_provider_renders_history_into_the_relayed_prompt():
    seen = {}

    def ask(prompt):
        seen["prompt"] = prompt
        return json.dumps(GOOD)

    manual = providers.ManualProvider(
        providers.ProviderConfig(name="paste", kind="manual"), ask=ask
    )
    manual.complete(
        "SYSTEM",
        "Fix it",
        None,
        history=[
            {"role": "user", "content": "first ask"},
            {"role": "assistant", "content": "{bad"},
        ],
    )
    assert "YOUR PREVIOUS ANSWER" in seen["prompt"]
    assert "{bad" in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("REQUEST\nFix it")


def test_stream_collector_keeps_the_finish_reason():
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="{}"), finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="length")],
            usage=None,
        ),
    ]
    text, _usage, finish = providers.collect_openai_stream(chunks, lambda _: None)
    assert text == "{}" and finish == "length"
    assert providers.was_truncated(SimpleNamespace(last_stop="length"))
    assert not providers.was_truncated(SimpleNamespace(last_stop="stop"))
