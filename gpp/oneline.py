"""One-line workouts: "20min warmup, 6x3m @ threshold w/ 2min recovery, 10 min cooldown".

TrainingPeaks lets coaches type a workout in a sentence and get a structured
session back. This is that, for people who know exactly what they want and
do not need a model to write it.

Grammar, informally: comma-separated segments, each

    [N x] <extent> [<kind word>] [@ <target>] [w/ <extent> <recovery word>]

  extent   20min | 20m (under 100 = minutes) | 800m (100+ = metres) | 1km |
           5k | 1mi | 30s | lap
  kind     warmup/wu, cooldown/cd, recovery/recover/jog/rest, strides, run
  target   a zone name or alias (easy, T, MP, 5k, 10k, HM ...), an explicit
           pace like 4:00/km, an HR zone like Z2, watts like 280w, RPE 7,
           or a range 4:00-4:10/km
"""

from __future__ import annotations

import re

from .plan import Plan, PlanError, Workout

_KIND_WORDS = {
    "warmup": "warmup",
    "wu": "warmup",
    "warm-up": "warmup",
    "warm": "warmup",
    "cooldown": "cooldown",
    "cd": "cooldown",
    "cool-down": "cooldown",
    "cool": "cooldown",
    "recovery": "recover",
    "recover": "recover",
    "jog": "recover",
    "float": "recover",
    "rest": "rest",
    "walk": "rest",
    "standing": "rest",
    "stride": "stride",
    "strides": "stride",
    "run": "run",
    "steady": "run",
    "tempo": "run",
    "hard": "run",
    "easy": "run",
}
_RACE_PACES = {
    "5k": "interval",
    "5km": "interval",
    "3k": "repetition",
    "mile": "repetition",
    "10k": "threshold",
    "10km": "threshold",
    "15k": "threshold",
    "hm": "threshold",
    "half": "threshold",
    "hmp": "threshold",
    "mp": "marathon",
    "marathon": "marathon",
}
_ZONE_WORDS = {
    "recovery",
    "easy",
    "steady",
    "marathon",
    "threshold",
    "interval",
    "repetition",
    "e",
    "m",
    "t",
    "i",
    "r",
    "tempo",
    "vo2",
    "vo2max",
    "jog",
}

_EXTENT = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>min|mins|minutes|m|s|sec|secs|seconds|km|k|mi|mile|miles|h|hr|hours?)?$",
    re.I,
)
_PACE = re.compile(r"^(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?\s*(?:/\s*(km|k|mi|mile))?$", re.I)
_HR = re.compile(r"^z(\d)(?:\s*hr)?$", re.I)
_WATTS = re.compile(r"^(\d{2,4})(?:\s*-\s*(\d{2,4}))?\s*w(?:atts)?$", re.I)
_RPE = re.compile(r"^rpe\s*(\d{1,2})$", re.I)
_REPS = re.compile(r"^(\d+)\s*[x\u00d7]\s*(.+)$", re.I)


class OneLineError(ValueError):
    """The sentence could not be read as a workout."""


def parse_extent(text: str) -> dict:
    """'20min' -> {'duration': '20m'}, '800m' -> {'distance': '800m'}, 'lap' -> until."""
    t = text.strip().lower()
    if t in ("lap", "button", "press lap"):
        return {"until": "lap"}
    m = _EXTENT.match(t)
    if not m:
        raise OneLineError(f"cannot read the length {text!r}")
    num = float(m.group("num"))
    unit = (m.group("unit") or "").lower()
    if unit in ("", "min", "mins", "minutes"):
        return {"duration": f"{num:g}m"}
    if unit == "m":
        return {"distance": f"{num:g}m"} if num >= 100 else {"duration": f"{num:g}m"}
    if unit in ("s", "sec", "secs", "seconds"):
        return {"duration": f"{num:g}s"}
    if unit in ("h", "hr", "hour", "hours"):
        return {"duration": f"{num * 60:g}m"}
    if unit in ("km", "k"):
        return {"distance": f"{num:g}km"}
    return {"distance": f"{num:g}mi"}


