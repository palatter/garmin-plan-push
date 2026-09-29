"""Athlete profile: turns zone names into concrete pace / heart-rate targets.

The plan DSL talks in zone names ("easy", "threshold") so an LLM never has to
invent numbers. This module is the only place those become real paces. Three
models can back the zones:

  threshold  multipliers of a threshold pace (the default; needs one number)
  cs         fractions of critical speed, fitted from two all-out trials
  vdot       Daniels' intensities, from a recent race

The profile also carries what a coach would know that a model cannot guess:
injury history, standing constraints, availability, the goal race, and any
persistent instructions. All of it is fed into every prompt.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import os
import re
import tempfile
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from . import models
from .textfile import TextError, read_text
from .units import UnitError, format_pace, parse_distance, parse_duration, parse_pace

# Multipliers applied to threshold pace (seconds per km).
# >1 is SLOWER than threshold, <1 is FASTER. (slow_bound, fast_bound)
DEFAULT_PACE_ZONES: dict[str, tuple[float, float]] = {
    "recovery": (1.45, 1.26),
    "easy": (1.26, 1.15),
    "steady": (1.15, 1.09),
    "marathon": (1.09, 1.05),
    "threshold": (1.05, 0.99),
    "interval": (0.99, 0.92),
    "repetition": (0.92, 0.85),
}

# Daniels' letter names, for people who think in E/M/T/I/R.
ZONE_ALIASES: dict[str, str] = {
    "e": "easy",
    "m": "marathon",
    "t": "threshold",
    "i": "interval",
    "r": "repetition",
    "tempo": "threshold",
    "vo2": "interval",
    "vo2max": "interval",
    "jog": "recovery",
    "mp": "marathon",
    "hmp": "threshold",
}

# Fractions of LTHR (Friel running zones). (low, high)
DEFAULT_HR_ZONES: dict[int, tuple[float, float]] = {
    1: (0.70, 0.85),
    2: (0.85, 0.89),
    3: (0.90, 0.94),
    4: (0.95, 0.99),
    5: (1.00, 1.06),
}

ZONE_MODELS = ("threshold", "cs", "vdot")
INTENSITY_DISTRIBUTIONS = ("pyramidal", "polarized", "singles")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RACE_PRIORITIES = ("A", "B", "C")

# Zone used to estimate the duration of a distance-based step that has no
# pace target of its own.
FALLBACK_ESTIMATE_ZONE = "easy"


class ProfileError(ValueError):
    """The athlete profile is missing or inconsistent."""


@dataclass
class Availability:
    days: list[str] = field(default_factory=list)  # mon..sun, empty = any
    weekday_max_minutes: int | None = None
    weekend_max_minutes: int | None = None
    long_run_day: str | None = None
    sessions_per_week: int | None = None

    def allows(self, day: dt.date) -> bool:
        if not self.days:
            return True
        return WEEKDAYS[day.weekday()] in self.days

    def max_minutes(self, day: dt.date) -> int | None:
        return self.weekend_max_minutes if day.weekday() >= 5 else self.weekday_max_minutes

    def describe(self) -> str:
        parts = []
        if self.days:
            parts.append("runs on " + ", ".join(d.title() for d in self.days))
        if self.sessions_per_week:
            parts.append(f"{self.sessions_per_week} sessions a week")
        if self.weekday_max_minutes:
            parts.append(f"weekday sessions at most {self.weekday_max_minutes} min")
        if self.weekend_max_minutes:
            parts.append(f"weekend sessions at most {self.weekend_max_minutes} min")
        if self.long_run_day:
            parts.append(f"long run on {self.long_run_day.title()}")
        return "; ".join(parts)


@dataclass
class GoalRace:
    name: str
    date: dt.date
    distance: str | None = None
    priority: str = "A"
    goal_time: str | None = None

    def describe(self) -> str:
        text = f"{self.name} on {self.date.isoformat()}"
        if self.distance:
            text += f" ({self.distance})"
        if self.goal_time:
            text += f", goal {self.goal_time}"
        return text


@dataclass
class Profile:
    name: str = "athlete"
    imperial: bool = False
    threshold_pace: float = 300.0  # seconds per km
    lthr: int | None = None
    hr_max: int | None = None
    pace_zones: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_PACE_ZONES)
    )
    hr_zones: dict[int, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_HR_ZONES))
    zone_model: str = "threshold"
    # critical speed, from [cs] trials
    cs: models.CriticalSpeed | None = None
    # VDOT, from a [vdot] race
    vdot: float | None = None
    # running power, for pace<->power transpilation and power targets
    power_cp: int | None = None
    power_pace_at_cp: float | None = None  # seconds per km
    # what a coach would know
    instructions: str | None = None
    injuries: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    availability: Availability | None = None
    goal_race: GoalRace | None = None
    longest_recent_run_km: float | None = None
    recent_weekly_km: float | None = None
    language: str | None = None
    # pyramidal (default: better in recreational runners), polarized, or
    # singles (sub-threshold sessions, no intervals)
    intensity_distribution: str = "pyramidal"
    latitude: float | None = None
    longitude: float | None = None
    # The parsed config file, kept so the [ai] block can be read from the same
    # place without loading the file twice.
    raw: dict = field(default_factory=dict, repr=False)

    # --- zone resolution ---

    def zone_table(self) -> dict[str, tuple[float, float]]:
        """(slower, faster) seconds/km for every zone, under the active model."""
        if self.zone_model == "cs" and self.cs is not None:
            return models.cs_zones(self.cs.cs_mps)
        if self.zone_model == "vdot" and self.vdot is not None:
            return models.vdot_zones(self.vdot)
        return {
            name: (self.threshold_pace * slow, self.threshold_pace * fast)
            for name, (slow, fast) in self.pace_zones.items()
        }

    def canonical_zone(self, zone: str) -> str:
        key = zone.strip().lower()
        return ZONE_ALIASES.get(key, key)

    def pace_zone(self, zone: str) -> tuple[float, float]:
        """Return (slower, faster) bounds in seconds per km."""
        key = self.canonical_zone(zone)
        table = self.zone_table()
        if key not in table:
            raise ProfileError(f"unknown pace zone {zone!r}; known: " + ", ".join(sorted(table)))
        return table[key]

    def hr_zone(self, zone: int) -> tuple[int, int]:
        """Return (low, high) bpm."""
        if self.lthr is None:
            raise ProfileError("this plan uses heart-rate zones but the profile has no `lthr`")
        if zone not in self.hr_zones:
            raise ProfileError(
                f"unknown HR zone {zone!r}; known: "
                + ", ".join(str(z) for z in sorted(self.hr_zones))
            )
        low, high = self.hr_zones[zone]
        return round(self.lthr * low), round(self.lthr * high)

    def with_garmin_hr_zones(self, zones: list[tuple[int, int]]) -> Profile:
        """Adopt Garmin's five HR zones (bpm) as fractions of LTHR (#149).

        Garmin's zone 4 top is its lactate-threshold estimate; when the
        profile has no LTHR that boundary becomes it, so the zones survive a
        later threshold change as proportions rather than as stale numbers.
        """
        if len(zones) < 5:
            raise ProfileError(f"expected five Garmin HR zones, got {len(zones)}")
        lthr = self.lthr or int(zones[3][1])
        fractions = {
            index + 1: (low / lthr, high / lthr) for index, (low, high) in enumerate(zones[:5])
        }
        out = copy.copy(self)
        out.lthr = lthr
        out.hr_zones = {k: (round(lo, 3), round(hi, 3)) for k, (lo, hi) in fractions.items()}
        out.raw = deepcopy(self.raw)
        hr = dict(out.raw.get("hr") or {})
        hr["lthr"] = lthr
        hr["zones"] = {str(k): [lo, hi] for k, (lo, hi) in out.hr_zones.items()}
        out.raw["hr"] = hr
        return out

    def estimate_pace(self) -> float:
        """Seconds per km used when a distance step carries no pace target."""
        slow, fast = self.pace_zone(FALLBACK_ESTIMATE_ZONE)
        return (slow + fast) / 2

    def describe(self) -> str:
        lines = [f"Profile: {self.name}"]
        lines.append(f"  threshold pace  {format_pace(self.threshold_pace, self.imperial)}")
        if self.zone_model != "threshold":
            lines.append(f"  zone model      {self.zone_model}")
        if self.cs:
            lines.append(
                f"  critical speed  {format_pace(self.cs.cs_pace, self.imperial)}"
                f"  (D' {self.cs.d_prime_m:.0f} m)"
            )
        if self.vdot:
            lines.append(f"  VDOT            {self.vdot:.1f}")
        if self.lthr:
            lines.append(f"  LTHR            {self.lthr} bpm")
        if self.hr_max:
            lines.append(f"  HR max          {self.hr_max} bpm")
        if self.power_cp:
            lines.append(f"  critical power  {self.power_cp} W")
        lines.append("  pace zones:")
        for name, (slow, fast) in self.zone_table().items():
            lines.append(
                f"    {name:<11} {format_pace(slow, self.imperial)}"
                f" - {format_pace(fast, self.imperial)}"
            )
        if self.lthr:
            lines.append("  heart-rate zones:")
            for zone in sorted(self.hr_zones):
                low, high = self.hr_zone(zone)
                lines.append(f"    Z{zone}          {low} - {high} bpm")
        if self.goal_race:
            lines.append(f"  goal race       {self.goal_race.describe()}")
        if self.availability:
            lines.append(f"  availability    {self.availability.describe()}")
        if self.injuries:
            lines.append("  injuries        " + "; ".join(self.injuries))
        if self.constraints:
            lines.append("  constraints     " + "; ".join(self.constraints))
        if self.longest_recent_run_km:
            lines.append(f"  longest recent run  {self.longest_recent_run_km:g} km")
        if self.recent_weekly_km:
            lines.append(f"  recent weekly volume {self.recent_weekly_km:g} km")
        if self.intensity_distribution != "pyramidal":
            lines.append(f"  intensity model {self.intensity_distribution}")
        if self.instructions:
            lines.append(f"  instructions    {self.instructions}")
        return "\n".join(lines)

    # --- loading ---

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        imperial = str(data.get("units", "metric")).lower() in ("imperial", "us")
        unit = "mi" if imperial else "km"

        pace_cfg = data.get("pace", {})
        if "threshold" not in pace_cfg:
            raise ProfileError('profile needs [pace] threshold, e.g. "4:05/km"')
        try:
            threshold = parse_pace(pace_cfg["threshold"], default_unit=unit)
        except UnitError as exc:
            raise ProfileError(str(exc)) from exc

        zones = dict(DEFAULT_PACE_ZONES)
        for name, bounds in (pace_cfg.get("zones") or {}).items():
            if len(bounds) != 2:
                raise ProfileError(f"pace zone {name!r} needs exactly two multipliers")
            slow, fast = float(bounds[0]), float(bounds[1])
            if slow < fast:
                raise ProfileError(
                    f"pace zone {name!r}: first multiplier is the SLOWER bound "
                    f"so it must be the larger number (got {slow} then {fast})"
                )
            zones[name.lower()] = (slow, fast)

        zone_model = str(pace_cfg.get("model", "threshold")).lower()
        if zone_model not in ZONE_MODELS:
            raise ProfileError(f"pace model must be one of {', '.join(ZONE_MODELS)}")

        hr_cfg = data.get("hr", {})
        hr_zones = dict(DEFAULT_HR_ZONES)
        for key, bounds in (hr_cfg.get("zones") or {}).items():
            hr_zones[int(key)] = (float(bounds[0]), float(bounds[1]))

        cs = None
        cs_cfg = data.get("cs") or {}
        if cs_cfg.get("trials"):
            trials = []
            for trial in cs_cfg["trials"]:
                try:
                    trials.append(
                        (parse_distance(trial["distance"]), parse_duration(trial["time"]))
                    )
                except (KeyError, UnitError) as exc:
                    raise ProfileError(f"[cs] trial needs distance and time: {exc}") from exc
            try:
                cs = models.critical_speed(trials)
            except models.ModelError as exc:
                raise ProfileError(f"[cs]: {exc}") from exc
        if zone_model == "cs" and cs is None:
            raise ProfileError('pace model "cs" needs [cs] trials')

        vdot = None
        vdot_cfg = data.get("vdot") or {}
        if vdot_cfg.get("value"):
            vdot = float(vdot_cfg["value"])
        elif vdot_cfg.get("distance") and vdot_cfg.get("time"):
            try:
                vdot = models.vdot_from_race(
                    parse_distance(vdot_cfg["distance"]), parse_duration(vdot_cfg["time"])
                )
            except (UnitError, models.ModelError) as exc:
                raise ProfileError(f"[vdot]: {exc}") from exc
        if zone_model == "vdot" and vdot is None:
            raise ProfileError('pace model "vdot" needs a [vdot] race (distance + time) or value')

        power_cfg = data.get("power") or {}
        power_cp = int(power_cfg["cp"]) if power_cfg.get("cp") else None
        power_pace = None
        if power_cfg.get("pace_at_cp"):
            try:
                power_pace = parse_pace(power_cfg["pace_at_cp"], default_unit=unit)
            except UnitError as exc:
                raise ProfileError(f"[power] pace_at_cp: {exc}") from exc
        if power_cp and not power_pace:
            # Without a matched pace, assume CP is roughly a threshold effort.
            power_pace = threshold

        athlete = data.get("athlete") or {}
        availability = None
        avail_cfg = data.get("availability") or {}
        if avail_cfg:
            days = [str(d).lower()[:3] for d in avail_cfg.get("days", [])]
            bad = [d for d in days if d not in WEEKDAYS]
            if bad:
                raise ProfileError(f"[availability] days must be mon..sun, got {bad}")
            long_day = avail_cfg.get("long_run_day")
            if long_day and str(long_day).lower()[:3] not in WEEKDAYS:
                raise ProfileError(
                    f"[availability] long_run_day must be mon..sun, got {long_day!r}"
                )
            availability = Availability(
                days=days,
                weekday_max_minutes=avail_cfg.get("weekday_max_minutes"),
                weekend_max_minutes=avail_cfg.get("weekend_max_minutes"),
                long_run_day=str(long_day).lower()[:3] if long_day else None,
                sessions_per_week=avail_cfg.get("sessions_per_week"),
            )

        goal = None
        goal_cfg = data.get("goal_race") or {}
        priority = str(goal_cfg.get("priority", "A")).upper()
        if priority not in RACE_PRIORITIES:
            raise ProfileError(f"[goal_race] priority must be A, B or C, got {priority!r}")
        if goal_cfg.get("date"):
            try:
                goal = GoalRace(
                    name=goal_cfg.get("name", "Goal race"),
                    date=dt.date.fromisoformat(str(goal_cfg["date"])),
                    distance=goal_cfg.get("distance"),
                    priority=priority,
                    goal_time=goal_cfg.get("goal_time"),
                )
            except ValueError as exc:
                raise ProfileError(f"[goal_race] date: {exc}") from exc

        location = data.get("location") or {}
        tid = str(athlete.get("intensity_distribution", "pyramidal")).lower()
        if tid not in INTENSITY_DISTRIBUTIONS:
            raise ProfileError(
                f"[athlete] intensity_distribution must be one of {INTENSITY_DISTRIBUTIONS}, got {tid!r}"
            )

        return cls(
            name=data.get("name", "athlete"),
            imperial=imperial,
            threshold_pace=threshold,
            lthr=hr_cfg.get("lthr"),
            hr_max=hr_cfg.get("max"),
            pace_zones=zones,
            hr_zones=hr_zones,
            zone_model=zone_model,
            cs=cs,
            vdot=vdot,
            power_cp=power_cp,
            power_pace_at_cp=power_pace,
            instructions=(athlete.get("instructions") or data.get("instructions") or None),
            injuries=[str(x) for x in athlete.get("injuries", [])],
            constraints=[str(x) for x in athlete.get("constraints", [])],
            availability=availability,
            goal_race=goal,
            longest_recent_run_km=athlete.get("longest_recent_run_km"),
            recent_weekly_km=athlete.get("recent_weekly_km"),
            language=athlete.get("language"),
            intensity_distribution=tid,
            latitude=location.get("latitude"),
            longitude=location.get("longitude"),
            raw=data,
        )

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        path = Path(path)
        if not path.exists():
            raise ProfileError(f"no profile at {path}")
        try:
            text = read_text(path)
        except TextError as exc:
            raise ProfileError(str(exc)) from exc
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            # The file invites hand-editing, so a typo here is expected.
            # Callers recover from ProfileError; a raw TOMLDecodeError
            # escaping would take the whole app down instead.
            raise ProfileError(f"{path} is not valid TOML: {exc}") from exc
        return cls.from_dict(data)

    # --- saving ---

    def to_toml(self) -> str:
        """Render this profile as a config file.

        Hand-rolled rather than via a TOML writer: the output is a file a human
        will read and edit, so it keeps comments and a stable field order that
        a generic serialiser would throw away.
        """
        raw = self.raw or {}
        lines = [
            "# garmin-plan-push profile.",
            "# Written by gpp; safe to edit by hand.",
            "",
            f'name = "{_escape(self.name)}"',
            f'units = "{"imperial" if self.imperial else "metric"}"',
        ]
        # Keys gpp does not know are kept, so a save never loses a setting a
        # newer version (or the user) put there. Top-level values must come
        # before the first table.
        others = {
            k: v
            for k, v in raw.items()
            if k not in _MANAGED[""] or (k == "ai" and not isinstance(v, dict))
        }
        lines += [_kv(k, v) for k, v in others.items() if not isinstance(v, dict)]
        lines += [
            "",
            "[pace]",
            "# Roughly the pace you could hold in a hard one-hour race.",
            "# Every pace zone derives from this, so re-check it every 6-8 weeks.",
            f'threshold = "{format_pace(self.threshold_pace, self.imperial)}"',
        ]
        if self.zone_model != "threshold":
            lines.append(f'model = "{self.zone_model}"')
        lines += _extra(raw, "pace")

        custom = {
            name: bounds
            for name, bounds in self.pace_zones.items()
            if DEFAULT_PACE_ZONES.get(name) != bounds
        }
        if custom:
            lines += ["", "[pace.zones]", "# First number is the SLOWER bound."]
            lines += [f"{_key(name)} = [{lo}, {hi}]" for name, (lo, hi) in custom.items()]

        hr_extra = _extra(raw, "hr")
        if self.lthr or self.hr_max or hr_extra:
            lines += ["", "[hr]"]
            if self.lthr:
                lines.append(f"lthr = {self.lthr}")
            if self.hr_max:
                lines.append(f"max = {self.hr_max}")
            lines += hr_extra
            if self.hr_zones != DEFAULT_HR_ZONES:
                lines += ["", "[hr.zones]", "# Fractions of LTHR: low, high."]
                for zone, (low, high) in sorted(self.hr_zones.items()):
                    lines.append(f'"{zone}" = [{low}, {high}]')

        cs_cfg = raw.get("cs") or {}
        cs_extra = _extra(raw, "cs")
        if cs_cfg.get("trials") or cs_extra:
            lines += ["", "[cs]", "# Two or more all-out trials of 2-20 minutes."]
            if cs_cfg.get("trials"):
                lines.append(_kv("trials", cs_cfg["trials"]))
            lines += cs_extra

        vdot_cfg = raw.get("vdot") or {}
        if vdot_cfg:
            lines += ["", "[vdot]"]
            for key in ("distance", "time", "value"):
                if vdot_cfg.get(key) is not None:
                    lines.append(_kv(key, vdot_cfg[key]))
            lines += _extra(raw, "vdot")

        power_extra = _extra(raw, "power")
        if self.power_cp or power_extra:
            lines += ["", "[power]"]
            if self.power_cp:
                lines.append(f"cp = {self.power_cp}")
            if self.power_pace_at_cp:
                lines.append(f'pace_at_cp = "{format_pace(self.power_pace_at_cp, self.imperial)}"')
            lines += power_extra

        athlete_lines = []
        if self.instructions:
            athlete_lines.append(f'instructions = "{_escape(self.instructions)}"')
        if self.injuries:
            athlete_lines.append(
                "injuries = [" + ", ".join(f'"{_escape(x)}"' for x in self.injuries) + "]"
            )
        if self.constraints:
            athlete_lines.append(
                "constraints = [" + ", ".join(f'"{_escape(x)}"' for x in self.constraints) + "]"
            )
        if self.longest_recent_run_km is not None:
            athlete_lines.append(f"longest_recent_run_km = {self.longest_recent_run_km}")
        if self.recent_weekly_km is not None:
            athlete_lines.append(f"recent_weekly_km = {self.recent_weekly_km}")
        if self.language:
            athlete_lines.append(f'language = "{_escape(self.language)}"')
        if self.intensity_distribution != "pyramidal":
            athlete_lines.append(f'intensity_distribution = "{self.intensity_distribution}"')
        athlete_lines += _extra(raw, "athlete")
        if athlete_lines:
            lines += [
                "",
                "[athlete]",
                "# What a coach would know and a model cannot guess.",
                *athlete_lines,
            ]

        availability_extra = _extra(raw, "availability")
        if self.availability or availability_extra:
            a = self.availability
            lines += ["", "[availability]"]
            if a and a.days:
                lines.append(_kv("days", list(a.days)))
            for key, value in (
                ("weekday_max_minutes", a.weekday_max_minutes if a else None),
                ("weekend_max_minutes", a.weekend_max_minutes if a else None),
                ("sessions_per_week", a.sessions_per_week if a else None),
            ):
                if value is not None:
                    lines.append(f"{key} = {int(value)}")
            if a and a.long_run_day:
                lines.append(_kv("long_run_day", a.long_run_day))
            lines += availability_extra

        goal_extra = _extra(raw, "goal_race")
        if self.goal_race or goal_extra:
            lines += ["", "[goal_race]"]
            g = self.goal_race
            if g:
                lines += [f'name = "{_escape(g.name)}"', f'date = "{g.date.isoformat()}"']
                if g.distance:
                    lines.append(f'distance = "{_escape(g.distance)}"')
                lines.append(_kv("priority", g.priority))
                if g.goal_time:
                    lines.append(f'goal_time = "{_escape(g.goal_time)}"')
            lines += goal_extra

        location_extra = _extra(raw, "location")
        has_location = self.latitude is not None and self.longitude is not None
        if has_location or location_extra:
            lines += ["", "[location]"]
            if has_location:
                lines += [f"latitude = {self.latitude}", f"longitude = {self.longitude}"]
            lines += location_extra

        # Tables gpp does not manage, such as [defaults] (device, port).
        for name, table in others.items():
            if isinstance(table, dict):
                lines += ["", f"[{_key(name)}]", *(_kv(k, v) for k, v in table.items())]

        ai_block = _render_ai(raw.get("ai"))
        if ai_block:
            lines += ["", ai_block]

        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: str | Path) -> Path:
        """Write the profile in one step: an interrupted save leaves the old file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_toml()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            if path.exists():
                os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _escape(value: str) -> str:
    """Escape a string for a TOML basic string.

    Newlines and control characters are the ones that matter: a pasted name
    with a trailing newline would otherwise split the line and produce a file
    that can never be parsed again, locking the user out of their own config.
    """
    out = []
    for char in value:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


