"""Pluggable AI providers for writing the plan.

Deliberately provider-neutral: the plan DSL is the contract, and any model that
can emit JSON can fill it. Each provider lazily imports its own vendor SDK, so
you only install what you actually use.

    kind = "anthropic" / "claude"   official anthropic SDK
    kind = "openai" / "chatgpt"     official openai SDK (api.openai.com)
    kind = "gemini"                 Google's OpenAI-compatible endpoint, so the
                                    openai SDK again -- only the address, key
                                    variable and model names differ
    kind = "openai-compatible"      same SDK, your own base_url -- covers
                                    OpenRouter, Groq, Together, Fireworks,
                                    LM Studio, vLLM, anything speaking the
                                    chat-completions shape
    kind = "ollama"                 local models on localhost:11434
    kind = "manual" / "paste"       no API at all: you relay the prompt to any
                                    chat window and paste the answer back

With no [ai] block at all you get claude, chatgpt, gemini and paste, and the
first one with a key set is preselected. Adding a kind is one row in
KIND_DEFAULTS if the vendor speaks an existing dialect; a class with one
method if not.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MAX_TOKENS = 16000


class ProviderError(RuntimeError):
    """The provider could not be built, or the call failed."""


# Collects a long text answer from the user: given a prompt to relay, returns
# whatever they paste back. The web UI supplies one; the CLI does not.
AskFn = Callable[[str], str]


@dataclass
class ProviderConfig:
    name: str
    kind: str
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    # Ask the API to constrain output to the plan schema where supported.
    strict_schema: bool = True
    options: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        """Return the model's raw text response."""


# --- Anthropic --------------------------------------------------------------


class AnthropicProvider:
    """Claude via the official SDK.

    Thinking is deliberately left unset: on Opus 5 adaptive thinking is the
    default, which is what we want for plan construction.
    """

    def __init__(self, config: ProviderConfig):
        self.name = config.name
        self.config = config
        self.model = config.model or "claude-opus-5"

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "the anthropic SDK is not installed. Run: uv sync --extra anthropic"
            ) from exc

        key = _api_key(self.config, "ANTHROPIC_API_KEY")
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if schema and self.config.strict_schema:
            request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

        try:
            response = client.messages.create(**request)
        except Exception as exc:
            # A schema the API will not accept (ours uses $ref/oneOf) should
            # degrade to prompt-only JSON rather than kill the run.
            if "output_config" in request and _is_bad_request(exc):
                request.pop("output_config")
                try:
                    response = client.messages.create(**request)
                except Exception as inner:
                    raise ProviderError(
                        describe_failure(self.name, self.model, inner, ANTHROPIC_MODELS_URL)
                    ) from inner
            else:
                raise ProviderError(
                    describe_failure(self.name, self.model, exc, ANTHROPIC_MODELS_URL)
                ) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("Claude declined this request")

        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        if not parts:
            raise ProviderError("Claude returned no text content")
        return "\n".join(parts)


# --- OpenAI and anything speaking its dialect -------------------------------


