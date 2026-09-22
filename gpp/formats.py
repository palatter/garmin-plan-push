"""Import and export: intervals.icu text, Zwift ZWO, MRC/ERG, and share bundles.

The DSL is the contract; these are adapters at its edge. Each export is a
faithful projection of what the step tree can say in that format, and each
import produces a validated plan or workout -- nothing arrives unchecked.

  intervals.icu  their plain-text workout syntax ("- 10m Z2", "3x", pace
                 ranges), the closest free tool and a natural sibling of ours
  ZWO            Zwift's XML; running workouts use Power as a fraction of
                 threshold pace, which is how Zwift itself does it
  MRC / ERG      the time-percent tables TrainingPeaks and trainers read;
                 percent of threshold, minutes on the left
  share bundle   our own JSON envelope for sending a plan to a friend

FIT is deliberately not here: encoding it needs a binary library and could
not be verified against a device from this machine. The sideload path is
the ZWO export into Garmin Connect's importer, or the direct push.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass

from .compile import resolve_pace_bounds
from .plan import Plan, PlanError, Step, Workout
from .profile import Profile
from .units import format_pace, parse_distance, parse_duration

SHARE_VERSION = 1


class FormatError(ValueError):
    """The file could not be read or written in that format."""


# --- helpers ----------------------------------------------------------------


def _seconds(step: Step, profile: Profile) -> float:
    from .compile import _estimate_step_seconds

    return _estimate_step_seconds(step, profile)


def _fraction_of_threshold(step: Step, profile: Profile) -> float:
    """Speed as a fraction of threshold speed, from the step's target."""
    t = step.target
    if t.type == "pace":
        slow, fast = resolve_pace_bounds(t, profile)
        mid = (slow + fast) / 2
        return profile.threshold_pace / mid
    if t.type == "power" and t.low and profile.power_cp:
        return ((t.low + (t.high or t.low)) / 2) / profile.power_cp
    return {"warmup": 0.7, "cooldown": 0.7, "recover": 0.6, "rest": 0.4, "stride": 1.15}.get(
        step.kind, 0.85
    )


def _flatten(steps: list[Step]) -> list[Step]:
    out = []
    for s in steps:
        if s.is_repeat:
            for _ in range(int(s.reps or 1)):
                out.extend(_flatten(s.steps))
        else:
            out.append(s)
    return out


# --- intervals.icu text -----------------------------------------------------

_ICU_ZONE_BY_INTENSITY = {
    "recovery": "Z1",
    "easy": "Z2",
    "steady": "Z3",
    "marathon": "Z3",
    "threshold": "Z4",
    "interval": "Z5",
    "repetition": "Z6",
}


def _icu_extent(step: Step) -> str:
    if step.duration is not None:
        secs = parse_duration(step.duration)
        return f"{secs / 60:g}m" if secs % 60 == 0 else f"{secs:g}s"
    if step.distance is not None:
        m = parse_distance(step.distance)
        return f"{m / 1000:g}km" if m >= 1000 else f"{m:g}m"
    return "lap"


def _icu_target(step: Step, profile: Profile) -> str:
    t = step.target
    if t.type == "pace":
        slow, fast = resolve_pace_bounds(t, profile)
        return f"{format_pace(fast, False)[:-3]}-{format_pace(slow, False)}"
    if t.type == "hr":
        return f"Z{t.zone} HR" if t.zone else f"{t.low}-{t.high}bpm"
    if t.type == "power":
        return f"Z{t.zone}" if t.zone else f"{t.low}-{t.high}w"
    if t.type == "rpe":
        return f"RPE {t.value}"
    return ""


def export_icu(workout: Workout, profile: Profile) -> str:
    lines = [workout.name, ""]
    if workout.notes:
        lines += [workout.notes, ""]

    def emit(steps: list[Step], indent: str = "") -> None:
        for s in steps:
            if s.is_repeat:
                lines.append(f"{indent}{s.reps}x")
                emit(s.steps, indent + "  ")
                continue
            label = {"warmup": "Warmup", "cooldown": "Cooldown"}.get(s.kind)
            if label and not indent:
                lines.append(label)
            line = f"{indent}- {_icu_extent(s)} {_icu_target(s, profile)}".rstrip()
            if s.note:
                line += f"  # {s.note}"
            lines.append(line)

    emit(workout.steps)
    return "\n".join(lines) + "\n"


_ICU_LINE = re.compile(r"^\s*-\s*(?P<extent>\S+)\s*(?P<target>.*?)\s*(?:#\s*(?P<note>.*))?$")
_ICU_REPEAT = re.compile(r"^\s*(\d+)\s*[x\u00d7]\s*$", re.I)


