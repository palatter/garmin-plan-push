"""Generates the prompt you hand to Claude (or any model) to write a plan.

The point of generating this rather than writing it by hand: it embeds your
*resolved* zones, so the model sees "easy = 5:09-5:39/km" rather than having
to guess; it embeds the real schema, so it cannot drift from what the
validator accepts; and it embeds what a coach would know -- injuries,
availability, the goal race, your standing instructions -- because the one
consistent finding about AI-written plans is that quality rises with the
amount of real input they get.

The rules section is the sanity report (checks.py) stated as instructions:
a plan that follows them will pass; one that does not is sent back with the
report as a correction turn.
"""

from __future__ import annotations

import datetime as dt
import json

from .plan import PLAN_SCHEMA
from .profile import Profile
from .units import format_pace

TEMPLATE = """\
You are writing a structured running plan that will be compiled into Garmin
workouts and pushed to a Garmin watch. Output ONLY a single JSON object
matching the schema below. No prose, no markdown fence, no commentary.

ATHLETE
{profile}
{coach_context}
{dates}
FORMAT RULES
1. Every step needs exactly one of: "duration", "distance", or "until": "lap".
   Never two, never zero. (A strength "exercise" step uses "count" instead.)
2. Prefer zone names over explicit paces. Known pace zones: {zones}.
   Daniels letters also work: E, M, T, I, R. Use an explicit
   {{"type": "pace", "slow": "...", "fast": "..."}} only for race-specific work
   where a zone is genuinely wrong.
3. "slow" is the SLOWER pace (bigger mm:ss), "fast" is the QUICKER pace
   (smaller mm:ss). Getting these backwards is rejected by the validator.
4. Warm-ups and cool-downs are "warmup"/"cooldown" kinds with an "easy" or
   "recovery" pace target, or none.
5. Work intervals are kind "run". Jogged recoveries are "recover", standing
   ones are "rest". Strides are kind "stride": 15-25 seconds fast and relaxed,
   full recovery, no pace target.
6. Wrap repeated blocks in {{"kind": "repeat", "reps": N, "steps": [...]}}.
   Put the recovery INSIDE the repeat. Never nest repeats more than two deep.
7. Dates are ISO "YYYY-MM-DD". Workout names are short and unique per day.
8. Give every workout a "role": easy, long, medium-long, quality, race,
   recovery, cross, strength or rest -- and a "phase": base, build, peak,
   taper or recovery. Use "notes" for the session's purpose in one sentence.
   Use a step "note" sparingly, for cues the runner needs mid-rep.
9. Other targets: {{"type": "hr", "zone": 1-5}}, {{"type": "power", "zone": 1-7}}
   or {{"type": "power", "low": W, "high": W}}, {{"type": "rpe", "value": 1-10}},
   {{"type": "cadence", "low": spm, "high": spm}}, {{"type": "none"}}.

COACHING RULES (the plan is checked against these; violations come back to you)
A. Progress gently. No single run longer than about 110% of the athlete's
   longest recent run. Weekly volume should not jump; build for 2-3 weeks,
   then a lighter week (a "deload" at roughly 60-70% volume).
B. Mostly easy. Around 80% of running time easy or recovery, and keep some
   steady/moderate running -- a plan that is only very easy or very hard is
   wrong. Never schedule two quality sessions on consecutive days.
C. Structure the week: a recovery or easy day after each quality session;
   strides in base weeks; in marathon blocks, a medium-long run midweek.
D. Taper toward the A race: over the final ~2 weeks reduce volume 41-60%,
   keep intensity and keep the number of sessions. Do not cut more than 60%.
   No B or C race inside the 14 days before an A race.
E. Respect availability and constraints exactly. Never schedule on a day the
   athlete said they cannot run, and never exceed their session limits.
F. If the athlete has an injury or constraint listed, honour it in every
   session -- for example no hill repeats with an Achilles note.
{language_rule}
SCHEMA
{schema}

EXAMPLE OUTPUT
{example}
"""