class OpenAICompatibleProvider:
    """OpenAI, or any server exposing /v1/chat/completions."""

    def __init__(self, config: ProviderConfig):
        self.name = config.name
        self.config = config
        self.model = config.model or KIND_DEFAULTS["openai"]["model"]
        self.docs_url = KIND_DEFAULTS.get(config.kind.lower(), {}).get("docs", OPENAI_MODELS_URL)

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError(
                "the openai SDK is not installed. Run: uv sync --extra openai"
            ) from exc

        key = _api_key(self.config, "OPENAI_API_KEY")
        if not key and (self.config.api_key_env is None or self.config.base_url):
            # Local servers (Ollama, LM Studio, vLLM) take any non-empty
            # string; the SDK insists on one. A hosted base_url with a missing
            # key gets a 401 that describe_failure explains.
            key = "not-needed"
        if not key:
            raise ProviderError(
                f"no API key for provider {self.name!r}; set the "
                f"{self.config.api_key_env or 'OPENAI_API_KEY'} environment "
                f"variable, or choose the paste provider, which needs none"
            )

        client = OpenAI(api_key=key, base_url=self.config.base_url or None)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.config.strict_schema:
            # json_object is far more widely supported across compatible
            # servers than full json_schema, and the validator catches the
            # rest.
            request["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:
            if "response_format" in request and _is_bad_request(exc):
                request.pop("response_format")
                try:
                    response = client.chat.completions.create(**request)
                except Exception as inner:
                    raise ProviderError(
                        describe_failure(self.name, self.model, inner, self.docs_url)
                    ) from inner
            else:
                raise ProviderError(
                    describe_failure(self.name, self.model, exc, self.docs_url)
                ) from exc

        content = response.choices[0].message.content
        if not content:
            raise ProviderError(f"{self.name} returned an empty response")
        return content


# --- Manual / paste ---------------------------------------------------------


class ManualProvider:
    """No API key, no SDK: you relay the prompt to any chat window yourself.

    This is the zero-cost path, and it is the one that makes the tool
    shareable - a friend with no API budget can still use every other part of
    it by pasting into whatever assistant they already have open.

    Two ways to collect the answer. With an `ask` callback (the web UI passes
    one), the prompt and the reply box appear on screen. Without one, it falls
    back to files and the terminal, which is what the CLI wants.
    """

    def __init__(self, config: ProviderConfig, ask: AskFn | None = None):
        self.name = config.name
        self.config = config
        self.ask = ask
        self.prompt_path = Path(config.options.get("prompt_file", "plan-prompt.txt"))
        self.response_path = Path(config.options.get("response_file", "plan-response.json"))

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        full_prompt = f"{system}\n\nREQUEST\n{user}\n"

        if self.ask is not None:
            reply = self.ask(full_prompt)
            if not reply.strip():
                raise ProviderError("no plan was pasted back")
            return reply

        self.prompt_path.write_text(full_prompt, encoding="utf-8")
        print(f"\nPrompt written to {self.prompt_path}")
        print(f"Paste the model's JSON reply into {self.response_path}, then press Enter.")
        input()
        if not self.response_path.exists():
            raise ProviderError(f"no response found at {self.response_path}")
        return self.response_path.read_text(encoding="utf-8")


# --- registry ---------------------------------------------------------------

KINDS = {
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,
    "openai": OpenAICompatibleProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "chatgpt": OpenAICompatibleProvider,
    "gemini": OpenAICompatibleProvider,
    "ollama": OpenAICompatibleProvider,
    "manual": ManualProvider,
    "paste": ManualProvider,
}

ANTHROPIC_MODELS_URL = "https://docs.anthropic.com/en/docs/about-claude/models"
OPENAI_MODELS_URL = "https://platform.openai.com/docs/models"
GEMINI_MODELS_URL = "https://ai.google.dev/gemini-api/docs/models"

# What each kind needs when the user has not said. Model names are the one
# thing here that WILL go stale -- vendors retire them -- which is why the
# failure path (describe_failure) tells the user exactly which line to edit,
# and why `gpp doctor --ping` exists.
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "model": "claude-opus-5",
        "key_env": "ANTHROPIC_API_KEY",
        "docs": ANTHROPIC_MODELS_URL,
    },
    "openai": {
        "model": "gpt-5.2",
        "key_env": "OPENAI_API_KEY",
        "docs": OPENAI_MODELS_URL,
    },
    "gemini": {
        # Google exposes Gemini through an OpenAI-compatible endpoint, so the
        # same client works; only the address, key and model names differ.
        "model": "gemini-2.5-pro",
        "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "docs": GEMINI_MODELS_URL,
    },
    "ollama": {
        "model": "llama3.3",
        "key_env": None,
        "base_url": "http://localhost:11434/v1",
        "docs": "https://ollama.com/library",
    },
    "openai-compatible": {"model": None, "key_env": "OPENAI_API_KEY", "docs": OPENAI_MODELS_URL},
    "manual": {"model": None, "key_env": None, "docs": None},
}
KIND_DEFAULTS["claude"] = KIND_DEFAULTS["anthropic"]
KIND_DEFAULTS["chatgpt"] = KIND_DEFAULTS["openai"]
KIND_DEFAULTS["paste"] = KIND_DEFAULTS["manual"]


def resolve(config: ProviderConfig) -> ProviderConfig:
    """Fill in whatever the user left out, in place, from KIND_DEFAULTS."""
    kind = config.kind.strip().lower()
    defaults = KIND_DEFAULTS.get(kind)
    if defaults is None:
        raise ProviderError(
            f"unknown provider kind {config.kind!r}; known: " + ", ".join(sorted(KINDS))
        )
    if config.model is None:
        config.model = defaults.get("model")
    if config.base_url is None:
        config.base_url = defaults.get("base_url")
    if config.api_key_env is None:
        config.api_key_env = defaults.get("key_env")
    return config


def build_provider(config: ProviderConfig, ask: AskFn | None = None) -> Provider:
    """`ask` is only meaningful for manual providers; others ignore it."""
    resolve(config)
    cls = KINDS[config.kind.strip().lower()]
    if cls is ManualProvider:
        return cls(config, ask=ask)
    return cls(config)


def is_manual(config: ProviderConfig) -> bool:
    return config.kind.strip().lower() in ("manual", "paste")


def key_present(config: ProviderConfig) -> bool | None:
    """True/False for kinds that need a key; None for kinds that don't."""
    resolve(config)
    if config.api_key_env is None:
        return None
    return bool(os.environ.get(config.api_key_env))