def import_icu(text: str, date: dt.date, name: str | None = None) -> Workout:
    """A best-effort reader for intervals.icu's text syntax."""
    from .oneline import OneLineError, parse_extent, parse_target

    lines = [ln.rstrip() for ln in text.splitlines()]
    title = name
    steps: list[dict] = []
    stack: list[list[dict]] = [steps]
    section = "run"
    for raw in lines:
        if not raw.strip():
            continue
        if m := _ICU_REPEAT.match(raw):
            group = {"kind": "repeat", "reps": int(m.group(1)), "steps": []}
            stack[-1].append(group)
            section = "run"
            stack = [steps, group["steps"]]
            continue
        if not raw.lstrip().startswith("-"):
            word = raw.strip().lower()
            if word in ("warmup", "warm-up", "warm up"):
                section, stack = "warmup", [steps]
            elif word in ("cooldown", "cool-down", "cool down"):
                section, stack = "cooldown", [steps]
            elif word in ("main", "main set", "intervals"):
                section, stack = "run", [steps]
            elif title is None:
                title = raw.strip()
            continue
        m = _ICU_LINE.match(raw)
        if not m:
            raise FormatError(f"cannot read line: {raw!r}")
        if not raw.startswith("  ") and len(stack) > 1:
            stack = [steps]
        try:
            step = parse_extent(m.group("extent"))
        except OneLineError as exc:
            raise FormatError(str(exc)) from exc
        kind = section if section in ("warmup", "cooldown") else "run"
        target_text = (m.group("target") or "").strip()
        if target_text:
            tt = target_text.lower().replace(" hr", "")
            if re.fullmatch(r"z\d", tt):
                step["target"] = (
                    {"type": "hr", "zone": int(tt[1])}
                    if "hr" in target_text.lower()
                    else _zone_from_icu(tt)
                )
            elif tt.endswith("%"):
                step["target"] = _zone_from_percent(float(tt.rstrip("%").split("-")[0]))
            else:
                step["target"] = parse_target(target_text)
        else:
            step["target"] = {"type": "none"}
        if len(stack) > 1 and kind == "run" and step["target"].get("zone") in ("recovery", "easy"):
            kind = "recover"
        step["kind"] = kind
        if m.group("note"):
            step["note"] = m.group("note").strip()
        stack[-1].append(step)
    if not steps:
        raise FormatError("no steps found")
    data = {"name": title or "Imported workout", "date": date.isoformat(), "steps": steps}
    try:
        return Plan.from_dict({"plan": "_", "workouts": [data]}).workouts[0]
    except PlanError as exc:
        raise FormatError(str(exc)) from exc


def _zone_from_icu(z: str) -> dict:
    zone = {
        "z1": "recovery",
        "z2": "easy",
        "z3": "steady",
        "z4": "threshold",
        "z5": "interval",
        "z6": "repetition",
        "z7": "repetition",
    }[z]
    return {"type": "pace", "zone": zone}


def _zone_from_percent(pct: float) -> dict:
    """Percent of threshold pace/power -> the nearest of our zones."""
    if pct < 75:
        return {"type": "pace", "zone": "recovery"}
    if pct < 85:
        return {"type": "pace", "zone": "easy"}
    if pct < 91:
        return {"type": "pace", "zone": "steady"}
    if pct < 96:
        return {"type": "pace", "zone": "marathon"}
    if pct < 103:
        return {"type": "pace", "zone": "threshold"}
    if pct < 112:
        return {"type": "pace", "zone": "interval"}
    return {"type": "pace", "zone": "repetition"}


# --- ZWO --------------------------------------------------------------------


