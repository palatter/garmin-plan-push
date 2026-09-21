import pytest

from gpp.generate import generate_plan
from gpp.profile import Profile
from gpp.providers import ProviderError, extract_json, load_providers

PROFILE = Profile.from_dict({"pace": {"threshold": "4:00/km"}})

GOOD = """{
  "plan": "p",
  "workouts": [
    {"name": "W", "date": "2026-09-24",
     "steps": [{"kind": "run", "duration": "30m",
                "target": {"type": "pace", "zone": "easy"}}]}
  ]
}"""


# --- JSON extraction --------------------------------------------------------


def test_extract_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_strips_markdown_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_finds_object_in_prose():
    text = 'Here is your plan:\n\n{"a": 1}\n\nHope that helps!'
    assert extract_json(text) == {"a": 1}


def test_extract_ignores_braces_inside_strings():
    assert extract_json('{"note": "a } brace"}') == {"note": "a } brace"}


def test_extract_reports_truncation():
    with pytest.raises(ProviderError, match="truncated"):
        extract_json('{"a": {"b": 1}')


def test_extract_reports_missing_json():
    with pytest.raises(ProviderError, match="no JSON object"):
        extract_json("I cannot help with that.")


# --- provider config --------------------------------------------------------


def test_defaults_when_no_ai_block():
    configs, default = load_providers({})
    assert "claude" in configs and "paste" in configs
    assert default is None


def test_loads_configured_providers():
    configs, default = load_providers(
        {
            "ai": {
                "default": "local",
                "providers": {
                    "local": {"kind": "ollama", "model": "llama3.3"},
                    "gpt": {"kind": "openai", "model": "gpt-4o"},
                },
            }
        }
    )
    assert default == "local"
    assert configs["local"].model == "llama3.3"
    assert configs["gpt"].kind == "openai"


def test_provider_missing_kind_is_rejected():
    with pytest.raises(ProviderError, match="missing `kind`"):
        load_providers({"ai": {"providers": {"x": {"model": "m"}}}})


# --- the retry loop ---------------------------------------------------------


class ScriptedProvider:
    """Returns canned responses in order, recording what it was asked."""

    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, system, user, schema):
        self.prompts.append(user)
        if not self.responses:
            raise AssertionError("called more times than scripted")
        return self.responses.pop(0)


def test_accepts_a_good_plan_first_time():
    provider = ScriptedProvider([GOOD])
    result = generate_plan(provider, PROFILE, "give me a run")
    assert result.attempts == 1
    assert result.corrections == []
    assert len(result.plan.workouts) == 1


def test_feeds_validation_errors_back_and_recovers():
    bad = '{"plan": "p", "workouts": [{"name": "W", "date": "2026-09-24", "steps": [{"kind": "run"}]}]}'
    provider = ScriptedProvider([bad, GOOD])
    result = generate_plan(provider, PROFILE, "give me a run")
    assert result.attempts == 2
    assert "exactly one of duration" in result.corrections[0]
    # The second prompt must actually contain the correction.
    assert "exactly one of duration" in provider.prompts[1]


def test_catches_bad_zone_via_compilation():
    """A zone the profile does not define passes schema but must not slip through."""
    bad = (
        '{"plan": "p", "workouts": [{"name": "W", "date": "2026-09-24", "steps": '
        '[{"kind": "run", "duration": "10m", "target": {"type": "pace", "zone": "moon"}}]}]}'
    )
    provider = ScriptedProvider([bad, GOOD])
    result = generate_plan(provider, PROFILE, "give me a run")
    assert result.attempts == 2
    assert "unknown pace zone" in result.corrections[0]


def test_gives_up_after_the_attempt_budget():
    provider = ScriptedProvider(["nonsense", "still nonsense"])
    with pytest.raises(ProviderError, match="could not produce a valid plan"):
        generate_plan(provider, PROFILE, "go", attempts=2)
