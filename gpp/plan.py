"""The plan DSL: the small, constrained format an LLM is allowed to emit.

Design rule: the model never writes Garmin JSON. It writes this, which is
flat, uses human units, and is validated hard before anything is compiled.
Every ambiguity that could silently produce a wrong workout is designed out:

  * pace bounds are named `slow`/`fast`, never `low`/`high`, because "low
    pace" is genuinely ambiguous and a model will get it wrong half the time;
  * a step takes exactly one of duration / distance / until, never a mix;
  * zone names are a closed set, resolved from your profile, so the model
    cannot invent a 3:12/km recovery jog.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

EXECUTABLE_KINDS = ("warmup", "run", "recover", "rest", "cooldown")
MAX_REPEAT_DEPTH = 2

TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        {
            "properties": {
                "type": {"const": "pace"},
                "zone": {"type": "string"},
            },
            "required": ["type", "zone"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "pace"},
                "slow": {"type": "string"},
                "fast": {"type": "string"},
            },
            "required": ["type", "slow", "fast"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "hr"},
                "zone": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["type", "zone"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "hr"},
                "low": {"type": "integer", "minimum": 30, "maximum": 240},
                "high": {"type": "integer", "minimum": 30, "maximum": 240},
            },
            "required": ["type", "low", "high"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "cadence"},
                "low": {"type": "integer", "minimum": 30, "maximum": 260},
                "high": {"type": "integer", "minimum": 30, "maximum": 260},
            },
            "required": ["type", "low", "high"],
            "additionalProperties": False,
        },
        {
            "properties": {"type": {"const": "none"}},
            "required": ["type"],
            "additionalProperties": False,
        },
    ],
}

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        {
            "properties": {
                "kind": {"enum": list(EXECUTABLE_KINDS)},
                "duration": {"type": ["string", "number"]},
                "distance": {"type": ["string", "number"]},
                "until": {"const": "lap"},
                "target": TARGET_SCHEMA,
                "note": {"type": "string", "maxLength": 512},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "kind": {"const": "repeat"},
                "reps": {"type": "integer", "minimum": 1, "maximum": 50},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/step"},
                },
                "note": {"type": "string", "maxLength": 512},
            },
            "required": ["kind", "reps", "steps"],
            "additionalProperties": False,
        },
    ],
}

WORKOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "sport": {"enum": ["running", "cycling", "swimming"]},
        "notes": {"type": "string", "maxLength": 1024},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/step"},
        },
    },
    "required": ["name", "date", "steps"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gpp training plan",
    "type": "object",
    "properties": {
        "plan": {"type": "string", "minLength": 1, "maxLength": 80},
        "workouts": {"type": "array", "minItems": 1, "items": WORKOUT_SCHEMA},
    },
    "required": ["plan", "workouts"],
    "additionalProperties": False,
    "$defs": {"step": _STEP_SCHEMA},
}


class PlanError(ValueError):
    """The plan is structurally or semantically invalid."""


@dataclass
class Target:
    type: str = "none"
    zone: str | int | None = None
    slow: str | None = None
    fast: str | None = None
    low: int | None = None
    high: int | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "Target":
        if not data:
            return cls()
        return cls(**data)


@dataclass
class Step:
    kind: str
    duration: str | float | None = None
    distance: str | float | None = None
    until: str | None = None
    target: Target = field(default_factory=Target)
    note: str | None = None
    reps: int | None = None
    steps: list["Step"] = field(default_factory=list)

    @property
    def is_repeat(self) -> bool:
        return self.kind == "repeat"

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        if data.get("kind") == "repeat":
            return cls(
                kind="repeat",
                reps=data["reps"],
                steps=[cls.from_dict(child) for child in data["steps"]],
                note=data.get("note"),
            )
        return cls(
            kind=data["kind"],
            duration=data.get("duration"),
            distance=data.get("distance"),
            until=data.get("until"),
            target=Target.from_dict(data.get("target")),
            note=data.get("note"),
        )


@dataclass
class Workout:
    name: str
    date: dt.date
    steps: list[Step]
    sport: str = "running"
    notes: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Workout":
        return cls(
            name=data["name"],
            date=dt.date.fromisoformat(data["date"]),
            steps=[Step.from_dict(s) for s in data["steps"]],
            sport=data.get("sport", "running"),
            notes=data.get("notes"),
        )


@dataclass
class Plan:
    plan: str
    workouts: list[Workout]

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        validate(data)
        return cls(
            plan=data["plan"],
            workouts=[Workout.from_dict(w) for w in data["workouts"]],
        )

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        raw = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


# --- validation -------------------------------------------------------------


def validate(data: dict) -> None:
    """Structural validation, then the semantic rules with friendly messages."""
    validator = jsonschema.Draft202012Validator(PLAN_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise PlanError(f"schema error at {where}: {first.message}")

    seen: set[tuple[str, str]] = set()
    for workout in data["workouts"]:
        _check_date(workout)
        key = (workout["date"], workout["name"].strip().lower())
        if key in seen:
            raise PlanError(
                f"two workouts on {workout['date']} share the name "
                f"{workout['name']!r}; names must be unique per day so the "
                f"tool can tell them apart when re-pushing"
            )
        seen.add(key)
        for index, step in enumerate(workout["steps"], start=1):
            _check_step(step, f"{workout['name']} step {index}", depth=0)


def _check_date(workout: dict) -> None:
    try:
        dt.date.fromisoformat(workout["date"])
    except ValueError as exc:
        raise PlanError(f"workout {workout['name']!r}: bad date: {exc}") from exc


def _check_step(step: dict, where: str, depth: int) -> None:
    if step["kind"] == "repeat":
        if depth + 1 > MAX_REPEAT_DEPTH:
            raise PlanError(
                f"{where}: repeats nested more than {MAX_REPEAT_DEPTH} deep; "
                f"Garmin watches render these unreliably -- flatten it"
            )
        for index, child in enumerate(step["steps"], start=1):
            _check_step(child, f"{where}.{index}", depth + 1)
        if not any(c["kind"] != "repeat" for c in step["steps"]):
            raise PlanError(f"{where}: a repeat must contain at least one real step")
        return

    bounds = [k for k in ("duration", "distance", "until") if step.get(k) is not None]
    if len(bounds) != 1:
        raise PlanError(
            f"{where}: needs exactly one of duration / distance / until, "
            f"got {bounds or 'none'}"
        )

    target = step.get("target")
    if not target:
        return

    if target["type"] == "pace" and "slow" in target:
        from .units import UnitError, parse_pace

        try:
            slow = parse_pace(target["slow"])
            fast = parse_pace(target["fast"])
        except UnitError as exc:
            raise PlanError(f"{where}: {exc}") from exc
        if fast > slow:
            raise PlanError(
                f"{where}: pace target has fast={target['fast']} slower than "
                f"slow={target['slow']}; `fast` must be the quicker pace"
            )

    if target["type"] in ("hr", "cadence") and "low" in target:
        if target["high"] <= target["low"]:
            raise PlanError(
                f"{where}: {target['type']} target high ({target['high']}) "
                f"must be greater than low ({target['low']})"
            )
