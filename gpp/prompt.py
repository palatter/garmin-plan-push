"""Generates the prompt you hand to Claude (or any model) to write a plan.

The point of generating this rather than writing it by hand: it embeds your
*resolved* zones, so the model sees "easy = 5:09-5:39/km" rather than having
to guess, and it embeds the real schema, so it cannot drift from what the
validator accepts.
"""

from __future__ import annotations

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

RULES
1. Every step needs exactly one of: "duration", "distance", or "until": "lap".
   Never two, never zero.
2. Prefer zone names over explicit paces. Known pace zones: {zones}.
   Use an explicit {{"type": "pace", "slow": "...", "fast": "..."}} only for
   race-specific work where a zone is genuinely wrong.
3. "slow" is the SLOWER pace (bigger mm:ss), "fast" is the QUICKER pace
   (smaller mm:ss). Getting these backwards is rejected by the validator.
4. Warm-ups and cool-downs are "warmup"/"cooldown" kinds and normally carry an
   "easy" or "recovery" pace target, or no target at all.
5. Work intervals are kind "run". Jogged/standing recoveries between them are
   kind "recover" (moving) or "rest" (stationary).
6. Wrap repeated blocks in {{"kind": "repeat", "reps": N, "steps": [...]}}.
   Put the recovery INSIDE the repeat. Do not nest repeats more than two deep.
7. Dates are ISO "YYYY-MM-DD". Workout names are short and unique per day.
8. Use "notes" on a workout for the session's purpose in one sentence. Use a
   step "note" sparingly, for cues the runner needs mid-rep.

SCHEMA
{schema}

EXAMPLE OUTPUT
{example}
"""

EXAMPLE = {
    "plan": "Autumn 10k block",
    "workouts": [
        {
            "name": "Threshold 5x1k",
            "date": "2026-09-24",
            "sport": "running",
            "notes": "Controlled threshold volume; should feel comfortably hard, not a race.",
            "steps": [
                {
                    "kind": "warmup",
                    "duration": "15m",
                    "target": {"type": "pace", "zone": "easy"},
                },
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
                {
                    "kind": "cooldown",
                    "duration": "10m",
                    "target": {"type": "pace", "zone": "easy"},
                },
            ],
        }
    ],
}


def build_prompt(profile: Profile) -> str:
    zones_line = ", ".join(
        f"{name} ({format_pace(slow, profile.imperial)}"
        f"-{format_pace(fast, profile.imperial)})"
        for name, (slow, fast) in (
            (n, profile.pace_zone(n)) for n in profile.pace_zones
        )
    )
    return TEMPLATE.format(
        profile=profile.describe(),
        zones=zones_line,
        schema=json.dumps(PLAN_SCHEMA, indent=2),
        example=json.dumps(EXAMPLE, indent=2),
    )