EXAMPLE = {
    "plan": "Autumn 10k block",
    "race_date": "2026-10-25",
    "races": [{"name": "City 10k", "date": "2026-10-25", "priority": "A", "distance": "10k"}],
    "workouts": [
        {
            "name": "Threshold 5x1k",
            "date": "2026-09-24",
            "sport": "running",
            "role": "quality",
            "phase": "build",
            "notes": "Controlled threshold volume; should feel comfortably hard, not a race.",
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
        {
            "name": "Easy + strides",
            "date": "2026-09-26",
            "sport": "running",
            "role": "easy",
            "phase": "build",
            "notes": "Aerobic maintenance with a little leg speed at the end.",
            "steps": [
                {"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": "easy"}},
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
                },
            ],
        },
    ],
}


def coach_context(profile: Profile) -> str:
    """Everything the athlete told us that a model could not guess."""
    lines: list[str] = []
    if profile.goal_race:
        lines.append(f"Goal race: {profile.goal_race.describe()}.")
    if profile.availability:
        lines.append(f"Availability: {profile.availability.describe()}.")
    if profile.injuries:
        lines.append("Injury history: " + "; ".join(profile.injuries) + ".")
    if profile.constraints:
        lines.append("Constraints: " + "; ".join(profile.constraints) + ".")
    if profile.longest_recent_run_km:
        lines.append(f"Longest run in the last month: {profile.longest_recent_run_km:g} km.")
    if profile.recent_weekly_km:
        lines.append(f"Recent weekly volume: about {profile.recent_weekly_km:g} km.")
    if profile.instructions:
        lines.append(f"Standing instructions from the athlete: {profile.instructions}")
    if not lines:
        return ""
    return "\nWHAT THE ATHLETE TOLD US\n" + "\n".join(lines) + "\n"


def dates_block(today: dt.date | None) -> str:
    """The one thing a model cannot know: what day it is.

    Without this, "four weeks to a 10k" leaves the year and the first Monday
    to chance, and plans arrive dated in the past.
    """
    today = today or dt.date.today()
    next_monday = today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)
    return (
        "DATES\n"
        f"Today is {today.isoformat()} ({today.strftime('%A')}). Unless the request says\n"
        f"otherwise, the plan starts on the next Monday, {next_monday.isoformat()}, and every\n"
        "date is on or after today. Count the weeks from that start; if a race date is\n"
        "given, end the plan on it.\n"
    )


def build_prompt(profile: Profile, today: dt.date | None = None) -> str:
    zones_line = ", ".join(
        f"{name} ({format_pace(slow, profile.imperial)}-{format_pace(fast, profile.imperial)})"
        for name, (slow, fast) in profile.zone_table().items()
    )
    language_rule = ""
    if profile.language and profile.language.lower() not in ("en", "english"):
        language_rule = (
            f'G. Write every "notes" and "note" value in {profile.language}. '
            "Keep all JSON keys, zone names and kinds in English.\n"
        )
    return TEMPLATE.format(
        profile=profile.describe(),
        coach_context=coach_context(profile),
        dates=dates_block(today),
        zones=zones_line,
        language_rule=language_rule,
        schema=json.dumps(PLAN_SCHEMA, indent=2),
        example=json.dumps(EXAMPLE, indent=2),
    )


ONE_WORKOUT_TEMPLATE = """\
Rewrite ONE workout of an existing plan. Output ONLY the JSON object for that
single workout (matching the "workouts" item schema), not the whole plan.

The workout to rewrite is dated {date} and currently looks like this:
{current}

The rest of the plan, for context (do not repeat it):
{context}

Instruction from the athlete: {instruction}
"""


def build_rewrite_prompt(current: dict, context: list[dict], instruction: str) -> str:
    """For "rewrite just Thursday": same rules, one workout in, one out."""
    summary = "\n".join(
        f"- {w['date']} {w['name']} ({w.get('role', '?')}, {w.get('phase', '?')})" for w in context
    )
    return ONE_WORKOUT_TEMPLATE.format(
        date=current.get("date"),
        current=json.dumps(current, indent=2),
        context=summary or "(none)",
        instruction=instruction,
    )
