"""Provider kinds, defaults, and the pieces that decide which one gets used."""

import pytest

from gpp.providers import (
    KIND_DEFAULTS,
    ProviderConfig,
    ProviderError,
    default_configs,
    describe_failure,
    key_present,
    load_providers,
    pick_default,
    resolve,
)

# --- kinds and defaults -----------------------------------------------------


def test_gemini_resolves_to_googles_openai_compatible_endpoint():
    config = resolve(ProviderConfig(name="g", kind="gemini"))
    assert config.base_url.startswith("https://generativelanguage.googleapis.com/")
    assert config.api_key_env == "GEMINI_API_KEY"
    assert config.model  # some default model is filled in


def test_chatgpt_is_an_alias_for_openai():
    a = resolve(ProviderConfig(name="a", kind="chatgpt"))
    b = resolve(ProviderConfig(name="b", kind="openai"))
    assert (a.model, a.api_key_env, a.base_url) == (b.model, b.api_key_env, b.base_url)


def test_claude_is_an_alias_for_anthropic():
    assert KIND_DEFAULTS["claude"] is KIND_DEFAULTS["anthropic"]


def test_user_values_are_never_overwritten():
    config = ProviderConfig(
        name="x",
        kind="gemini",
        model="gemini-custom",
        base_url="http://proxy",
        api_key_env="MY_KEY",
    )
    resolve(config)
    assert (config.model, config.base_url, config.api_key_env) == (
        "gemini-custom",
        "http://proxy",
        "MY_KEY",
    )


def test_unknown_kind_is_rejected_with_the_known_list():
    with pytest.raises(ProviderError, match=r"known:.*gemini"):
        resolve(ProviderConfig(name="x", kind="skynet"))


def test_out_of_the_box_you_get_the_big_three_plus_paste():
    names = list(default_configs())
    assert names == ["claude", "chatgpt", "gemini", "paste"]
    configs, default = load_providers({})
    assert list(configs) == names
    assert default is None


# --- key presence -----------------------------------------------------------


def test_key_present_reports_per_kind(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert key_present(ProviderConfig(name="g", kind="gemini")) is False
    assert key_present(ProviderConfig(name="c", kind="chatgpt")) is True
    assert key_present(ProviderConfig(name="p", kind="paste")) is None
    assert key_present(ProviderConfig(name="o", kind="ollama")) is None


# --- choosing a default -----------------------------------------------------


def test_explicit_default_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    configs = default_configs()
    assert pick_default(configs, "gemini") == "gemini"


def test_falls_back_to_the_first_provider_with_a_key(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert pick_default(default_configs(), None) == "gemini"


def test_falls_back_to_paste_when_nothing_is_keyed(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    assert pick_default(default_configs(), None) == "paste"


def test_bogus_explicit_default_is_ignored(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    assert pick_default(default_configs(), "nope") == "paste"


# --- error translation ------------------------------------------------------


class _Exc(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_retired_model_names_get_the_fix_spelled_out():
    msg = describe_failure(
        "gemini", "gemini-1.0", _Exc("model gemini-1.0 not found", 404), "https://docs"
    )
    assert "does not recognise the model 'gemini-1.0'" in msg
    assert "[ai.providers.gemini]" in msg
    assert "https://docs" in msg


def test_model_error_is_detected_from_the_message_alone():
    msg = describe_failure("chatgpt", "gpt-x", _Exc("The model `gpt-x` does not exist"), None)
    assert "does not recognise" in msg


def test_bad_key_is_named_as_such():
    msg = describe_failure("claude", "m", _Exc("invalid x-api-key", 401), None)
    assert "rejected the API key" in msg


def test_rate_limit_says_wait():
    msg = describe_failure("claude", "m", _Exc("rate limit exceeded", 429), None)
    assert "rate-limiting" in msg


def test_unknown_errors_pass_through():
    msg = describe_failure("claude", "m", _Exc("connection reset"), None)
    assert msg == "claude call failed: connection reset"
