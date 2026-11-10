"""Push receipts: what went to Garmin, when, with which ids (#148).

Every push writes one JSON file, so `gpp pushes` can list them and `gpp
unpush --receipt` can remove exactly those workouts by id -- even after the
plan file changed, the names changed, or the tag would no longer match.

A receipt is written for every push that sent anything, also one that
stopped half-way: it lists what went through, what failed, and what was
never reached. The next push of the same plan reads them back
(`earlier_pushes`): a workout Garmin created but never put on the calendar
is scheduled instead of being created again, and a calendar entry whose
tag cannot be read is still known as this plan's by its id.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RECEIPTS_DIR = Path.home() / ".config" / "gpp" / "pushes"
REMOVABLE = ("created", "replaced", "updated", "unchanged")


@dataclass
class Receipt:
    when: str
    plan: str
    results: list[dict] = field(default_factory=list)
    path: Path | None = None

    @property
    def ids(self) -> list[int]:
        """Every workout this push left on Garmin: the ones it scheduled, and
        the ones it created but could not put on the calendar."""
        out = []
        for r in self.results:
            if (r.get("action") in REMOVABLE or r.get("unscheduled")) and r.get("workout_id"):
                out.append(int(r["workout_id"]))
        return sorted(set(out))

    def to_dict(self) -> dict:
        return {"when": self.when, "plan": self.plan, "results": self.results}

    def describe(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
        summary = ", ".join(f"{n} {action}" for action, n in sorted(counts.items()))
        unsure = sum(1 for r in self.results if r.get("duplicates_unchecked"))
        if unsure:
            summary += f"; duplicates not ruled out for {unsure}"
        dates = sorted(r.get("date", "") for r in self.results if r.get("date"))
        span = f"{dates[0]} to {dates[-1]}" if dates else "no dates"
        return f"{self.when[:16].replace('T', ' ')}  {self.plan}  ({span}; {summary})"


def save_receipt(
    plan_name: str, results: list, root: Path | None = None, now: dt.datetime | None = None
) -> Path:
    root = root or RECEIPTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    now = now or dt.datetime.now(dt.UTC)
    slug = re.sub(r"[^a-z0-9]+", "-", plan_name.lower()).strip("-") or "plan"
    path = root / f"{now.strftime('%Y%m%dT%H%M%S')}-{slug}.json"
    rows = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in results]
    receipt = Receipt(
        when=now.isoformat(timespec="seconds"), plan=plan_name, results=rows, path=path
    )
    path.write_text(json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load_receipt(path: str | Path) -> Receipt:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Receipt(
        when=str(data.get("when", "")),
        plan=str(data.get("plan", "?")),
        results=list(data.get("results", [])),
        path=path,
    )


def list_receipts(root: Path | None = None) -> list[Receipt]:
    root = root or RECEIPTS_DIR
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            out.append(load_receipt(path))
        except (OSError, ValueError):
            continue
    return out


@dataclass
class Earlier:
    """What earlier pushes of one plan left on Garmin, from their receipts."""

    # Workouts gpp scheduled for this plan and has not removed since.
    ids: set[int] = field(default_factory=set)
    # (date, session name) -> a workout that was created but never scheduled.
    unscheduled: dict[tuple[str, str], int] = field(default_factory=dict)


def _row_id(row: dict) -> int | None:
    try:
        return int(row["workout_id"]) if row.get("workout_id") else None
    except (TypeError, ValueError):
        return None


def earlier_pushes(plan_name: str, root: Path | None = None) -> Earlier:
    """Read this plan's receipts, oldest first, into what is still out there.

    Receipts are a second witness, never the first: the push only uses them
    for workouts that are on the calendar anyway (ids) or that it then reads
    back from Garmin and finds tagged as this plan's (unscheduled).
    """
    earlier = Earlier()
    for receipt in reversed(list_receipts(root)):
        if receipt.plan != plan_name:
            continue
        for row in receipt.results:
            if not isinstance(row, dict):
                continue
            workout_id = _row_id(row)
            if workout_id is None:
                continue
            where = (str(row.get("date", "")), str(row.get("name", "")).strip())
            waiting = [k for k, v in earlier.unscheduled.items() if v == workout_id]
            if row.get("unscheduled"):
                earlier.unscheduled[where] = workout_id
            elif row.get("action") in REMOVABLE:
                earlier.ids.add(workout_id)
                for key in (*waiting, where):  # it reached the calendar after all
                    earlier.unscheduled.pop(key, None)
            elif row.get("action") == "removed":
                earlier.ids.discard(workout_id)
                for key in waiting:
                    earlier.unscheduled.pop(key, None)
    return earlier
