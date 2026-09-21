"""Athlete profile: turns zone names into concrete pace / heart-rate targets.

The plan DSL talks in zone names ("easy", "threshold") so an LLM never has to
invent numbers. This module is the only place those become real paces, derived
from your threshold pace and LTHR. Change your threshold here and every future
plan re-derives correctly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .units import UnitError, format_pace, parse_pace

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

# Fractions of LTHR (Friel running zones). (low, high)
DEFAULT_HR_ZONES: dict[int, tuple[float, float]] = {
    1: (0.70, 0.85),
    2: (0.85, 0.89),
    3: (0.90, 0.94),
    4: (0.95, 0.99),
    5: (1.00, 1.06),
}

# Zone used to estimate the duration of a distance-based step that has no
# pace target of its own.
FALLBACK_ESTIMATE_ZONE = "easy"


class ProfileError(ValueError):
    """The athlete profile is missing or inconsistent."""


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
    hr_zones: dict[int, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_HR_ZONES)
    )
    # The parsed config file, kept so the [ai] block can be read from the same
    # place without loading the file twice.
    raw: dict = field(default_factory=dict, repr=False)

    # --- zone resolution ---

    def pace_zone(self, zone: str) -> tuple[float, float]:
        """Return (slower, faster) bounds in seconds per km."""
        key = zone.strip().lower()
        if key not in self.pace_zones:
            raise ProfileError(
                f"unknown pace zone {zone!r}; known: "
                + ", ".join(sorted(self.pace_zones))
            )
        slow_mult, fast_mult = self.pace_zones[key]
        return self.threshold_pace * slow_mult, self.threshold_pace * fast_mult

    def hr_zone(self, zone: int) -> tuple[int, int]:
        """Return (low, high) bpm."""
        if self.lthr is None:
            raise ProfileError(
                "this plan uses heart-rate zones but the profile has no `lthr`"
            )
        if zone not in self.hr_zones:
            raise ProfileError(
                f"unknown HR zone {zone!r}; known: "
                + ", ".join(str(z) for z in sorted(self.hr_zones))
            )
        low, high = self.hr_zones[zone]
        return round(self.lthr * low), round(self.lthr * high)

    def estimate_pace(self) -> float:
        """Seconds per km used when a distance step carries no pace target."""
        slow, fast = self.pace_zone(FALLBACK_ESTIMATE_ZONE)
        return (slow + fast) / 2

    def describe(self) -> str:
        lines = [f"Profile: {self.name}"]
        lines.append(
            f"  threshold pace  {format_pace(self.threshold_pace, self.imperial)}"
        )
        if self.lthr:
            lines.append(f"  LTHR            {self.lthr} bpm")
        if self.hr_max:
            lines.append(f"  HR max          {self.hr_max} bpm")
        lines.append("  pace zones:")
        for name in self.pace_zones:
            slow, fast = self.pace_zone(name)
            lines.append(
                f"    {name:<11} {format_pace(slow, self.imperial)}"
                f" - {format_pace(fast, self.imperial)}"
            )
        if self.lthr:
            lines.append("  heart-rate zones:")
            for zone in sorted(self.hr_zones):
                low, high = self.hr_zone(zone)
                lines.append(f"    Z{zone}          {low} - {high} bpm")
        return "\n".join(lines)

    # --- loading ---

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        imperial = str(data.get("units", "metric")).lower() in ("imperial", "us")

        pace_cfg = data.get("pace", {})
        if "threshold" not in pace_cfg:
            raise ProfileError("profile needs [pace] threshold, e.g. \"4:05/km\"")
        try:
            threshold = parse_pace(
                pace_cfg["threshold"], default_unit="mi" if imperial else "km"
            )
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

        hr_cfg = data.get("hr", {})
        hr_zones = dict(DEFAULT_HR_ZONES)
        for key, bounds in (hr_cfg.get("zones") or {}).items():
            hr_zones[int(key)] = (float(bounds[0]), float(bounds[1]))

        return cls(
            name=data.get("name", "athlete"),
            imperial=imperial,
            threshold_pace=threshold,
            lthr=hr_cfg.get("lthr"),
            hr_max=hr_cfg.get("max"),
            pace_zones=zones,
            hr_zones=hr_zones,
            raw=data,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        path = Path(path)
        if not path.exists():
            raise ProfileError(f"no profile at {path}")
        with path.open("rb") as handle:
            try:
                data = tomllib.load(handle)
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
        a generic serialiser would throw away. Only the fields the setup flow
        collects are written; anything else in `raw` is preserved verbatim
        underneath.
        """
        lines = [
            "# garmin-plan-push profile.",
            "# Written by `gpp init`; safe to edit by hand.",
            "",
            f'name = "{_escape(self.name)}"',
            f'units = "{"imperial" if self.imperial else "metric"}"',
            "",
            "[pace]",
            "# Roughly the pace you could hold in a hard one-hour race.",
            "# Every pace zone derives from this, so re-check it every 6-8 weeks.",
            f'threshold = "{format_pace(self.threshold_pace, self.imperial)}"',
        ]

        custom = {
            name: bounds
            for name, bounds in self.pace_zones.items()
            if DEFAULT_PACE_ZONES.get(name) != bounds
        }
        if custom:
            lines += ["", "[pace.zones]", "# First number is the SLOWER bound."]
            lines += [f"{name} = [{lo}, {hi}]" for name, (lo, hi) in custom.items()]

        if self.lthr or self.hr_max:
            lines += ["", "[hr]"]
            if self.lthr:
                lines.append(f"lthr = {self.lthr}")
            if self.hr_max:
                lines.append(f"max = {self.hr_max}")

        ai_block = _render_ai(self.raw.get("ai"))
        if ai_block:
            lines += ["", ai_block]

        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_toml(), encoding="utf-8")
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


def _render_ai(ai: dict | None) -> str:
    """Re-emit the [ai] block so saving a profile does not drop provider config."""
    if not ai:
        return ""
    lines = ["[ai]"]
    if ai.get("default"):
        lines.append(f'default = "{_escape(str(ai["default"]))}"')
    for name, entry in (ai.get("providers") or {}).items():
        lines += ["", f"[ai.providers.{name}]"]
        for key, value in entry.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f'{key} = "{_escape(str(value))}"')
    return "\n".join(lines)


DEFAULT_PROFILE_PATHS = (
    Path("profile.toml"),
    Path.home() / ".config" / "gpp" / "profile.toml",
)


def find_profile() -> Path | None:
    """The profile this machine should use, if one exists yet."""
    for candidate in DEFAULT_PROFILE_PATHS:
        if candidate.exists():
            return candidate
    return None


def default_save_path() -> Path:
    """Where a new profile goes: beside the project if writable, else XDG config."""
    return DEFAULT_PROFILE_PATHS[0]
