"""Contract tests against the real anthropic and openai libraries, offline.

The other provider tests replace the vendor SDK with a stand-in, which is how
a request the real library refuses before sending anything (a plain Claude
call with the default 32,000 max tokens) went unnoticed. These build the real
client and swap only its HTTP transport, so what is checked is what the
library actually sends and how it reads the answer. Nothing here reaches a
vendor, so a model the vendor has retired, or a parameter it has stopped
accepting, still needs `gpp doctor --ping` against the real service.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")

from gpp.providers import (  # noqa: E402
    AnthropicProvider,
    ProviderConfig,
)

Responder = Callable[[dict], "httpx2.Response"]


def sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    lines = []
    for name, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        lines.append((f"event: {name}\n" if name else "") + f"data: {payload}\n\n")
    return "".join(lines).encode()


def claude_stream(text: str, stop: str = "end_turn") -> httpx2.Response:
    """A Messages API stream carrying `text` as one text block."""
    half = len(text) // 2
    events: list[tuple[str | None, dict | str]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for piece in (text[:half], text[half:]):
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))


class Wire:
    """Stands in for the network: records each request body, answers from a script."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.answers: list[Responder] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if not self.answers:
            raise AssertionError(f"unexpected request to {request.url}")
        return self.answers.pop(0)(body)


@pytest.fixture
def wire(monkeypatch):
    """Every client the providers build talks to `wire` instead of the network."""
    wire = Wire()
    transport = httpx2.MockTransport(wire.handler)
    real_anthropic = anthropic.Anthropic

    def build_anthropic(**kw):
        return real_anthropic(**kw, http_client=httpx2.Client(transport=transport), max_retries=0)

    monkeypatch.setattr(anthropic, "Anthropic", build_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return wire


def claude(max_tokens: int = 32000) -> AnthropicProvider:
    return AnthropicProvider(
        ProviderConfig(
            name="claude", kind="anthropic", model="claude-opus-5-5", max_tokens=max_tokens
        )
    )


def test_claude_is_streamed_even_without_a_progress_callback(wire):
    # The SDK raises before sending a plain request this large; regenerating
    # one workout and chunked generation pass no callback.
    wire.answers.append(lambda body: claude_stream('{"ok": true}'))
    provider = claude()
    assert provider.complete("system", "rewrite Thursday", {"type": "object"}) == '{"ok": true}'
    sent = wire.bodies[-1]
    assert sent["stream"] is True and sent["max_tokens"] == 32000
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert provider.last_stop == "end_turn"
    assert (provider.last_usage.input_tokens, provider.last_usage.output_tokens) == (10, 7)


def test_claude_progress_arrives_in_pieces(wire):
    wire.answers.append(lambda body: claude_stream('{"plan": "Autumn"}'))
    pieces: list[str] = []
    assert claude().complete("s", "u", None, on_delta=pieces.append) == '{"plan": "Autumn"}'
    assert len(pieces) == 2 and "".join(pieces) == '{"plan": "Autumn"}'
    assert "output_config" not in wire.bodies[-1]


def test_rewriting_one_workout_through_claude(wire):
    from gpp.generate import regenerate_workout
    from gpp.plan import Plan
    from gpp.profile import Profile

    profile = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}})
    easy = {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
    plan = Plan.from_dict(
        {"plan": "Block", "workouts": [{"name": "Easy", "date": "2026-10-01", "steps": [easy]}]}
    )
    rewritten = {"name": "Shorter", "date": "2026-10-01", "steps": [{**easy, "duration": "30m"}]}
    wire.answers.append(lambda body: claude_stream(json.dumps(rewritten)))
    result = regenerate_workout(claude(), profile, plan, "2026-10-01", "make it shorter")
    assert [w.name for w in result.workouts] == ["Shorter"]