def export_zwo(workout: Workout, profile: Profile) -> str:
    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = "garmin-plan-push"
    ET.SubElement(root, "name").text = workout.name
    ET.SubElement(root, "description").text = workout.notes or ""
    ET.SubElement(root, "sportType").text = "bike" if workout.sport == "cycling" else "run"
    body = ET.SubElement(root, "workout")

    def add(step: Step) -> None:
        tag = {"warmup": "Warmup", "cooldown": "Cooldown"}.get(step.kind, "SteadyState")
        el = ET.SubElement(body, tag)
        el.set("Duration", str(int(round(_seconds(step, profile)) or 60)))
        el.set("Power", f"{_fraction_of_threshold(step, profile):.3f}")
        if step.note:
            ET.SubElement(el, "textevent", timeoffset="0", message=step.note)

    for step in workout.steps:
        if step.is_repeat and len(step.steps) == 2 and not any(s.is_repeat for s in step.steps):
            on, off = step.steps
            el = ET.SubElement(body, "IntervalsT")
            el.set("Repeat", str(step.reps))
            el.set("OnDuration", str(int(round(_seconds(on, profile)) or 60)))
            el.set("OffDuration", str(int(round(_seconds(off, profile)) or 60)))
            el.set("OnPower", f"{_fraction_of_threshold(on, profile):.3f}")
            el.set("OffPower", f"{_fraction_of_threshold(off, profile):.3f}")
        elif step.is_repeat:
            for s in _flatten([step]):
                add(s)
        else:
            add(step)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def import_zwo(text: str, date: dt.date) -> Workout:
    try:
        root = ET.fromstring(text)  # noqa: S314 - a local file the user chose; no entities resolved
    except ET.ParseError as exc:
        raise FormatError(f"not a ZWO file: {exc}") from exc
    name = (root.findtext("name") or "Imported workout").strip()
    notes = (root.findtext("description") or "").strip() or None
    body = root.find("workout")
    if body is None:
        raise FormatError("ZWO has no <workout> element")
    steps: list[dict] = []
    for el in body:
        tag = el.tag
        if tag == "IntervalsT":
            steps.append(
                {
                    "kind": "repeat",
                    "reps": int(el.get("Repeat", "1")),
                    "steps": [
                        {
                            "kind": "run",
                            "duration": f"{int(el.get('OnDuration', '60'))}s",
                            "target": _zone_from_percent(float(el.get("OnPower", "1")) * 100),
                        },
                        {
                            "kind": "recover",
                            "duration": f"{int(el.get('OffDuration', '60'))}s",
                            "target": _zone_from_percent(float(el.get("OffPower", "0.6")) * 100),
                        },
                    ],
                }
            )
            continue
        kind = {"Warmup": "warmup", "Cooldown": "cooldown"}.get(tag, "run")
        power = float(el.get("Power") or el.get("PowerHigh") or el.get("PowerLow") or "0.8")
        steps.append(
            {
                "kind": kind,
                "duration": f"{int(el.get('Duration', '60'))}s",
                "target": _zone_from_percent(power * 100),
            }
        )
    data = {"name": name, "date": date.isoformat(), "steps": steps}
    if notes:
        data["notes"] = notes
    try:
        return Plan.from_dict({"plan": "_", "workouts": [data]}).workouts[0]
    except PlanError as exc:
        raise FormatError(str(exc)) from exc


# --- MRC / ERG --------------------------------------------------------------


def export_mrc(workout: Workout, profile: Profile, erg: bool = False) -> str:
    """Minutes/percent table. ERG writes watts (needs CP); MRC writes percent."""
    if erg and not profile.power_cp:
        raise FormatError("ERG export needs a critical power (profile [power] cp)")
    unit = "WATTS" if erg else "PERCENT"
    lines = [
        "[COURSE HEADER]",
        "VERSION = 2",
        "UNITS = METRIC",
        f"DESCRIPTION = {workout.notes or workout.name}",
        f"FILE NAME = {workout.name}",
        f"MINUTES {unit}",
        "[END COURSE HEADER]",
        "[COURSE DATA]",
    ]
    t = 0.0
    for step in _flatten(workout.steps):
        frac = _fraction_of_threshold(step, profile)
        value = frac * profile.power_cp if erg else frac * 100
        secs = _seconds(step, profile) or 60
        lines.append(f"{t / 60:.2f}\t{value:.0f}")
        t += secs
        lines.append(f"{t / 60:.2f}\t{value:.0f}")
    lines.append("[END COURSE DATA]")
    return "\n".join(lines) + "\n"


# --- share bundles ----------------------------------------------------------


@dataclass
class ShareBundle:
    plan: Plan
    shared_by: str
    exported_at: str
    note: str | None = None


def export_share(plan: Plan, profile: Profile, note: str | None = None) -> str:
    """A self-contained envelope: the plan, and who it came from. No paces --
    zone names travel, and the receiver's profile resolves them."""
    bundle = {
        "gpp_share": SHARE_VERSION,
        "shared_by": profile.name,
        "exported_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "note": note,
        "plan": plan.to_dict(),
    }
    return json.dumps(bundle, indent=2) + "\n"


def import_share(text: str, start_monday: dt.date | None = None) -> ShareBundle:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatError(f"not a share bundle: {exc}") from exc
    if not isinstance(data, dict) or data.get("gpp_share") != SHARE_VERSION:
        raise FormatError("not a gpp share bundle (or a newer version than this tool understands)")
    try:
        plan = Plan.from_dict(data["plan"])
    except (KeyError, PlanError) as exc:
        raise FormatError(f"the shared plan does not validate: {exc}") from exc
    if start_monday:
        from .library import rebase

        plan = rebase(plan, start_monday)
    return ShareBundle(
        plan=plan,
        shared_by=str(data.get("shared_by", "?")),
        exported_at=str(data.get("exported_at", "?")),
        note=data.get("note"),
    )