def parse_target(text: str) -> dict:
    t = text.strip().lower().replace("pace", "").strip()
    if not t or t in ("none", "open", "no target"):
        return {"type": "none"}
    if m := _PACE.match(t):
        unit = (m.group(3) or "km").lower()
        suffix = "/mi" if unit in ("mi", "mile") else "/km"
        first, second = m.group(1), m.group(2)
        if second:
            a, b = first + suffix, second + suffix
            from .units import parse_pace

            slow, fast = (a, b) if parse_pace(a) >= parse_pace(b) else (b, a)
            return {"type": "pace", "slow": slow, "fast": fast}
        return {"type": "pace", "slow": first + suffix, "fast": first + suffix}
    if m := _HR.match(t):
        return {"type": "hr", "zone": int(m.group(1))}
    if m := _WATTS.match(t):
        low = int(m.group(1))
        high = int(m.group(2) or low + 10)
        return {
            "type": "power",
            "low": min(low, high),
            "high": max(low, high) if high != low else low + 10,
        }
    if m := _RPE.match(t):
        return {"type": "rpe", "value": max(1, min(10, int(m.group(1))))}
    if t in _RACE_PACES:
        return {"type": "pace", "zone": _RACE_PACES[t]}
    if t in _ZONE_WORDS:
        return {"type": "pace", "zone": t}
    raise OneLineError(f"cannot read the target {text!r}")


def _parse_segment(text: str) -> list[dict]:
    """One comma-separated chunk -> one or more steps."""
    seg = text.strip()
    if not seg:
        return []
    reps = None
    if m := _REPS.match(seg):
        reps, seg = int(m.group(1)), m.group(2).strip()

    recovery = None
    parts = re.split(r"\s+(?:w/|with)\s+", seg, maxsplit=1)
    if len(parts) == 2:
        seg, rec_text = parts
        recovery = _parse_simple(rec_text, default_kind="recover")

    step = _parse_simple(seg, default_kind="run")
    if reps:
        inner = [step] + ([recovery] if recovery else [])
        return [{"kind": "repeat", "reps": reps, "steps": inner}]
    return [step] + ([recovery] if recovery else [])


def _parse_simple(text: str, default_kind: str) -> dict:
    t = text.strip()
    target_text = None
    if "@" in t:
        t, target_text = (x.strip() for x in t.split("@", 1))
    words = t.split()
    if not words:
        raise OneLineError("empty step")

    # The extent is the first token, possibly with its unit as the next token.
    extent_text = words[0]
    rest = words[1:]
    if rest and _EXTENT.match(f"{extent_text} {rest[0]}") and not _EXTENT.match(extent_text):
        extent_text, rest = f"{extent_text} {rest[0]}", rest[1:]
    elif rest and rest[0].lower() in (
        "min",
        "mins",
        "minutes",
        "sec",
        "secs",
        "seconds",
        "km",
        "k",
        "mi",
        "miles",
    ):
        extent_text, rest = f"{extent_text}{rest[0]}", rest[1:]
    step = parse_extent(extent_text)

    kind = default_kind
    leftovers = []
    for word in rest:
        w = word.lower().strip(",.")
        if w in _KIND_WORDS:
            kind = _KIND_WORDS[w]
            if w in ("easy", "steady", "tempo") and target_text is None:
                target_text = {"easy": "easy", "steady": "steady", "tempo": "threshold"}[w]
        else:
            leftovers.append(word)
    step["kind"] = kind
    if target_text is None and leftovers:
        target_text = " ".join(leftovers)
    if target_text is not None:
        step["target"] = parse_target(target_text)
    elif kind in ("warmup", "cooldown"):
        step["target"] = {"type": "pace", "zone": "easy"}
    elif kind == "recover":
        step["target"] = {"type": "pace", "zone": "recovery"}
    else:
        step["target"] = {"type": "none"}
    return step


def parse_workout(line: str, name: str | None = None, date: str = "2000-01-03") -> Workout:
    """A sentence in, a validated Workout out."""
    segments = [s for s in re.split(r"\s*,\s*|\s*;\s*|\s+then\s+", line.strip()) if s]
    if not segments:
        raise OneLineError("nothing to parse")
    steps: list[dict] = []
    for seg in segments:
        steps.extend(_parse_segment(seg))
    data = {"name": name or _auto_name(line), "date": date, "steps": steps}
    try:
        return Plan.from_dict({"plan": "_", "workouts": [data]}).workouts[0]
    except PlanError as exc:
        raise OneLineError(str(exc)) from exc


def _auto_name(line: str) -> str:
    cleaned = re.sub(r"\s+", " ", line.strip())
    return (cleaned[:60] + "...") if len(cleaned) > 60 else cleaned
