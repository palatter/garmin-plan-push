"""Push receipts: what went to Garmin, when, with which ids (#148).

Every push writes one JSON file, so `gpp pushes` can list them and `gpp
unpush --receipt` can remove exactly those workouts by id -- even after the
plan file changed, the names changed, or the tag would no longer match.
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
        out = []
        for r in self.results:
            if r.get("action") in REMOVABLE and r.get("workout_id"):
                out.append(int(r["workout_id"]))
        return sorted(set(out))

    def to_dict(self) -> dict:
        return {"when": self.when, "plan": self.plan, "results": self.results}

    def describe(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
        summary = ", ".join(f"{n} {action}" for action, n in sorted(counts.items()))
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
