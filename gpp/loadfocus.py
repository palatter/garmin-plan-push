"""Garmin's Load Focus, computed on the plan before it is run (#143).

The watch buckets training load into low aerobic, high aerobic and
anaerobic over the last four weeks and calls the mix balanced, or short of
one bucket. Garmin does not publish the arithmetic; what it does say is that
long work under ~85% of max HR counts as base, tempo work as high aerobic,
and repetition work as anaerobic. Mapping the plan's intensity scale to those
three buckets gives the same shape from the plan alone, so nobody discovers
in week five that the block never left the base bucket.
"""

from __future__ import annotations

import datetime as dt

from .load import week_start
from .plan import Plan
from .profile import Profile
from .timeline import workout_timeline

BUCKETS = ("low_aerobic", "high_aerobic", "anaerobic")
# Intensity scale (timeline.py): recovery .08, easy .24, steady .42, marathon
# .56, threshold .72, interval .88, repetition 1.0. Marathon and threshold
# work is the tempo band; intervals and reps are anaerobic on the watch.
HIGH_AEROBIC_FROM = 0.5
ANAEROBIC_FROM = 0.8
WINDOW_WEEKS = 4


def bucket_for(intensity: float) -> str:
    if intensity >= ANAEROBIC_FROM:
        return "anaerobic"
    if intensity >= HIGH_AEROBIC_FROM:
        return "high_aerobic"
    return "low_aerobic"


def _verdict(shares: dict[str, float]) -> str:
    low, high, hard = shares["low_aerobic"], shares["high_aerobic"], shares["anaerobic"]
    if low < 0.55:
        return "short on low-aerobic work: the base bucket should carry most of the load"
    if high < 0.10:
        return "short on high-aerobic work: no tempo or marathon-pace load at all"
    if hard > 0.20:
        return "anaerobic-heavy: interval and repetition load above a fifth of the total"
    if hard == 0 and high >= 0.10:
        return "balanced, with no anaerobic work -- fine in base, thin before a 5k or 10k"
    return "balanced across the three buckets"


def load_focus(plan: Plan, profile: Profile, weeks: int = WINDOW_WEEKS) -> dict:
    """Minutes and shares per bucket over the plan's last `weeks`, and per week."""
    per_week: dict[dt.date, dict[str, float]] = {}
    for workout in plan.sorted_workouts():
        monday = week_start(workout.date)
        row = per_week.setdefault(monday, dict.fromkeys(BUCKETS, 0.0))
        for block in workout_timeline(workout, profile):
            if block["open_ended"]:
                continue
            row[bucket_for(block["intensity"])] += block["seconds"] / 60.0
    ordered = sorted(per_week.items())
    window = ordered[-weeks:] if len(ordered) > weeks else ordered
    totals = dict.fromkeys(BUCKETS, 0.0)
    for _, row in window:
        for key in BUCKETS:
            totals[key] += row[key]
    grand = sum(totals.values())
    shares = {key: (totals[key] / grand if grand else 0.0) for key in BUCKETS}
    return {
        "window_weeks": len(window),
        "minutes": {key: round(totals[key]) for key in BUCKETS},
        "shares": {key: round(shares[key], 3) for key in BUCKETS},
        "verdict": _verdict(shares) if grand else "no load in the window",
        "weeks": [
            {"start": monday.isoformat(), **{key: round(row[key]) for key in BUCKETS}}
            for monday, row in ordered
        ],
    }


def describe(focus: dict) -> str:
    m, s = focus["minutes"], focus["shares"]
    return (
        f"Load focus over the last {focus['window_weeks']} week(s): "
        f"low aerobic {m['low_aerobic']} min ({s['low_aerobic']:.0%}), "
        f"high aerobic {m['high_aerobic']} min ({s['high_aerobic']:.0%}), "
        f"anaerobic {m['anaerobic']} min ({s['anaerobic']:.0%}) -- {focus['verdict']}."
    )
