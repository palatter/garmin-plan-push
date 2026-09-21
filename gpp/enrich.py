"""Deterministic additions a coach makes after the model has written the plan.

Each function takes a plan and returns a new one plus the reasons for every
change, in the same shape as adapt.py. None of it is a model's judgement:
these are the parts of the literature that translate directly into a note on
a session, so they should not depend on whether the model remembered them.

  * fuelling practice on long runs -- 30 g/h building to 90 g/h over six to
    eight weeks, 2:1 glucose:fructose, first intake inside 30 minutes
    (Jeukendrup; runnersconnect.net/marathon-gut-training);
  * a heat block before a hot race -- five or six post-run passive heat
    exposures in the final two weeks, as effective as exercising in the heat
    (Zurawlew 2021; GSSI SSE #153);
  * strength placed by rule -- two sessions a week on easy days, never the
    day before a key session, at least 48 h apart (Llanos-Lagos 2024);
  * durability work in marathon peaks -- fast-finish long runs, hills,
    fuelling practice (Jones 2025, "physiological resilience");
  * a cadence cue for runners with an overuse-injury history -- a 5-10% lift
    lowers loading rates (2025 systematic review).
"""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from dataclasses import dataclass, field

from .load import roles_for, week_start
from .plan import Plan, Workout
from .profile import Profile
from .timeline import workout_summary

FUEL_STEPS_G_PER_H = (30, 45, 60, 75, 90)
LONG_RUN_MINUTES = 90
HEAT_DAYS = 14
HEAT_SESSIONS = 6
STRENGTH_TEMPLATE = "runner-strength"
OVERUSE_WORDS = (
    "shin",
    "knee",
    "achilles",
    "itb",
    "it band",
    "stress fracture",
    "plantar",
    "tibial",
    "patell",
    "hamstring",
)
CADENCE_CUE = "Cadence: 5-10% quicker steps than usual, shorter stride, land under you"


@dataclass
class Enrichment:
    plan: Plan
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"plan": self.plan.to_dict(), "reasons": self.reasons}


def _note(workout: Workout, text: str) -> None:
    workout.notes = f"{workout.notes.rstrip('. ')}. {text}" if workout.notes else text


def _long_runs(plan: Plan, profile: Profile) -> list[Workout]:
    workouts = plan.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    return [
        w
        for w in workouts
        if w.sport == "running"
        and (roles[id(w)] == "long" or summaries[id(w)]["seconds"] >= LONG_RUN_MINUTES * 60)
    ]


def add_fuelling_cues(plan: Plan, profile: Profile) -> Enrichment:
    """Progressive carbohydrate practice on every long run that lacks it."""
    new = deepcopy(plan)
    reasons: list[str] = []
    step = 0
    for w in _long_runs(new, profile):
        if w.notes and "fuel" in w.notes.lower():
            step += 1
            continue
        grams = FUEL_STEPS_G_PER_H[min(step, len(FUEL_STEPS_G_PER_H) - 1)]
        _note(
            w,
            f"Fuel: practise race-day fuelling, about {grams} g/h carbohydrate "
            "(2:1 glucose:fructose), first intake inside 30 min, 400-800 ml/h to thirst",
        )
        reasons.append(f"{w.date.isoformat()} {w.name}: fuelling cue at {grams} g/h.")
        step += 1
    if not reasons:
        reasons.append("No long run without a fuelling cue.")
    return Enrichment(new, reasons)


