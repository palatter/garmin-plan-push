"""Parsing and formatting of the human units used in the plan DSL.

Everything is normalised to SI internally: seconds, metres, metres/second.
Pace is carried as seconds-per-kilometre where it needs to stay human, and
converted to m/s only at the Garmin boundary.
"""

from __future__ import annotations

import re

METRES_PER_MILE = 1609.344


class UnitError(ValueError):
    """A duration/distance/pace string could not be understood."""


# --- Duration ---------------------------------------------------------------

_DUR_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|m|min|mins|s|sec|secs)", re.I)
_DUR_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")
_DUR_MMSS = re.compile(r"^(\d{1,3}):(\d{2}(?:\.\d+)?)$")

_DUR_FACTOR = {
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
}


def parse_duration(text: str | int | float) -> float:
    """Return seconds.

    Accepts "90s", "15m", "1h30m", "1h", "45:00" (mm:ss), "1:05:00" (h:mm:ss),
    or a bare number (interpreted as seconds).
    """
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    if not s:
        raise UnitError("empty duration")

    clock = _DUR_CLOCK.match(s)
    if clock:
        hours, minutes, seconds = clock.groups()
        return float(hours or 0) * 3600 + float(minutes) * 60 + float(seconds)

    mmss = _DUR_MMSS.match(s)
    if mmss:
        minutes, seconds = mmss.groups()
        return float(minutes) * 60 + float(seconds)

    total = 0.0
    matched = 0
    for value, unit in _DUR_TOKEN.findall(s):
        total += float(value) * _DUR_FACTOR[unit.lower()]
        matched += 1
    if matched:
        # Reject junk like "15m banana" that partially matched.
        stripped = _DUR_TOKEN.sub("", s).strip()
        if stripped:
            raise UnitError(f"unparsed text in duration {text!r}: {stripped!r}")
        return total

    try:
        return float(s)
    except ValueError as exc:
        raise UnitError(f"cannot parse duration {text!r}") from exc


def format_duration(seconds: float) -> str:
    """Render seconds as h:mm:ss or m:ss."""
    seconds = round(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --- Distance ---------------------------------------------------------------

_DIST = re.compile(r"^(\d+(?:\.\d+)?)\s*(km|k|m|mi|mile|miles|yd|yds)?$", re.I)

_DIST_FACTOR = {
    "km": 1000.0,
    "k": 1000.0,
    "m": 1.0,
    "mi": METRES_PER_MILE,
    "mile": METRES_PER_MILE,
    "miles": METRES_PER_MILE,
    "yd": 0.9144,
    "yds": 0.9144,
}


def parse_distance(text: str | int | float) -> float:
    """Return metres. A bare number is metres. "1km", "800m", "5mi", "3.1 miles"."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    match = _DIST.match(s)
    if not match:
        raise UnitError(f"cannot parse distance {text!r}")
    value, unit = match.groups()
    return float(value) * _DIST_FACTOR[(unit or "m").lower()]


def format_distance(metres: float, imperial: bool = False) -> str:
    if imperial:
        miles = metres / METRES_PER_MILE
        if miles < 0.25:
            return f"{metres / 0.9144:.0f} yd"
        return f"{miles:.2f} mi"
    if metres < 1000:
        return f"{metres:.0f} m"
    return f"{metres / 1000:.2f} km"


# --- Pace -------------------------------------------------------------------

_PACE = re.compile(r"^(\d{1,3}):(\d{2}(?:\.\d+)?)\s*(?:/\s*(km|k|mi|mile))?$", re.I)


def parse_pace(text: str, default_unit: str = "km") -> float:
    """Return seconds per kilometre.

    Accepts "4:00/km", "7:30/mi", or "4:00" (uses `default_unit`).
    """
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    match = _PACE.match(s)
    if not match:
        raise UnitError(f"cannot parse pace {text!r} (expected like '4:05/km')")
    minutes, seconds, unit = match.groups()
    per_unit = float(minutes) * 60 + float(seconds)
    unit = (unit or default_unit).lower()
    if unit in ("mi", "mile"):
        return per_unit / (METRES_PER_MILE / 1000.0)
    return per_unit


def format_pace(sec_per_km: float, imperial: bool = False) -> str:
    if imperial:
        sec_per_km = sec_per_km * (METRES_PER_MILE / 1000.0)
    total = round(sec_per_km)
    minutes, seconds = divmod(total, 60)
    suffix = "/mi" if imperial else "/km"
    return f"{minutes}:{seconds:02d}{suffix}"


def pace_to_mps(sec_per_km: float) -> float:
    """Seconds-per-km -> metres-per-second, the unit Garmin stores."""
    if sec_per_km <= 0:
        raise UnitError("pace must be positive")
    return 1000.0 / sec_per_km


def mps_to_pace(mps: float) -> float:
    """Metres-per-second -> seconds-per-km."""
    if mps <= 0:
        raise UnitError("speed must be positive")
    return 1000.0 / mps