# The keys to_toml writes itself, per table ("" is the top level). Anything
# else found in the file is written back as it was read.
_MANAGED: dict[str, set[str]] = {
    "": {
        "name",
        "units",
        "instructions",
        "pace",
        "hr",
        "cs",
        "vdot",
        "power",
        "athlete",
        "availability",
        "goal_race",
        "location",
        "ai",
    },
    "pace": {"threshold", "model", "zones"},
    "hr": {"lthr", "max", "zones"},
    "cs": {"trials"},
    "vdot": {"distance", "time", "value"},
    "power": {"cp", "pace_at_cp"},
    "athlete": {
        "instructions",
        "injuries",
        "constraints",
        "longest_recent_run_km",
        "recent_weekly_km",
        "language",
        "intensity_distribution",
    },
    "availability": {
        "days",
        "weekday_max_minutes",
        "weekend_max_minutes",
        "sessions_per_week",
        "long_run_day",
    },
    "goal_race": {"name", "date", "distance", "priority", "goal_time"},
    "location": {"latitude", "longitude"},
}

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name: object) -> str:
    """A TOML key: bare when it can be, quoted otherwise ("my easy", "lätt")."""
    text = str(name)
    return text if _BARE_KEY.match(text) else f'"{_escape(text)}"'


def _value(value) -> str:
    """Any value tomllib can read, written back as TOML."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    return f'"{_escape(str(value))}"'


def _kv(key: object, value) -> str:
    return f"{_key(key)} = {_value(value)}"


def _extra(raw: dict, table: str) -> list[str]:
    """Lines for the keys of `table` that to_toml does not write itself."""
    section = raw.get(table)
    if not isinstance(section, dict):
        return []
    return [_kv(k, v) for k, v in section.items() if k not in _MANAGED[table]]


def _render_ai(ai: dict | None) -> str:
    """Re-emit the [ai] block so saving a profile does not drop provider config."""
    if not ai or not isinstance(ai, dict):
        return ""
    lines = ["[ai]"]
    providers = ai.get("providers")
    tables = isinstance(providers, dict) and all(isinstance(e, dict) for e in providers.values())
    for key, value in ai.items():
        if key != "providers" or not tables:
            lines.append(_kv(key, value))
    if tables:
        for name, entry in providers.items():
            lines += ["", f"[ai.providers.{_key(name)}]"]
            lines += [_kv(key, value) for key, value in entry.items()]
    return "\n".join(lines)


DEFAULT_PROFILE_PATHS = (
    Path("profile.toml"),
    Path.home() / ".config" / "gpp" / "profile.toml",
)

PROFILES_DIR = Path.home() / ".config" / "gpp" / "profiles"


def find_profile(athlete: str | None = None) -> Path | None:
    """The profile this machine should use, if one exists yet.

    With `athlete`, look for a named profile under ~/.config/gpp/profiles/ so
    several people can share one install.
    """
    if athlete:
        candidate = PROFILES_DIR / f"{athlete}.toml"
        return candidate if candidate.exists() else None
    for candidate in DEFAULT_PROFILE_PATHS:
        if candidate.exists():
            return candidate
    return None


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


def default_save_path(athlete: str | None = None) -> Path:
    """Where a new profile goes."""
    if athlete:
        return PROFILES_DIR / f"{athlete}.toml"
    return DEFAULT_PROFILE_PATHS[0]
