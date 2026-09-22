"""Recent plans (#165, #172): the plan you were just looking at, kept.

The web app used to lose the plan on a refresh, and the CLI had no notion of
"the plan I am working on". Every plan the app opens or edits, and every
plan a command writes, is saved here by content hash; `gpp plans` and the
compose screen list them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

RECENT_DIR = Path.home() / ".config" / "gpp" / "recent"
KEEP = 20


@dataclass
class RecentPlan:
    path: Path
    name: str
    label: str
    saved: str
    sessions: int
    first: str | None
    last: str | None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "label": self.label,
            "saved": self.saved,
            "sessions": self.sessions,
            "first": self.first,
            "last": self.last,
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "plan"


def save_recent(plan: dict, label: str = "edited", root: Path | None = None) -> Path:
    """Keep this plan; an identical one (by content) refreshes its timestamp instead."""
    root = root or RECENT_DIR
    root.mkdir(parents=True, exist_ok=True)
    body = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
    now = dt.datetime.now(dt.UTC)
    for existing in root.glob(f"*-{digest}.json"):
        existing.touch()
        return existing
    path = (
        root
        / f"{now.strftime('%Y%m%dT%H%M%S')}-{_slug(str(plan.get('plan', 'plan')))}-{digest}.json"
    )
    envelope = {"label": label, "saved": now.isoformat(timespec="seconds"), "plan": plan}
    path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    _prune(root)
    return path


def _prune(root: Path) -> None:
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[KEEP:]:
        stale.unlink(missing_ok=True)


def load_recent(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (
        data["plan"]
        if isinstance(data, dict) and "plan" in data and "workouts" in data["plan"]
        else data
    )


def list_recent(root: Path | None = None) -> list[RecentPlan]:
    root = root or RECENT_DIR
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plan = data.get("plan", {})
            dates = sorted(w.get("date", "") for w in plan.get("workouts", []) if w.get("date"))
            out.append(
                RecentPlan(
                    path=path,
                    name=str(plan.get("plan", path.stem)),
                    label=str(data.get("label", "")),
                    saved=str(data.get("saved", "")),
                    sessions=len(plan.get("workouts", [])),
                    first=dates[0] if dates else None,
                    last=dates[-1] if dates else None,
                )
            )
        except (OSError, ValueError, AttributeError):
            continue
    return out
