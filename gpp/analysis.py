"""Analysis over training history: compliance, threshold re-estimation,
marathon shape, and the daily readiness nudge.

Pure functions over plain records, so they are testable without a Garmin
account. `history.py` feeds them from SQLite once activities are synced.

Records are dicts with at least:
  activity: date (ISO), distance_m, seconds, and optionally avg_hr
  planned:  date (ISO), seconds (the estimate), name
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import models


# Final Surge's completion colour-coding, planned vs actual duration.
def compliance(
    planned_seconds: float, actual_seconds: float | None, bands=(0.5, 0.8, 1.2, 1.5)
) -> str:
    """green 80-120%, yellow 50-79% or 121-150%, red outside, grey if no data."""
    if actual_seconds is None or planned_seconds <= 0:
        return "grey"
    ratio = actual_seconds / planned_seconds
    lo_red, lo_yellow, hi_yellow, hi_red = bands
    if lo_yellow <= ratio <= hi_yellow:
        return "green"
    if lo_red <= ratio < lo_yellow or hi_yellow < ratio <= hi_red:
        return "yellow"
    return "red"


def match_planned_to_actual(planned: list[dict], activities: list[dict]) -> list[dict]:
    """Pair each planned session with the same-day activity, if any."""
    by_day: dict[str, list[dict]] = {}
    for a in activities:
        by_day.setdefault(a["date"], []).append(a)
    out = []
    for p in planned:
        candidates = by_day.get(p["date"], [])
        actual = max(candidates, key=lambda a: a.get("seconds", 0)) if candidates else None
        out.append(
            {
                "date": p["date"],
                "name": p.get("name"),
                "planned_seconds": p["seconds"],
                "actual_seconds": actual.get("seconds") if actual else None,
                "actual_distance_m": actual.get("distance_m") if actual else None,
                "status": compliance(p["seconds"], actual.get("seconds") if actual else None),
            }
        )
    return out


# --- threshold re-estimation ------------------------------------------------


@dataclass
class ThresholdEstimate:
    threshold_pace: float  # seconds/km
    basis: str
    activity_date: str
    confidence: str  # high | medium | low


def reestimate_threshold(
    activities: list[dict], as_of: dt.date | None = None, days: int = 60
) -> ThresholdEstimate | None:
    """Best sustained effort in the window, treated as a one-hour race via Riegel.

    Uses runs of 20-90 minutes. A 40-70 minute effort is taken as threshold
    directly (high confidence); shorter or longer ones are converted with
    Riegel (medium). Only faster-than-current estimates should replace a
    profile threshold automatically; slower ones may just be easy days.
    """
    as_of = as_of or dt.date.today()
    best: tuple[float, dict] | None = None
    for a in activities:
        try:
            when = dt.date.fromisoformat(a["date"])
        except (KeyError, ValueError):
            continue
        if (as_of - when).days > days or a.get("distance_m", 0) <= 0 or a.get("seconds", 0) <= 0:
            continue
        minutes = a["seconds"] / 60
        if not 20 <= minutes <= 90:
            continue
        # Invert Riegel: the distance this effort implies for 3600 s, hence the pace.
        d_hour = a["distance_m"] * (3600.0 / a["seconds"]) ** (1 / models.RIEGEL_EXPONENT)
        pace = 3600.0 / (d_hour / 1000.0)
        if best is None or pace < best[0]:
            best = (pace, a)
    if best is None:
        return None
    pace, a = best
    minutes = a["seconds"] / 60
    if 40 <= minutes <= 70:
        conf, basis = "high", f"{minutes:.0f}-minute effort, close to a one-hour race"
    else:
        conf, basis = "medium", f"{minutes:.0f}-minute effort, converted to one hour with Riegel"
    return ThresholdEstimate(
        threshold_pace=pace, basis=basis, activity_date=a["date"], confidence=conf
    )


# --- marathon shape ---------------------------------------------------------

# Runalyze's weighting (2/3 recent weekly mileage, 1/3 long runs) is theirs;
# the reference volumes are ours and stated: "well prepared" is about 1.5x
# the race distance per week on average, and long runs about 3/4 of it.
SHAPE_WEEKLY_FACTOR = 1.5
SHAPE_LONG_FACTOR = 0.75


@dataclass
class Shape:
    score: int
    mileage_score: int
    long_run_score: int
    basis: str


def marathon_shape(weekly_km: list[float], long_runs_km: list[float], goal_km: float) -> Shape:
    """0-100: do you have the endurance for this distance?

    weekly_km: the last ~26 weeks of volume; long_runs_km: the last ~10 weeks'
    longest runs.
    """
    if goal_km <= 0:
        raise ValueError("goal distance must be positive")
    weekly = [x for x in weekly_km if x >= 0][-26:]
    longs = sorted(x for x in long_runs_km if x >= 0)[-10:]
    avg_weekly = sum(weekly) / len(weekly) if weekly else 0.0
    top_longs = sorted(longs)[-3:]
    avg_long = sum(top_longs) / len(top_longs) if top_longs else 0.0
    mileage = min(1.0, avg_weekly / (goal_km * SHAPE_WEEKLY_FACTOR))
    long_run = min(1.0, avg_long / (goal_km * SHAPE_LONG_FACTOR))
    score = round(100 * (2 / 3 * mileage + 1 / 3 * long_run))
    return Shape(
        score=score,
        mileage_score=round(100 * mileage),
        long_run_score=round(100 * long_run),
        basis=f"avg {avg_weekly:.0f} km/week over {len(weekly)} weeks; top long runs avg {avg_long:.0f} km",
    )


# --- today's nudge ----------------------------------------------------------


@dataclass
class Advice:
    downgrade: bool
    reasons: list[str] = field(default_factory=list)
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {"downgrade": self.downgrade, "reasons": self.reasons, "suggestion": self.suggestion}


def today_advice(
    planned_role: str | None,
    readiness: int | None = None,
    hrv_status: str | None = None,
    sleep_score: int | None = None,
    resting_hr: int | None = None,
    resting_hr_baseline: int | None = None,
) -> Advice:
    """Suggest -- never impose -- an easier day when the body says so.

    HRV-guided training shows small-to-medium effects in small trials, and
    Garmin's readiness score is a black box; both are treated as signals to
    show with their reason, not as commands.
    """
    reasons = []
    if readiness is not None and readiness < 40:
        reasons.append(f"Garmin training readiness is low ({readiness}/100)")
    if hrv_status and hrv_status.lower() in ("low", "unbalanced", "poor"):
        reasons.append(f"HRV status is {hrv_status.lower()}")
    if sleep_score is not None and sleep_score < 50:
        reasons.append(f"sleep score was {sleep_score}/100")
    if resting_hr and resting_hr_baseline and resting_hr >= resting_hr_baseline + 7:
        reasons.append(f"resting HR is {resting_hr - resting_hr_baseline} bpm above your baseline")
    hard = planned_role in ("quality", "race", "long", "medium-long")
    if reasons and hard and planned_role != "race":
        return Advice(
            downgrade=True,
            reasons=reasons,
            suggestion="Consider swapping today's session for an easy run and moving the hard one by a day.",
        )
    if reasons:
        return Advice(
            downgrade=False,
            reasons=reasons,
            suggestion="Today is easy anyway; just take it gently.",
        )
    return Advice(
        downgrade=False, reasons=[], suggestion="Nothing suggests changing today's session."
    )


# --- history aggregation ----------------------------------------------------


def weekly_from_activities(activities: list[dict]) -> list[dict]:
    """[{start, km, seconds, runs, longest_km}] by ISO week, oldest first."""
    weeks: dict[dt.date, dict] = {}
    for a in activities:
        try:
            when = dt.date.fromisoformat(a["date"])
        except (KeyError, ValueError):
            continue
        start = when - dt.timedelta(days=when.weekday())
        w = weeks.setdefault(
            start,
            {"start": start.isoformat(), "km": 0.0, "seconds": 0.0, "runs": 0, "longest_km": 0.0},
        )
        km = a.get("distance_m", 0) / 1000
        w["km"] += km
        w["seconds"] += a.get("seconds", 0)
        w["runs"] += 1
        w["longest_km"] = max(w["longest_km"], km)
    return [
        dict(v, km=round(v["km"], 1), longest_km=round(v["longest_km"], 1))
        for _, v in sorted(weeks.items())
    ]
