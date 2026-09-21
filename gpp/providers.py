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

DEFAULT_MAX_TOKENS = 32000
MAX_TOKENS_CEILING = 128000
# Stop reasons that mean the answer was cut off, by vendor.
TRUNCATED = ("max_tokens", "length")


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


@dataclass
class Usage:
    """Tokens spent on one call, and an estimated cost where the price is known."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def describe(self) -> str:
        text = f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens"
        if self.cache_read_tokens:
            text += f" ({self.cache_read_tokens:,} from cache)"
        if self.cost_usd is not None:
            text += f" (~${self.cost_usd:.3f})"
        return text

    def __add__(self, other: Usage) -> Usage:
        cost = None
        if self.cost_usd is not None or other.cost_usd is not None:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            cost,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


# USD per million tokens (input, output). Anthropic's published first-party
# rates; other vendors are left out rather than guessed, so their calls
# report tokens only.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Cache reads are billed at a tenth of input, cache writes at 1.25x."""
    if not model:
        return None
    for prefix, (cin, cout) in sorted(PRICES_PER_MTOK.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(prefix):
            return (
                input_tokens * cin
                + output_tokens * cout
                + cache_read * cin * 0.1
                + cache_write * cin * 1.25
            ) / 1_000_000
    return None


# Local models known to produce usable plans, and ones that do not. Shown by
# `gpp providers` so nobody spends an evening on an 8B model that cannot
# hold the schema (#50).
LOCAL_MODEL_PRESETS: list[dict] = [
    {
        "model": "llama3.3",
        "size": "70B",
        "verdict": "good",
        "note": "holds the schema and the coaching rules; slow on CPU",
    },
    {
        "model": "qwen2.5:32b",
        "size": "32B",
        "verdict": "good",
        "note": "reliable JSON; best size/quality trade-off on a 24 GB GPU",
    },
    {
        "model": "qwen2.5:14b",
        "size": "14B",
        "verdict": "usable",
        "note": "needs the retry loop; expect one correction turn",
    },
    {
        "model": "llama3.1:8b",
        "size": "8B",
        "verdict": "weak",
        "note": "drops fields and invents zones; use paste instead",
    },
    {
        "model": "phi4",
        "size": "14B",
        "verdict": "usable",
        "note": "fine for single workouts, flaky on multi-week plans",
    },
]

OnDelta = Callable[[str], None] | None
# Earlier turns of the same conversation: {"role": "user"|"assistant", "content": str}.
History = list[dict[str, str]] | None


class Provider(Protocol):
    name: str
    last_usage: Usage | None
    last_stop: str | None

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None,
        on_delta: OnDelta = None,
        history: History = None,
    ) -> str:
        """Return the model's raw text response, streaming chunks to on_delta if given.

        `history` carries the earlier turns of a correction loop so the model
        sees its own previous answer verbatim rather than a pasted excerpt.
        """


def _messages(history: History, user: str) -> list[dict[str, str]]:
    return [*(history or []), {"role": "user", "content": user}]


def was_truncated(provider: Any) -> bool:
    return getattr(provider, "last_stop", None) in TRUNCATED


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
        self.last_usage: Usage | None = None
        self.last_stop: str | None = None
        self.last_mode: str | None = None  # schema | schema-beta | plain

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None,
        on_delta: OnDelta = None,
        history: History = None,
    ) -> str:
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
            # The system prompt is large and constant across attempts: cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _messages(history, user),
        }

        def call(req: dict[str, Any]):
            if on_delta is None:
                return client.messages.create(**req)
            # Streaming: the plan appears as it is written, and long outputs
            # cannot hit a request timeout.
            with client.messages.stream(**req) as stream:
                for text in stream.text_stream:
                    on_delta(text)
                return stream.get_final_message()

        # Structured outputs, then the older beta spelling, then prompt-only
        # JSON. Each step is only taken when the API rejects the request
        # shape; anything else is a real failure and is reported as such.
        variants: list[tuple[str, dict[str, Any]]] = []
        if schema and self.config.strict_schema:
            variants.append(
                ("schema", {"output_config": {"format": {"type": "json_schema", "schema": schema}}})
            )
            variants.append(
                (
                    "schema-beta",
                    {
                        "output_format": {"type": "json_schema", "schema": schema},
                        "extra_headers": {"anthropic-beta": "structured-outputs-2025-11-13"},
                    },
                )
            )
        variants.append(("plain", {}))
        response = None
        for mode, extra in variants:
            try:
                response = call({**request, **extra})
                self.last_mode = mode
                break
            except Exception as exc:  # a TypeError here is the SDK rejecting a kwarg
                if mode != "plain" and (_is_bad_request(exc) or isinstance(exc, TypeError)):
                    continue
                raise ProviderError(
                    describe_failure(self.name, self.model, exc, ANTHROPIC_MODELS_URL)
                ) from exc

        self.last_usage = usage_from_anthropic(response, self.model)
        self.last_stop = getattr(response, "stop_reason", None)
        if self.last_stop == "refusal":
            raise ProviderError("Claude declined this request")

        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        if not parts:
            raise ProviderError("Claude returned no text content")
        return "\n".join(parts)