# --- iCalendar --------------------------------------------------------------


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """RFC 5545 folds lines at 75 octets with a leading space on continuations."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    out, chunk = [], bytearray()
    for byte in data:
        chunk.append(byte)
        if len(chunk) >= 74:
            out.append(chunk.decode("utf-8", "ignore"))
            chunk = bytearray()
    if chunk:
        out.append(chunk.decode("utf-8", "ignore"))
    return "\r\n ".join(out)


def export_ics(plan: Plan, profile: Profile) -> str:
    """The plan as all-day calendar events, one per session (#160)."""
    from .compile import compile_workout
    from .render import render_workout

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gpp//garmin-plan-push//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(plan.plan)}",
    ]
    for workout in plan.sorted_workouts():
        day = workout.date.strftime("%Y%m%d")
        nxt = (workout.date + dt.timedelta(days=1)).strftime("%Y%m%d")
        slug = "".join(c if c.isalnum() else "-" for c in workout.name.lower()).strip("-")
        body = render_workout(compile_workout(workout, profile, plan.plan), profile).rstrip()
        if workout.notes:
            body = f"{workout.notes}\n\n{body}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{day}-{slug}@gpp",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day}",
            f"DTEND;VALUE=DATE:{nxt}",
            f"SUMMARY:{_ics_escape(workout.name)}",
            f"DESCRIPTION:{_ics_escape(body)}",
            f"CATEGORIES:{_ics_escape(workout.role or workout.sport)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


# --- csv and markdown -------------------------------------------------------


def export_csv(plan: Plan, profile: Profile) -> str:
    """One row per session, for a spreadsheet (#180)."""
    import csv
    import io

    from .load import roles_for
    from .timeline import workout_summary

    workouts = plan.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["date", "day", "name", "sport", "role", "phase", "minutes", "km", "hard_minutes", "notes"]
    )
    for w in workouts:
        s = summaries[id(w)]
        writer.writerow(
            [
                w.date.isoformat(),
                w.date.strftime("%a"),
                w.name,
                w.sport,
                roles[id(w)],
                w.phase or "",
                round(s["seconds"] / 60),
                round(s["metres"] / 1000, 1),
                round(s["hard_seconds"] / 60),
                w.notes or "",
            ]
        )
    return out.getvalue()


def export_markdown(plan: Plan, profile: Profile) -> str:
    """The plan as a document, week by week, for Notion, Obsidian or email (#181)."""
    from .compile import compile_workout
    from .load import week_start, weekly_stats
    from .render import render_workout

    lines = [f"# {plan.plan}", ""]
    if plan.summary:
        lines += [plan.summary, ""]
    race = plan.a_race
    if race:
        lines += [f"**Race:** {race.name} on {race.date.isoformat()}", ""]
    weeks = {w.start: w for w in weekly_stats(plan, profile)}
    current = None
    for workout in plan.sorted_workouts():
        monday = week_start(workout.date)
        if monday != current:
            current = monday
            stats = weeks.get(monday)
            head = f"## Week of {monday.isoformat()}"
            if stats:
                head += (
                    f" — {stats.km:.0f} km, {stats.sessions} sessions, {stats.hard_share:.0%} hard"
                )
            lines += [head, ""]
        lines.append(f"### {workout.date.strftime('%a %d %b')} — {workout.name}")
        if workout.notes:
            lines.append(f"*{workout.notes}*")
        lines.append("")
        lines.append("```")
        lines.append(render_workout(compile_workout(workout, profile, plan.plan), profile).rstrip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# --- dispatch ---------------------------------------------------------------

EXPORTERS = {"icu": export_icu, "zwo": export_zwo, "mrc": export_mrc}


def export_workout(workout: Workout, profile: Profile, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "erg":
        return export_mrc(workout, profile, erg=True)
    if fmt not in EXPORTERS:
        raise FormatError(f"unknown format {fmt!r}; use icu, zwo, mrc, erg or share")
    return EXPORTERS[fmt](workout, profile)


def import_workout(text: str, date: dt.date, fmt: str | None = None) -> Workout:
    fmt = (fmt or "").lower()
    stripped = text.lstrip()
    if fmt == "zwo" or (not fmt and stripped.startswith("<")):
        return import_zwo(text, date)
    return import_icu(text, date)


def copy_with_dates(plan: Plan) -> Plan:
    return deepcopy(plan)
