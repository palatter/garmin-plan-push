"""Pluggable AI providers for writing the plan.

Deliberately provider-neutral: the plan DSL is the contract, and any model that
can emit JSON can fill it. Each provider lazily imports its own vendor SDK, so
you only install what you actually use.

    kind = "anthropic"          official anthropic SDK
    kind = "openai"             official openai SDK (api.openai.com)
    kind = "openai-compatible"  same SDK, your own base_url -- covers
                                OpenRouter, Groq, Together, Fireworks,
                                Ollama (/v1), LM Studio, vLLM, anything else
                                speaking the chat-completions shape
    kind = "manual"             no API at all: writes the prompt to a file,
                                you paste the answer back. Works with any chat
                                UI, including one you are already talking to.

Adding a provider is one class with one method.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MAX_TOKENS = 16000


class ProviderError(RuntimeError):
    """The provider could not be built, or the call failed."""


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
            request["output_config"] = {
                "format": {"type": "json_schema", "schema": schema}
            }

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
                    raise ProviderError(f"Anthropic call failed: {inner}") from inner
            else:
                raise ProviderError(f"Anthropic call failed: {exc}") from exc

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
        self.model = config.model or "gpt-4o"

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError(
                "the openai SDK is not installed. Run: uv sync --extra openai"
            ) from exc

        key = _api_key(self.config, "OPENAI_API_KEY")
        if not key and self.config.base_url:
            # Local servers (Ollama, LM Studio, vLLM) want a placeholder.
            key = "not-needed"
        if not key:
            raise ProviderError(
                f"no API key for provider {self.name!r}; set "
                f"{self.config.api_key_env or 'OPENAI_API_KEY'}"
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
                    raise ProviderError(f"{self.name} call failed: {inner}") from inner
            else:
                raise ProviderError(f"{self.name} call failed: {exc}") from exc

        content = response.choices[0].message.content
        if not content:
            raise ProviderError(f"{self.name} returned an empty response")
        return content


# --- Manual / paste ---------------------------------------------------------


class ManualProvider:
    """No API. Writes the prompt out, waits for you to paste the answer back.

    This is the zero-dependency escape hatch, and it is genuinely useful: it
    works with any chat interface you already have open, including a Claude
    Code session, a browser tab, or a model with no API at all.
    """

    def __init__(self, config: ProviderConfig):
        self.name = config.name
        self.config = config
        self.prompt_path = Path(config.options.get("prompt_file", "plan-prompt.txt"))
        self.response_path = Path(
            config.options.get("response_file", "plan-response.json")
        )

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        self.prompt_path.write_text(f"{system}\n\nREQUEST\n{user}\n", encoding="utf-8")
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
    "ollama": OpenAICompatibleProvider,
    "manual": ManualProvider,
    "paste": ManualProvider,
}

# Sensible base_urls so common setups need almost no config.
KIND_DEFAULT_BASE_URL = {
    "ollama": "http://localhost:11434/v1",
}


def build_provider(config: ProviderConfig) -> Provider:
    kind = config.kind.strip().lower()
    cls = KINDS.get(kind)
    if cls is None:
        raise ProviderError(
            f"unknown provider kind {config.kind!r}; known: "
            + ", ".join(sorted(KINDS))
        )
    if config.base_url is None and kind in KIND_DEFAULT_BASE_URL:
        config.base_url = KIND_DEFAULT_BASE_URL[kind]
    return cls(config)


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
        # Usable with no config at all.
        configs["claude"] = ProviderConfig(name="claude", kind="anthropic")
        configs["paste"] = ProviderConfig(name="paste", kind="manual")
    return configs, ai.get("default")


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
