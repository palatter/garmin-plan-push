"""What changed between two versions of a plan.

Keyed by date + name, because that is how the push layer identifies
workouts too. A workout that moved date shows as removed + added, which is
what actually happens on the Garmin calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .plan import Plan, Step


@dataclass
class WorkoutChange:
    key: str
    kind: str  # added | removed | changed
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "details": self.details}


@dataclass
class PlanDiff:
    changes: list[WorkoutChange] = field(default_factory=list)
    meta: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes and not self.meta

    def text(self) -> str:
        if self.empty:
            return "No differences."
        lines = list(self.meta)
        for c in self.changes:
            mark = {"added": "+", "removed": "-", "changed": "~"}[c.kind]
            lines.append(f"{mark} {c.key}")
            lines.extend(f"    {d}" for d in c.details)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"meta": self.meta, "changes": [c.to_dict() for c in self.changes]}


def _step_lines(steps: list[Step], indent: int = 0) -> list[str]:
    out = []
    pad = "  " * indent
    for s in steps:
        if s.is_repeat:
            out.append(f"{pad}{s.reps}x")
            out.extend(_step_lines(s.steps, indent + 1))
            continue
        extent = s.duration or s.distance or (f"{s.count} reps" if s.count else "lap")
        t = s.target.to_dict()
        target = t.get("zone") or (
            f"{t.get('slow')}-{t.get('fast')}" if t.get("slow") else t.get("type", "none")
        )
        if t.get("type") in ("hr", "power") and t.get("low"):
            target = f"{t['low']}-{t['high']}"
        line = f"{pad}{s.kind} {extent} @ {target}"
        if s.exercise:
            line = f"{pad}{s.exercise} x{s.count or s.duration}"
        out.append(line)
    return out


def diff_plans(before: Plan, after: Plan) -> PlanDiff:
    result = PlanDiff()
    if before.plan != after.plan:
        result.meta.append(f"name: {before.plan!r} -> {after.plan!r}")
    if before.race_date != after.race_date:
        result.meta.append(f"race_date: {before.race_date} -> {after.race_date}")
    b_races = {(r.date.isoformat(), r.name): r.priority for r in before.races}
    a_races = {(r.date.isoformat(), r.name): r.priority for r in after.races}
    for key in sorted(set(b_races) | set(a_races)):
        if key not in a_races:
            result.meta.append(f"race removed: {key[0]} {key[1]}")
        elif key not in b_races:
            result.meta.append(f"race added: {key[0]} {key[1]} ({a_races[key]})")
        elif a_races[key] != b_races[key]:
            result.meta.append(f"race {key[1]}: priority {b_races[key]} -> {a_races[key]}")

    b = {f"{w.date.isoformat()} {w.name}": w for w in before.workouts}
    a = {f"{w.date.isoformat()} {w.name}": w for w in after.workouts}
    for key in sorted(set(b) | set(a)):
        if key not in a:
            result.changes.append(WorkoutChange(key, "removed"))
        elif key not in b:
            result.changes.append(WorkoutChange(key, "added", _step_lines(a[key].steps)))
        else:
            details = []
            wb, wa = b[key], a[key]
            for attr in ("role", "phase", "sport", "notes"):
                if getattr(wb, attr) != getattr(wa, attr):
                    details.append(f"{attr}: {getattr(wb, attr)!r} -> {getattr(wa, attr)!r}")
            lb, la = _step_lines(wb.steps), _step_lines(wa.steps)
            if lb != la:
                details.append("steps:")
                import difflib

                for line in difflib.unified_diff(lb, la, lineterm="", n=0):
                    if line.startswith(("---", "+++", "@@")):
                        continue
                    details.append(f"  {line}")
            if details:
                result.changes.append(WorkoutChange(key, "changed", details))
    return result
