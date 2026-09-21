"""A local library of workouts and plan templates.

Final Surge and TrainingPeaks both treat a plan as a first-class object,
separate from the calendar, that you apply to a date. Here that is a JSON
file under ~/.config/gpp/library/, and applying it is a date shift.

Workout templates are single workouts without a date. Plan templates are
whole plans whose dates are relative: applying one re-bases every date so
the first workout lands on the Monday you choose, and re-anchors the race.

A handful of seeds ship with the tool -- the sessions coaches reach for
most, including the two the roadmap called out: the threshold-embedded long
run and the return-to-run ramps.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from copy import deepcopy
from pathlib import Path

from .adapt import scale_workout
from .plan import Plan, PlanError, Workout

LIBRARY_DIR = Path.home() / ".config" / "gpp" / "library"


class LibraryError(ValueError):
    """Bad name, missing item, or an item that does not validate."""


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise LibraryError("a library item needs a name")
    return slug[:60]


def _dir(kind: str, root: Path | None = None) -> Path:
    if kind not in ("workouts", "plans"):
        raise LibraryError("kind must be 'workouts' or 'plans'")
    path = (root or LIBRARY_DIR) / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- workouts ---------------------------------------------------------------


def save_workout(
    workout: Workout | dict, name: str | None = None, root: Path | None = None
) -> Path:
    data = workout.to_dict() if isinstance(workout, Workout) else dict(workout)
    data.pop("date", None)
    title = name or data.get("name") or "workout"
    data["name"] = title
    # Validate by giving it a throwaway date.
    Plan.from_dict({"plan": "_", "workouts": [dict(data, date="2000-01-03")]})
    path = _dir("workouts", root) / f"{_slug(title)}.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load_workout(name: str, date: dt.date, root: Path | None = None) -> Workout:
    path = _dir("workouts", root) / f"{_slug(name)}.json"
    if not path.exists():
        seed = SEED_WORKOUTS.get(_slug(name))
        if seed is None:
            raise LibraryError(f"no workout named {name!r} in the library")
        data = deepcopy(seed)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    data["date"] = date.isoformat()
    return Plan.from_dict({"plan": "_", "workouts": [data]}).workouts[0]


def list_workouts(root: Path | None = None) -> list[dict]:
    out = []
    seen = set()
    for path in sorted(_dir("workouts", root).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"slug": path.stem, "name": data.get("name", path.stem), "source": "mine"})
        seen.add(path.stem)
    for slug, seed in SEED_WORKOUTS.items():
        if slug not in seen:
            meta = SEED_META.get(slug, {})
            out.append(
                {
                    "slug": slug,
                    "name": seed["name"],
                    "source": "built-in",
                    "race": meta.get("race", "any"),
                    "origin": meta.get("source", ""),
                }
            )
    return out


# --- plan templates ---------------------------------------------------------


def save_plan(plan: Plan, name: str | None = None, root: Path | None = None) -> Path:
    data = plan.to_dict()
    title = name or plan.plan
    data["plan"] = title
    path = _dir("plans", root) / f"{_slug(title)}.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def list_plans(root: Path | None = None) -> list[dict]:
    out = []
    for path in sorted(_dir("plans", root).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "slug": path.stem,
                "name": data.get("plan", path.stem),
                "workouts": len(data.get("workouts", [])),
                "source": "mine",
            }
        )
    return out


def apply_plan(
    name_or_path: str,
    start_monday: dt.date,
    race_date: dt.date | None = None,
    root: Path | None = None,
) -> Plan:
    """Re-base a template so its first week begins on `start_monday`.

    With `race_date`, the template's A race is moved there instead and the
    workouts shift by the same amount -- "this 12-week plan, ending on my
    race" -- which is how people actually think about it.
    """
    candidate = Path(name_or_path)
    path = candidate if candidate.exists() else _dir("plans", root) / f"{_slug(name_or_path)}.json"
    if not path.exists():
        raise LibraryError(f"no plan template named {name_or_path!r}")
    try:
        template = Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (PlanError, json.JSONDecodeError) as exc:
        raise LibraryError(f"{path} is not a valid plan template: {exc}") from exc
    return rebase(template, start_monday, race_date)


def rebase(template: Plan, start_monday: dt.date, race_date: dt.date | None = None) -> Plan:
    workouts = template.sorted_workouts()
    if not workouts:
        raise LibraryError("template has no workouts")
    first = workouts[0].date
    first_monday = first - dt.timedelta(days=first.weekday())
    if race_date and template.a_race:
        delta = race_date - template.a_race.date
    else:
        delta = start_monday - first_monday
    plan = deepcopy(template)
    for w in plan.workouts:
        w.date += delta
    for r in plan.races:
        r.date += delta
    for wk in plan.weeks:
        wk.start += delta
    if plan.race_date:
        plan.race_date += delta
    return plan


# --- return-to-run ramps ----------------------------------------------------


def return_to_run(start: dt.date, tier: str, base: Workout | None = None) -> Plan:
    """Pfitzinger's post-race recovery and a post-injury ramp, from a tier.

    resume: nothing to do. restart-phase: two easy weeks at 80%. back-to-base:
    three weeks easy at 65% climbing. foundation: four weeks, walk-run first.
    """
    easy = base or Workout.from_dict(
        {
            "name": "Easy",
            "date": start.isoformat(),
            "role": "easy",
            "phase": "recovery",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}}
            ],
        }
    )
    schedule = {
        "resume": [],
        "restart-phase": [0.8, 0.9],
        "back-to-base": [0.65, 0.75, 0.9],
        "foundation": [0.5, 0.6, 0.75, 0.9],
    }.get(tier)
    if schedule is None:
        raise LibraryError(f"unknown tier {tier!r}")
    workouts: list[Workout] = []
    for week_index, factor in enumerate(schedule):
        monday = start - dt.timedelta(days=start.weekday()) + dt.timedelta(days=7 * week_index)
        for offset in (0, 2, 4):
            w = scale_workout(easy, factor)
            w.date = monday + dt.timedelta(days=offset)
            w.name = f"Easy {int(factor * 100)}%"
            w.role, w.phase = "easy", "recovery"
            workouts.append(w)
    return Plan(plan=f"Return to running ({tier})", workouts=workouts)


# --- post-race recovery ----------------------------------------------------


def post_race_recovery(race_date: dt.date, distance: str = "marathon") -> Plan:
    """Pfitzinger's five weeks after a marathon (three after a half or shorter).

    Week 1: two short easy runs late in the week. Week 2: three easy runs.
    Week 3: four easy runs with a longer one. Week 4: strides return. Week 5:
    one light quality session. Every session is easy-zone running.
    """
    long = "marathon" in distance.lower() and "half" not in distance.lower()
    weeks = (
        [
            ((3, 25), (5, 30)),
            ((1, 35), (3, 40), (5, 40)),
            ((1, 40), (3, 45), (5, 40), (6, 60)),
            ((1, 45), (3, 40), (5, 45), (6, 70)),
            ((1, 45), (3, 40), (5, 45), (6, 80)),
        ]
        if long
        else [((3, 25), (5, 30)), ((1, 35), (3, 40), (5, 45)), ((1, 40), (3, 40), (5, 45), (6, 60))]
    )
    monday = race_date - dt.timedelta(days=race_date.weekday()) + dt.timedelta(days=7)
    workouts = []
    for index, sessions in enumerate(weeks):
        for offset, minutes in sessions:
            day = monday + dt.timedelta(days=7 * index + offset)
            steps = [
                {
                    "kind": "run",
                    "duration": f"{minutes}m",
                    "target": {"type": "pace", "zone": "easy"},
                }
            ]
            name = "Easy"
            notes = "Recovery: easy means easy; the race is still in your legs."
            if index == 3:
                steps.append(
                    {
                        "kind": "repeat",
                        "reps": 4,
                        "steps": [
                            {"kind": "stride", "duration": "20s", "target": {"type": "none"}},
                            {
                                "kind": "recover",
                                "duration": "60s",
                                "target": {"type": "pace", "zone": "recovery"},
                            },
                        ],
                    }
                )
                name = "Easy + strides"
            if index == len(weeks) - 1 and offset == 3:
                steps = [
                    {
                        "kind": "warmup",
                        "duration": "15m",
                        "target": {"type": "pace", "zone": "easy"},
                    },
                    {
                        "kind": "run",
                        "duration": "15m",
                        "target": {"type": "pace", "zone": "steady"},
                    },
                    {
                        "kind": "cooldown",
                        "duration": "10m",
                        "target": {"type": "pace", "zone": "easy"},
                    },
                ]
                name, notes = "Light steady", "First touch of pace since the race; controlled."
            workouts.append(
                {
                    "name": name,
                    "date": day.isoformat(),
                    "role": "easy" if index < len(weeks) - 1 or offset != 3 else "quality",
                    "phase": "recovery",
                    "notes": notes,
                    "steps": steps,
                }
            )
    return Plan.from_dict({"plan": f"Recovery after {distance}", "workouts": workouts})


# --- seeds ------------------------------------------------------------------

# Where each seed comes from, and which race it serves (#127).
SEED_META: dict[str, dict[str, str]] = {
    "threshold-5x1k": {
        "race": "10k",
        "source": "Daniels, Running Formula: T-pace cruise intervals",
    },
    "long-run-with-threshold": {
        "race": "half",
        "source": "Pfitzinger, Advanced Marathoning: LT long run",
    },
    "medium-long": {"race": "marathon", "source": "Pfitzinger: midweek medium-long run"},
    "easy-with-strides": {"race": "any", "source": "Daniels: strides after easy runs"},
    "hill-repeats": {"race": "5k", "source": "Lydiard-style hill sessions"},
    "marathon-pace-finish": {"race": "marathon", "source": "Pfitzinger: long run with MP finish"},
    "runner-strength": {
        "race": "any",
        "source": "Llanos-Lagos 2024 meta-analysis: heavy + plyometric",
    },
    "5k-12x400": {"race": "5k", "source": "Daniels: R-pace 400s, full recovery"},
    "10k-6x800": {"race": "10k", "source": "Daniels: I-pace 800s, equal jog"},
    "10k-tempo-20": {"race": "10k", "source": "Daniels: 20-minute T-pace tempo"},
    "half-2x3k": {"race": "half", "source": "Daniels: long T-pace intervals"},
    "half-hmp-finish": {"race": "half", "source": "Pfitzinger: long run with race-pace finish"},
    "marathon-mp-long": {
        "race": "marathon",
        "source": "Pfitzinger / Canova: MP within the long run",
    },
}

SEED_WORKOUTS: dict[str, dict] = {
    "5k-12x400": {
        "name": "12 x 400m",
        "role": "quality",
        "notes": "Repetition pace, relaxed and quick; walk or jog the recovery fully.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 12,
                "steps": [
                    {
                        "kind": "run",
                        "distance": "400m",
                        "target": {"type": "pace", "zone": "repetition"},
                    },
                    {
                        "kind": "recover",
                        "duration": "90s",
                        "target": {"type": "pace", "zone": "recovery"},
                    },
                ],
            },
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "10k-6x800": {
        "name": "6 x 800m",
        "role": "quality",
        "notes": "Interval pace; the jog is as long as the rep.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 6,
                "steps": [
                    {
                        "kind": "run",
                        "distance": "800m",
                        "target": {"type": "pace", "zone": "interval"},
                    },
                    {
                        "kind": "recover",
                        "duration": "3m",
                        "target": {"type": "pace", "zone": "recovery"},
                    },
                ],
            },
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "10k-tempo-20": {
        "name": "Tempo 20",
        "role": "quality",
        "notes": "Twenty continuous minutes at threshold; comfortably hard, not a race.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "threshold"}},
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "half-2x3k": {
        "name": "2 x 3km",
        "role": "quality",
        "notes": "Long threshold intervals close to half-marathon pace.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 2,
                "steps": [
                    {
                        "kind": "run",
                        "distance": "3km",
                        "target": {"type": "pace", "zone": "threshold"},
                    },
                    {
                        "kind": "recover",
                        "duration": "3m",
                        "target": {"type": "pace", "zone": "recovery"},
                    },
                ],
            },
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "half-hmp-finish": {
        "name": "Long run, HMP finish",
        "role": "long",
        "notes": "Easy for most of it, then race pace on tired legs.",
        "steps": [
            {"kind": "run", "duration": "70m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "threshold"}},
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "marathon-mp-long": {
        "name": "MP long run",
        "role": "long",
        "notes": "The marathon rehearsal: fuel as on race day, hold marathon pace in the middle.",
        "steps": [
            {"kind": "run", "distance": "8km", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "distance": "12km", "target": {"type": "pace", "zone": "marathon"}},
            {"kind": "run", "distance": "3km", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "threshold-5x1k": {
        "name": "Threshold 5x1k",
        "role": "quality",
        "notes": "Comfortably hard; the last rep should feel like the first.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 5,
                "steps": [
                    {
                        "kind": "run",
                        "distance": "1km",
                        "target": {"type": "pace", "zone": "threshold"},
                    },
                    {
                        "kind": "recover",
                        "duration": "90s",
                        "target": {"type": "pace", "zone": "recovery"},
                    },
                ],
            },
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "long-run-with-threshold": {
        "name": "Long run with threshold",
        "role": "long",
        "notes": "Pfitzinger's lactate-threshold long run: easy, then a sustained block at 15k-half pace.",
        "steps": [
            {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "25m", "target": {"type": "pace", "zone": "threshold"}},
            {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "medium-long": {
        "name": "Medium-long run",
        "role": "medium-long",
        "notes": "Midweek aerobic volume, 90 minutes, push the pace a little in the back half.",
        "steps": [
            {"kind": "run", "duration": "50m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "steady"}},
        ],
    },
    "easy-with-strides": {
        "name": "Easy + strides",
        "role": "easy",
        "notes": "Aerobic maintenance with a little leg speed at the end.",
        "steps": [
            {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 6,
                "steps": [
                    {"kind": "stride", "duration": "20s", "target": {"type": "none"}},
                    {
                        "kind": "recover",
                        "duration": "60s",
                        "target": {"type": "pace", "zone": "recovery"},
                    },
                ],
            },
        ],
    },
    "hill-repeats": {
        "name": "Hill repeats",
        "role": "quality",
        "notes": "Strength and form: strong uphill, tall posture, walk or jog down.",
        "steps": [
            {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
            {
                "kind": "repeat",
                "reps": 8,
                "steps": [
                    {
                        "kind": "run",
                        "duration": "45s",
                        "grade": 6,
                        "target": {"type": "rpe", "value": 8},
                    },
                    {"kind": "recover", "until": "lap", "target": {"type": "none"}},
                ],
            },
            {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}},
        ],
    },
    "marathon-pace-finish": {
        "name": "Long run with MP finish",
        "role": "long",
        "notes": "Steady through, then the last 20 minutes at marathon effort.",
        "steps": [
            {"kind": "run", "duration": "70m", "target": {"type": "pace", "zone": "easy"}},
            {"kind": "run", "duration": "20m", "target": {"type": "pace", "zone": "marathon"}},
            {"kind": "cooldown", "duration": "5m", "target": {"type": "pace", "zone": "recovery"}},
        ],
    },
    "runner-strength": {
        "name": "Runner's strength",
        "sport": "strength",
        "role": "strength",
        "notes": "Twenty minutes of the lifts that keep runners healthy.",
        "steps": [
            {
                "kind": "repeat",
                "reps": 3,
                "steps": [
                    {
                        "kind": "exercise",
                        "exercise": "goblet squat",
                        "category": "squat",
                        "count": 10,
                    },
                    {
                        "kind": "exercise",
                        "exercise": "single leg deadlift",
                        "category": "deadlift",
                        "count": 8,
                    },
                    {
                        "kind": "exercise",
                        "exercise": "calf raise",
                        "category": "calf raise",
                        "count": 15,
                    },
                    {
                        "kind": "exercise",
                        "exercise": "plank",
                        "category": "plank",
                        "duration": "45s",
                    },
                    {"kind": "rest", "duration": "60s"},
                ],
            },
        ],
    },
}
