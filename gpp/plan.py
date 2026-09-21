"""The plan DSL: the small, constrained format an LLM is allowed to emit.

Design rule: the model never writes Garmin JSON. It writes this, which is
flat, uses human units, and is validated hard before anything is compiled.
Every ambiguity that could silently produce a wrong workout is designed out:

  * pace bounds are named `slow`/`fast`, never `low`/`high`, because "low
    pace" is genuinely ambiguous and a model will get it wrong half the time;
  * a step takes exactly one of duration / distance / until (or `count` for a
    strength exercise), never a mix;
  * zone names are a closed set, resolved from your profile, so the model
    cannot invent a 3:12/km recovery jog.

Beyond the steps, a plan can carry the structure a coach would: a race date
and a list of A/B/C races, a phase per workout (base, build, peak, taper,
recovery) and a role (easy, long, quality, ...). Those fields exist so the
sanity checks in `checks.py` have something to check against.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXECUTABLE_KINDS = ("warmup", "run", "recover", "rest", "cooldown", "stride", "exercise")
SPORTS = ("running", "cycling", "swimming", "strength", "cardio")
ROLES = ("easy", "long", "medium-long", "quality", "race", "recovery", "cross", "strength", "rest")
PHASES = ("base", "build", "peak", "taper", "recovery")
PRIORITIES = ("A", "B", "C")
MAX_REPEAT_DEPTH = 2

_DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}

TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        {
            "properties": {"type": {"const": "pace"}, "zone": {"type": "string"}},
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
            "properties": {
                "type": {"const": "power"},
                "zone": {"type": "integer", "minimum": 1, "maximum": 7},
            },
            "required": ["type", "zone"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "power"},
                "low": {"type": "integer", "minimum": 30, "maximum": 2000},
                "high": {"type": "integer", "minimum": 30, "maximum": 2000},
            },
            "required": ["type", "low", "high"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "type": {"const": "rpe"},
                "value": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["type", "value"],
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
                "count": {"type": "integer", "minimum": 1, "maximum": 500},
                "target": TARGET_SCHEMA,
                "note": {"type": "string", "maxLength": 512},
                "grade": {"type": "number", "minimum": -30, "maximum": 30},
                "exercise": {"type": "string", "minLength": 1, "maxLength": 80},
                "category": {"type": "string", "minLength": 1, "maxLength": 80},
                "weight": {"type": "number", "minimum": 0, "maximum": 500},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "kind": {"const": "repeat"},
                "reps": {"type": "integer", "minimum": 1, "maximum": 50},
                "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}},
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
        "date": _DATE,
        "sport": {"enum": list(SPORTS)},
        "role": {"enum": list(ROLES)},
        "phase": {"enum": list(PHASES)},
        "notes": {"type": "string", "maxLength": 1024},
        "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}},
    },
    "required": ["name", "date", "steps"],
    "additionalProperties": False,
}

RACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "date": _DATE,
        "priority": {"enum": list(PRIORITIES)},
        "distance": {"type": "string", "maxLength": 20},
    },
    "required": ["name", "date", "priority"],
    "additionalProperties": False,
}

WEEK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start": _DATE,
        "phase": {"enum": list(PHASES)},
        "note": {"type": "string", "maxLength": 200},
    },
    "required": ["start", "phase"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gpp training plan",
    "type": "object",
    "properties": {
        "plan": {"type": "string", "minLength": 1, "maxLength": 80},
        "race_date": _DATE,
        "races": {"type": "array", "maxItems": 12, "items": RACE_SCHEMA},
        "weeks": {"type": "array", "maxItems": 60, "items": WEEK_SCHEMA},
        "workouts": {"type": "array", "minItems": 1, "maxItems": 400, "items": WORKOUT_SCHEMA},
    },
    "required": ["plan", "workouts"],
    "additionalProperties": False,
    "$defs": {"step": _STEP_SCHEMA},
}


class PlanError(ValueError):
    """The plan is structurally or semantically invalid."""


# --- dataclasses ------------------------------------------------------------


@dataclass
class Target:
    type: str = "none"
    zone: str | int | None = None
    slow: str | None = None
    fast: str | None = None
    low: int | None = None
    high: int | None = None
    value: int | None = None  # rpe

    @classmethod
    def from_dict(cls, data: dict | None) -> Target:
        if not data:
            return cls()
        return cls(**data)

    def to_dict(self) -> dict:
        out = {k: v for k, v in vars(self).items() if v is not None}
        if out.get("type") is None:
            out["type"] = "none"
        return out


@dataclass
class Step:
    kind: str
    duration: str | float | None = None
    distance: str | float | None = None
    until: str | None = None
    count: int | None = None
    target: Target = field(default_factory=Target)
    note: str | None = None
    grade: float | None = None
    exercise: str | None = None
    category: str | None = None
    weight: float | None = None
    reps: int | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def is_repeat(self) -> bool:
        return self.kind == "repeat"

    @classmethod
    def from_dict(cls, data: dict) -> Step:
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
            count=data.get("count"),
            target=Target.from_dict(data.get("target")),
            note=data.get("note"),
            grade=data.get("grade"),
            exercise=data.get("exercise"),
            category=data.get("category"),
            weight=data.get("weight"),
        )

    def to_dict(self) -> dict:
        if self.is_repeat:
            out: dict[str, Any] = {
                "kind": "repeat",
                "reps": self.reps,
                "steps": [s.to_dict() for s in self.steps],
            }
            if self.note:
                out["note"] = self.note
            return out
        out = {"kind": self.kind}
        for name in (
            "duration",
            "distance",
            "until",
            "count",
            "grade",
            "exercise",
            "category",
            "weight",
        ):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        target = self.target.to_dict()
        if target.get("type") != "none" or self.kind in ("run", "stride"):
            out["target"] = target
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Race:
    name: str
    date: dt.date
    priority: str = "A"
    distance: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Race:
        return cls(
            name=data["name"],
            date=dt.date.fromisoformat(data["date"]),
            priority=data.get("priority", "A"),
            distance=data.get("distance"),
        )

    def to_dict(self) -> dict:
        out = {"name": self.name, "date": self.date.isoformat(), "priority": self.priority}
        if self.distance:
            out["distance"] = self.distance
        return out


@dataclass
class Week:
    start: dt.date
    phase: str
    note: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Week:
        return cls(
            start=dt.date.fromisoformat(data["start"]), phase=data["phase"], note=data.get("note")
        )

    def to_dict(self) -> dict:
        out = {"start": self.start.isoformat(), "phase": self.phase}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Workout:
    name: str
    date: dt.date
    steps: list[Step]
    sport: str = "running"
    role: str | None = None
    phase: str | None = None
    notes: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Workout:
        return cls(
            name=data["name"],
            date=dt.date.fromisoformat(data["date"]),
            steps=[Step.from_dict(s) for s in data["steps"]],
            sport=data.get("sport", "running"),
            role=data.get("role"),
            phase=data.get("phase"),
            notes=data.get("notes"),
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "date": self.date.isoformat(),
            "sport": self.sport,
        }
        if self.role:
            out["role"] = self.role
        if self.phase:
            out["phase"] = self.phase
        if self.notes:
            out["notes"] = self.notes
        out["steps"] = [s.to_dict() for s in self.steps]
        return out


@dataclass
class Plan:
    plan: str
    workouts: list[Workout]
    race_date: dt.date | None = None
    races: list[Race] = field(default_factory=list)
    weeks: list[Week] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> Plan:
        validate(data)
        return cls(
            plan=data["plan"],
            workouts=[Workout.from_dict(w) for w in data["workouts"]],
            race_date=dt.date.fromisoformat(data["race_date"]) if data.get("race_date") else None,
            races=[Race.from_dict(r) for r in data.get("races", [])],
            weeks=[Week.from_dict(w) for w in data.get("weeks", [])],
        )

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"plan": self.plan}
        if self.race_date:
            out["race_date"] = self.race_date.isoformat()
        if self.races:
            out["races"] = [r.to_dict() for r in self.races]
        if self.weeks:
            out["weeks"] = [w.to_dict() for w in self.weeks]
        out["workouts"] = [w.to_dict() for w in self.workouts]
        return out

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(self.dumps(), encoding="utf-8")
        return path

    # Convenience the checks and UI lean on.
    @property
    def a_race(self) -> Race | None:
        for race in self.races:
            if race.priority == "A":
                return race
        if self.race_date:
            return Race(name="Goal race", date=self.race_date, priority="A")
        return None

    def sorted_workouts(self) -> list[Workout]:
        return sorted(self.workouts, key=lambda w: (w.date, w.name))


# --- validation -------------------------------------------------------------


def validate(data: dict) -> None:
    """Structural validation, then the semantic rules with friendly messages."""
    # jsonschema is 65% of the CLI's import time and only needed here, so it
    # is imported at the point of use. The schema dict above is plain data
    # and is safe to import everywhere (the LLM prompt embeds it).
    import jsonschema

    validator = jsonschema.Draft202012Validator(PLAN_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise PlanError(f"schema error at {where}: {first.message}")

    _check_iso_date(data.get("race_date"), "race_date")
    for race in data.get("races", []):
        _check_iso_date(race["date"], f"race {race['name']!r}")
    for week in data.get("weeks", []):
        _check_iso_date(week["start"], "week start")

    seen: set[tuple[str, str]] = set()
    for workout in data["workouts"]:
        _check_iso_date(workout["date"], f"workout {workout['name']!r}")
        key = (workout["date"], workout["name"].strip().lower())
        if key in seen:
            raise PlanError(
                f"two workouts on {workout['date']} share the name "
                f"{workout['name']!r}; names must be unique per day so the "
                f"tool can tell them apart when re-pushing"
            )
        seen.add(key)
        sport = workout.get("sport", "running")
        for index, step in enumerate(workout["steps"], start=1):
            _check_step(step, f"{workout['name']} step {index}", depth=0, sport=sport)


def _check_iso_date(value: str | None, what: str) -> None:
    if value is None:
        return
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise PlanError(f"{what}: bad date: {exc}") from exc


def _check_step(step: dict, where: str, depth: int, sport: str) -> None:
    if step["kind"] == "repeat":
        if depth + 1 > MAX_REPEAT_DEPTH:
            raise PlanError(
                f"{where}: repeats nested more than {MAX_REPEAT_DEPTH} deep; "
                f"Garmin watches render these unreliably -- flatten it"
            )
        for index, child in enumerate(step["steps"], start=1):
            _check_step(child, f"{where}.{index}", depth + 1, sport)
        if not any(c["kind"] != "repeat" for c in step["steps"]):
            raise PlanError(f"{where}: a repeat must contain at least one real step")
        return

    kind = step["kind"]
    if kind == "exercise":
        if not step.get("exercise"):
            raise PlanError(f"{where}: an exercise step needs an `exercise` name")
        bounds = [k for k in ("count", "duration", "until") if step.get(k) is not None]
        if len(bounds) != 1:
            raise PlanError(
                f"{where}: an exercise needs exactly one of count / duration / until, "
                f"got {bounds or 'none'}"
            )
    else:
        for forbidden in ("exercise", "category", "weight", "count"):
            if step.get(forbidden) is not None:
                raise PlanError(f"{where}: `{forbidden}` only belongs on an exercise step")
        bounds = [k for k in ("duration", "distance", "until") if step.get(k) is not None]
        if len(bounds) != 1:
            raise PlanError(
                f"{where}: needs exactly one of duration / distance / until, got {bounds or 'none'}"
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

    if (
        target["type"] in ("hr", "cadence", "power")
        and "low" in target
        and target["high"] <= target["low"]
    ):
        raise PlanError(
            f"{where}: {target['type']} target high ({target['high']}) "
            f"must be greater than low ({target['low']})"
        )