def add_heat_block(plan: Plan, race_date: dt.date, profile: Profile) -> Enrichment:
    """Passive post-run heat on up to six easy sessions in the last two weeks."""
    new = deepcopy(plan)
    workouts = new.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    window = [
        w
        for w in workouts
        if 1 <= (race_date - w.date).days <= HEAT_DAYS
        and w.sport == "running"
        and roles[id(w)] not in ("quality", "race")
    ]
    if not window:
        return Enrichment(
            new, ["No easy sessions in the two weeks before the race to attach heat exposures to."]
        )
    # Spread the exposures across the window rather than bunching them.
    stride = max(1, len(window) // HEAT_SESSIONS)
    chosen = window[::stride][:HEAT_SESSIONS]
    for w in chosen:
        _note(
            w,
            "Heat: 30-45 min hot bath or sauna straight after this run (passive heat "
            "acclimation; 5-6 exposures in the final two weeks are as effective as "
            "running in the heat)",
        )
    reasons = [f"{w.date.isoformat()} {w.name}: post-run heat exposure." for w in chosen]
    reasons.append(
        "Race-week checklist: hydrate to thirst plus 300-600 mg/h sodium, pre-cool, "
        "start conservatively; expect paces 2-4% slower per 10 degrees of dew point above 15."
    )
    return Enrichment(new, reasons)


def place_strength(plan: Plan, profile: Profile, per_week: int = 2) -> Enrichment:
    """Two strength sessions a week on easy days, never before a key session."""
    from .library import load_workout

    new = deepcopy(plan)
    workouts = new.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    by_date: dict[dt.date, list[Workout]] = {}
    for w in workouts:
        by_date.setdefault(w.date, []).append(w)
    race = new.a_race
    weeks: dict[dt.date, list[Workout]] = {}
    for w in workouts:
        weeks.setdefault(week_start(w.date), []).append(w)

    def hard(day: dt.date) -> bool:
        return any(
            roles[id(o)] in ("quality", "race", "long", "medium-long") for o in by_date.get(day, [])
        )

    added: list[Workout] = []
    reasons: list[str] = []
    for monday, sessions in sorted(weeks.items()):
        if any(w.sport == "strength" for w in sessions):
            continue
        if any(w.phase == "taper" for w in sessions) or (
            race and 0 <= (race.date - monday).days < 14
        ):
            reasons.append(f"week of {monday.isoformat()}: taper or race week, no strength added.")
            continue
        picked: list[dt.date] = []
        for offset in range(7):
            day = monday + dt.timedelta(days=offset)
            if hard(day) or hard(day + dt.timedelta(days=1)):
                continue
            if picked and (day - picked[-1]).days < 2:
                continue
            picked.append(day)
            if len(picked) == per_week:
                break
        for day in picked:
            w = load_workout(STRENGTH_TEMPLATE, day)
            w.role, w.sport = "strength", "strength"
            added.append(w)
            reasons.append(
                f"{day.isoformat()}: strength session added (easy day, 48 h from the last)."
            )
    new.workouts = sorted([*new.workouts, *added], key=lambda w: (w.date, w.name))
    if not added and not reasons:
        reasons.append("Every week already has strength, or no suitable day.")
    return Enrichment(new, reasons)


def add_durability_notes(plan: Plan, profile: Profile) -> Enrichment:
    """Fast-finish and hill notes on alternate long runs in a marathon build."""
    from .checks import _goal_distance, _is_marathon

    new = deepcopy(plan)
    if not _is_marathon(_goal_distance(new, profile)):
        return Enrichment(
            new, ["Not a marathon block; durability notes are for marathon long runs."]
        )
    race = new.a_race
    longs = [
        w
        for w in _long_runs(new, profile)
        if w.phase != "taper" and (not race or (race.date - w.date).days > 14)
    ]
    reasons: list[str] = []
    for index, w in enumerate(longs):
        if index % 2 == 1 and not (w.notes and "finish" in w.notes.lower()):
            _note(
                w,
                "Durability: run the final 20-30 min at marathon pace on tired legs; "
                "choose rolling hills if you can",
            )
            reasons.append(f"{w.date.isoformat()} {w.name}: fast-finish durability note.")
    if not reasons:
        reasons.append("No long run to add a durability note to.")
    return Enrichment(new, reasons)


def add_cadence_cues(plan: Plan, profile: Profile) -> Enrichment:
    """A cadence cue on the first easy run of each week, for overuse histories."""
    new = deepcopy(plan)
    history = " ".join(profile.injuries).lower()
    if not any(word in history for word in OVERUSE_WORDS):
        return Enrichment(new, ["No overuse injury in the history; no cadence cue."])
    workouts = new.sorted_workouts()
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    roles = roles_for(workouts, summaries)
    done: set[dt.date] = set()
    reasons: list[str] = []
    for w in workouts:
        monday = week_start(w.date)
        if monday in done or roles[id(w)] not in ("easy", "recovery") or not w.steps:
            continue
        first = w.steps[0]
        target = first.steps[0] if first.is_repeat and first.steps else first
        if not target.note:
            target.note = CADENCE_CUE
            done.add(monday)
            reasons.append(f"{w.date.isoformat()} {w.name}: cadence cue on the first step.")
    if not reasons:
        reasons.append("No easy run without a step note to carry the cadence cue.")
    return Enrichment(new, reasons)


def enrich(
    plan: Plan,
    profile: Profile,
    fuelling: bool = True,
    heat_race: dt.date | None = None,
    strength_per_week: int = 0,
    durability: bool = True,
    cadence: bool = True,
) -> Enrichment:
    """Apply the selected enrichments in a sensible order, collecting reasons."""
    current, reasons = deepcopy(plan), []
    steps = []
    if strength_per_week:
        steps.append(lambda p: place_strength(p, profile, strength_per_week))
    if fuelling:
        steps.append(lambda p: add_fuelling_cues(p, profile))
    if durability:
        steps.append(lambda p: add_durability_notes(p, profile))
    if cadence:
        steps.append(lambda p: add_cadence_cues(p, profile))
    if heat_race:
        steps.append(lambda p: add_heat_block(p, heat_race, profile))
    for step in steps:
        result = step(current)
        current = result.plan
        reasons.extend(result.reasons)
    return Enrichment(current, reasons)