def pick_default(configs: dict[str, ProviderConfig], explicit: str | None) -> str:
    """The provider to preselect.

    An explicit `default` in the profile wins. Otherwise the first provider
    whose key is actually set -- so a friend with only a GEMINI_API_KEY lands
    on Gemini, not on a Claude entry that will fail -- and failing that, the
    paste provider, which always works.
    """
    if explicit in configs:
        return explicit
    for name, config in configs.items():
        if key_present(config):
            return name
    for name, config in configs.items():
        if is_manual(config):
            return name
    return next(iter(configs))


def describe_failure(name: str, model: str | None, exc: Exception, docs_url: str | None) -> str:
    """Turn an SDK exception into something a runner can act on.

    The two failures a non-developer will actually hit are a retired model
    name and a bad key. Both deserve a sentence that names the fix, not an
    HTTP status.
    """
    text = str(exc)
    status = getattr(exc, "status_code", None)
    lowered = text.lower()

    looks_like_model = status == 404 or re.search(
        r"(model|engine)[^.]{0,60}(not found|does not exist|not exist|unknown|"
        r"not supported|deprecated|decommissioned|invalid)|"
        r"(not found|does not exist)[^.]{0,40}model",
        lowered,
    )
    if looks_like_model:
        hint = f" Current names: {docs_url}" if docs_url else ""
        return (
            f"{name} does not recognise the model {model!r}. Model names change "
            f'as vendors retire them - set  model = "..."  under '
            f"[ai.providers.{name}] in profile.toml.{hint}"
        )

    if status in (401, 403) or any(
        s in lowered
        for s in ("authentication", "api key", "api_key", "unauthorized", "permission denied")
    ):
        return (
            f"{name} rejected the API key. Check the environment variable is set "
            f"in the shell that launched gpp, and that the key is current. ({text})"
        )

    if status == 429 or "rate limit" in lowered or "quota" in lowered:
        return f"{name} is rate-limiting or out of quota - wait a minute and retry. ({text})"

    return f"{name} call failed: {text}"


def probe(config: ProviderConfig) -> str:
    """A one-line completion, to prove the key and model work.

    Used by `gpp doctor --ping`. Costs a few tokens.
    """
    trial = ProviderConfig(
        name=config.name,
        kind=config.kind,
        model=config.model,
        base_url=config.base_url,
        api_key_env=config.api_key_env,
        max_tokens=16,
        strict_schema=False,
        options=dict(config.options),
    )
    provider = build_provider(trial)
    return provider.complete(
        "Reply with the single word OK and nothing else.", "Ready?", None
    ).strip()


def load_providers(data: dict) -> tuple[dict[str, ProviderConfig], str | None]:
    """Read the [ai] block of profile.toml."""
    ai = data.get("ai") or {}
    configs: dict[str, ProviderConfig] = {}
    for name, entry in (ai.get("providers") or {}).items():
        if "kind" not in entry:
            raise ProviderError(f"provider {name!r} is missing `kind`")
        configs[name] = ProviderConfig(
            name=name,
            kind=entry["kind"],
            model=entry.get("model"),
            base_url=entry.get("base_url"),
            api_key_env=entry.get("api_key_env"),
            max_tokens=int(entry.get("max_tokens", DEFAULT_MAX_TOKENS)),
            strict_schema=bool(entry.get("strict_schema", True)),
            options={
                k: v
                for k, v in entry.items()
                if k
                not in (
                    "kind",
                    "model",
                    "base_url",
                    "api_key_env",
                    "max_tokens",
                    "strict_schema",
                )
            },
        )
    if not configs:
        configs = default_configs()
    return configs, ai.get("default")


def default_configs() -> dict[str, ProviderConfig]:
    """What you get with no [ai] block at all: the three big assistants plus
    paste, which needs no key and therefore always works."""
    return {
        "claude": ProviderConfig(name="claude", kind="anthropic"),
        "chatgpt": ProviderConfig(name="chatgpt", kind="openai"),
        "gemini": ProviderConfig(name="gemini", kind="gemini"),
        "paste": ProviderConfig(name="paste", kind="manual"),
    }


# --- helpers ----------------------------------------------------------------


def _api_key(config: ProviderConfig, fallback_env: str) -> str | None:
    if config.api_key_env:
        return os.environ.get(config.api_key_env)
    return os.environ.get(fallback_env)


def _is_bad_request(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 400:
        return True
    return "400" in str(exc) or "invalid_request" in str(exc).lower()


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences more often than they should, so this
    is forgiving: it strips fences, then falls back to the outermost balanced
    brace pair.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        raise ProviderError("no JSON object found in the model's response")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"model emitted malformed JSON: {exc}") from exc
    raise ProviderError("model's JSON response is truncated (unbalanced braces)")