def usage_from_anthropic(response: Any, model: str | None) -> Usage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return Usage(inp, out, estimate_cost(model, inp, out, read, write), read, write)


def usage_from_openai(usage: Any, model: str | None) -> Usage | None:
    if usage is None:
        return None
    inp = int(getattr(usage, "prompt_tokens", 0) or 0)
    out = int(getattr(usage, "completion_tokens", 0) or 0)
    return Usage(inp, out, estimate_cost(model, inp, out))


# --- OpenAI and anything speaking its dialect -------------------------------


class OpenAICompatibleProvider:
    """OpenAI, or any server exposing /v1/chat/completions."""

    def __init__(self, config: ProviderConfig):
        self.name = config.name
        self.config = config
        self.model = config.model or KIND_DEFAULTS["openai"]["model"]
        self.docs_url = KIND_DEFAULTS.get(config.kind.lower(), {}).get("docs", OPENAI_MODELS_URL)
        self.last_usage: Usage | None = None
        self.last_stop: str | None = None
        self.last_mode: str | None = None  # json_schema | json_object | plain

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None,
        on_delta: OnDelta = None,
        history: History = None,
    ) -> str:
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
            "messages": [{"role": "system", "content": system}, *_messages(history, user)],
        }
        # Schema-constrained decoding where the server supports it (OpenAI,
        # Gemini's endpoint, Ollama), json_object where it does not, and the
        # validator catches whatever is left. The retry ladder below steps
        # down one rung per 400.
        formats: list[dict[str, Any] | None] = []
        if self.config.strict_schema:
            if schema:
                formats.append(
                    {
                        "type": "json_schema",
                        "json_schema": {"name": "gpp_plan", "schema": schema, "strict": False},
                    }
                )
            formats.append({"type": "json_object"})
        formats.append(None)
        request["response_format"] = formats[0]
        if formats[0] is None:
            request.pop("response_format")

        def call(req: dict[str, Any], streaming: bool) -> tuple[str, Any, str | None]:
            if not streaming:
                response = client.chat.completions.create(**req)
                choice = response.choices[0]
                return (
                    (choice.message.content or ""),
                    getattr(response, "usage", None),
                    getattr(choice, "finish_reason", None),
                )
            return collect_openai_stream(
                client.chat.completions.create(
                    **req, stream=True, stream_options={"include_usage": True}
                ),
                on_delta,
            )

        # Degrade one rung at a time on a 400: streaming off first (some
        # compatible servers reject stream_options), then each response
        # format down to none. Anything that is not a 400 is a real failure.
        streaming = on_delta is not None
        ladder: list[tuple[bool, dict[str, Any] | None]] = []
        for fmt in formats:
            if streaming:
                ladder.append((True, fmt))
            ladder.append((False, fmt))
        last_exc: Exception | None = None
        content = usage = finish = None
        for stream_now, fmt in ladder:
            req = dict(request)
            req.pop("response_format", None)
            if fmt is not None:
                req["response_format"] = fmt
            try:
                content, usage, finish = call(req, stream_now)
                self.last_mode = (fmt or {}).get("type", "plain")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if not _is_bad_request(exc):
                    break
        if last_exc is not None:
            raise ProviderError(
                describe_failure(self.name, self.model, last_exc, self.docs_url)
            ) from last_exc

        self.last_usage = usage_from_openai(usage, self.model)
        self.last_stop = finish
        if not content:
            raise ProviderError(f"{self.name} returned an empty response")
        return content


def collect_openai_stream(
    chunks: Any, on_delta: Callable[[str], None]
) -> tuple[str, Any, str | None]:
    """Drain a chat-completions stream: text to on_delta, usage and the finish
    reason from the tail."""
    parts: list[str] = []
    usage = None
    finish = None
    for chunk in chunks:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                parts.append(text)
                on_delta(text)
            finish = getattr(choices[0], "finish_reason", None) or finish
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
    return "".join(parts), usage, finish


# --- Manual / paste ---------------------------------------------------------

# How much of a previous answer a relayed correction turn carries.
MANUAL_HISTORY_CHARS = 60_000


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
        self.last_usage: Usage | None = None
        self.last_stop: str | None = None
        self.prompt_path = Path(config.options.get("prompt_file", "plan-prompt.txt"))
        self.response_path = Path(config.options.get("response_file", "plan-response.json"))

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None,
        on_delta: OnDelta = None,
        history: History = None,
    ) -> str:
        # A chat window has no conversation state we can attach to, so earlier
        # turns are rendered into the text the user relays.
        earlier = ""
        for turn in history or []:
            label = "YOUR PREVIOUS ANSWER" if turn["role"] == "assistant" else "EARLIER REQUEST"
            earlier += f"\n{label}\n{turn['content'][:MANUAL_HISTORY_CHARS]}\n"
        full_prompt = (
            f"{system}\n\nREQUEST\n{user}\n"
            if not earlier
            else (f"{system}\n{earlier}\nREQUEST\n{user}\n")
        )

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
