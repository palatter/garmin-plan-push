"""The plan sanity report.

Every check here is a documented way AI-written plans go wrong, or a rule
with real evidence behind it. None of them is an injury-prediction model.
The report is shown before anything reaches a watch, and it is fed back to
the model as a correction turn during generation.

Severity:
  block  the plan contradicts something the athlete explicitly said
         (availability, a race, a constraint) -- always sent back
  warn   the plan breaks a coaching rule with evidence behind it -- shown,
         sent back once, then the athlete decides
  info   worth knowing, no action implied

Evidence, briefly (full citations in ROADMAP.md):
  * long-run spike: >110% of the longest run in the prior 30 days raised
    overuse-injury risk in a 5,200-runner cohort; the "10% weekly rule" did
    not hold up (Nielsen 2014) -- so the weekly check is loose (25%) and
    the single-run check is the one that matters.
  * taper: 41-60% volume cut over ~2 weeks, intensity and frequency kept;
    beyond 60% performs worse (Bosquet meta-analysis).
  * intensity: mostly easy is well supported; the exact split is not, so
    the rule is "warn past ~30% hard", never "enforce 80/20".
  * monotony: same-load-every-day precedes overtraining (Foster 1998).
  * the rest are the failure modes coaches found in ChatGPT plans: rapid
    progression, no middle gear, hard days stacked, no rest.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from itertools import pairwise

from .load import MONOTONY_LIMIT, WeekStats, infer_role, week_start, weekly_stats
from .plan import Plan, Workout
from .profile import Profile
from .timeline import workout_summary

LONG_RUN_SPIKE = 1.10
WEEKLY_RAMP = 1.25
DELOAD_AFTER_WEEKS = 4
DELOAD_FRACTION = 0.85  # a week below this share of the previous week counts as lighter
HARD_SHARE_LIMIT = 0.30
MIDDLE_GEAR_MIN_HARD = 0.15
QUALITY_MINUTES = 10
PROGRESSION_CAP = 1.30
TAPER_DAYS = 14
TAPER_MIN_CUT = 0.30
TAPER_MAX_CUT = 0.60
RACE_PROTECT_DAYS = 14

MARATHON_WORDS = ("marathon",)
HALF_WORDS = ("half",)


@dataclass
class Finding:
    code: str
    severity: str  # block | warn | info
    message: str
    dates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "dates": self.dates,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.blocks

    def add(self, code: str, severity: str, message: str, dates: list[str] | None = None) -> None:
        self.findings.append(Finding(code, severity, message, dates or []))

    def text(self) -> str:
        if not self.findings:
            return "Sanity report: nothing to flag."
        lines = ["Sanity report:"]
        for f in self.findings:
            mark = {"block": "!!", "warn": "! ", "info": "i "}[f.severity]
            when = f"  [{', '.join(f.dates)}]" if f.dates else ""
            lines.append(f"  {mark} {f.message}{when}")
        return "\n".join(lines)

    def feedback(self, include_warns: bool = True) -> str:
        """What goes back to the model as a correction."""
        chosen = self.blocks + (self.warns if include_warns else [])
        return "\n".join(
            f"- {f.message}" + (f" ({', '.join(f.dates)})" if f.dates else "") for f in chosen
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "blocks": len(self.blocks),
            "warns": len(self.warns),
            "findings": [f.to_dict() for f in self.findings],
        }


def check_plan(plan: Plan, profile: Profile) -> Report:
    report = Report()
    workouts = plan.sorted_workouts()
    if not workouts:
        return report
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    weeks = weekly_stats(plan, profile)
    running = [summaries[id(w)]["metres"] for w in workouts if w.sport == "running"]
    median_metres = sorted(running)[len(running) // 2] if running else 0.0
    roles = {id(w): infer_role(w, summaries[id(w)], median_metres) for w in workouts}

    _check_availability(report, workouts, summaries, profile)
    _check_constraints(report, workouts, profile)
    _check_races(report, plan)
    _check_long_run_spike(report, workouts, summaries, profile)
    _check_weekly_ramp(report, weeks, profile)
    _check_deload(report, weeks)
    _check_intensity(report, workouts, summaries, weeks, profile)
    _check_hard_days(report, workouts, roles)
    _check_monotony(report, weeks)
    _check_progression(report, weeks)
    _check_taper(report, plan, weeks)
    _check_structure(report, plan, weeks, profile)
    return report


# --- what the athlete said --------------------------------------------------


def _check_availability(
    report: Report, workouts: list[Workout], summaries: dict, profile: Profile
) -> None:
    avail = profile.availability
    if not avail:
        return
    bad_days = [w for w in workouts if not avail.allows(w.date) and w.sport != "strength"]
    if bad_days:
        report.add(
            "availability-day",
            "block",
            "Sessions land on days the athlete said they cannot run",
            [f"{w.date.isoformat()} {w.name}" for w in bad_days],
        )
    too_long = []
    for w in workouts:
        limit = avail.max_minutes(w.date)
        if limit and summaries[id(w)]["seconds"] > limit * 60 * 1.05:
            too_long.append(
                f"{w.date.isoformat()} {w.name} (~{round(summaries[id(w)]['seconds'] / 60)} min > {limit})"
            )
    if too_long:
        report.add(
            "availability-length",
            "block",
            "Sessions exceed the athlete's time limit for that day",
            too_long,
        )
    if avail.sessions_per_week:
        heavy = [
            wk
            for wk in weekly_stats_for(workouts, profile)
            if wk.sessions > avail.sessions_per_week
        ]
        if heavy:
            report.add(
                "availability-count",
                "warn",
                f"Weeks with more than the {avail.sessions_per_week} sessions the athlete asked for",
                [wk.start.isoformat() for wk in heavy],
            )


def weekly_stats_for(workouts: list[Workout], profile: Profile) -> list[WeekStats]:
    return weekly_stats(Plan(plan="_", workouts=workouts), profile)


def _check_constraints(report: Report, workouts: list[Workout], profile: Profile) -> None:
    """Literal "no X" constraints against workout names, notes and step notes."""
    rules = []
    for text in profile.constraints + profile.injuries:
        m = re.search(r"\bno\s+([a-z][a-z\-]{2,30})", text.lower())
        if m:
            rules.append((m.group(1).rstrip("s"), text))
    if not rules:
        return
    for keyword, source in rules:
        hits = []
        for w in workouts:
            blob = " ".join(
                filter(None, [w.name, w.notes, *[s.note for s in _all_steps(w.steps)]])
            ).lower()
            hilly = any((s.grade or 0) > 2 for s in _all_steps(w.steps))
            if keyword in blob or (keyword.startswith("hill") and hilly):
                hits.append(f"{w.date.isoformat()} {w.name}")
        if hits:
            report.add(
                "constraint",
                "block",
                f'Athlete said "{source}", but these sessions include it',
                hits,
            )


def _all_steps(steps):
    for s in steps:
        yield s
        if s.is_repeat:
            yield from _all_steps(s.steps)


def _check_races(report: Report, plan: Plan) -> None:
    a = plan.a_race
    if not a:
        return
    inside = [
        r
        for r in plan.races
        if r.priority in ("B", "C") and 0 < (a.date - r.date).days <= RACE_PROTECT_DAYS
    ]
    if inside:
        report.add(
            "race-window",
            "block",
            f"B/C races inside the {RACE_PROTECT_DAYS} days before the A race ({a.name}); they will wreck the taper",
            [f"{r.date.isoformat()} {r.name}" for r in inside],
        )
    after = [
        w
        for w in plan.workouts
        if w.date > a.date and w.role not in ("recovery", "rest", "easy", None)
    ]
    if after and not any(w.phase == "recovery" for w in after):
        report.add(
            "post-race",
            "info",
            "Sessions after the A race are not marked as recovery; the week after a race should be easy",
            [w.date.isoformat() for w in after[:3]],
        )


# --- progression ------------------------------------------------------------


def _check_long_run_spike(
    report: Report, workouts: list[Workout], summaries: dict, profile: Profile
) -> None:
    baseline = (profile.longest_recent_run_km or 0.0) * 1000.0
    first_day = min(w.date for w in workouts)
    hits = []
    for w in workouts:
        if w.sport != "running":
            continue
        # With no baseline from the profile, the first week of the plan IS the
        # baseline; comparing its long run to its easy runs would be noise.
        if not baseline and (w.date - first_day).days < 7:
            continue
        metres = summaries[id(w)]["metres"]
        window = [
            summaries[id(o)]["metres"]
            for o in workouts
            if o.sport == "running" and 0 < (w.date - o.date).days <= 30
        ]
        reference = max([baseline, *window]) if (baseline or window) else 0.0
        if reference and metres > reference * LONG_RUN_SPIKE:
            hits.append(
                f"{w.date.isoformat()} {w.name} ({metres / 1000:.1f} km vs {reference / 1000:.1f} km)"
            )
    if hits:
        report.add(
            "long-run-spike",
            "warn",
            "A single run jumps more than 10% past the longest of the previous 30 days -- the best-supported injury signal there is",
            hits,
        )


def _check_weekly_ramp(report: Report, weeks: list[WeekStats], profile: Profile) -> None:
    hits = []
    previous_peak = (profile.recent_weekly_km or 0.0) * 1000.0
    for index, week in enumerate(weeks):
        recent = [w.metres for w in weeks[max(0, index - 2) : index]]
        reference = max([previous_peak, *recent]) if (previous_peak or recent) else 0.0
        if reference and week.metres > reference * WEEKLY_RAMP:
            hits.append(
                f"week of {week.start.isoformat()} ({week.km:.0f} km vs {reference / 1000:.0f} km)"
            )
    if hits:
        report.add(
            "weekly-ramp",
            "warn",
            "Weekly volume jumps more than 25% over recent weeks (the 10% rule is folklore; 25% is where studies start to see it)",
            hits,
        )


def _check_deload(report: Report, weeks: list[WeekStats]) -> None:
    if len(weeks) < DELOAD_AFTER_WEEKS + 1:
        return
    streak = 0
    for index in range(1, len(weeks)):
        lighter = weeks[index].metres < weeks[index - 1].metres * DELOAD_FRACTION
        streak = 0 if lighter else streak + 1
        if streak >= DELOAD_AFTER_WEEKS:
            report.add(
                "no-deload",
                "warn",
                f"{streak + 1} weeks in a row without a lighter week; build for 2-3 weeks, then back off to ~60-70%",
                [weeks[index].start.isoformat()],
            )
            return


def _check_progression(report: Report, weeks: list[WeekStats]) -> None:
    hits = []
    for prev, week in pairwise(weeks):
        if prev.hard_seconds >= 600 and week.hard_seconds > prev.hard_seconds * PROGRESSION_CAP:
            hits.append(
                f"week of {week.start.isoformat()} ({round(week.hard_seconds / 60)} vs {round(prev.hard_seconds / 60)} hard min)"
            )
    if hits:
        report.add(
            "progression-cap",
            "warn",
            "Hard-running minutes rise more than 30% week over week",
            hits,
        )


# --- intensity distribution -------------------------------------------------


def _check_intensity(
    report: Report, workouts, summaries, weeks: list[WeekStats], profile: Profile
) -> None:
    total = sum(summaries[id(w)]["seconds"] for w in workouts)
    hard = sum(summaries[id(w)]["hard_seconds"] for w in workouts)
    if total <= 0:
        return
    share = hard / total
    if share > HARD_SHARE_LIMIT:
        report.add(
            "hard-share",
            "warn",
            f"{share:.0%} of running time is at threshold or harder; mostly-easy is the well-supported pattern (aim under ~30%)",
        )
    # Middle gear: any steady/marathon-intensity time at all?
    from .timeline import ZONE_INTENSITY, workout_timeline

    moderate = 0.0
    for w in workouts:
        for b in workout_timeline(w, profile):
            if ZONE_INTENSITY["steady"] - 0.05 <= b["intensity"] < ZONE_INTENSITY["threshold"]:
                moderate += b["seconds"]
    if len(weeks) >= 2 and share >= MIDDLE_GEAR_MIN_HARD and moderate == 0:
        report.add(
            "no-middle-gear",
            "warn",
            "Everything is either very easy or very hard -- no steady or marathon-pace running at all, the pattern runners complain about in AI plans",
        )


def _check_hard_days(report: Report, workouts: list[Workout], roles: dict) -> None:
    by_date: dict[dt.date, list[Workout]] = {}
    for w in workouts:
        by_date.setdefault(w.date, []).append(w)
    hard_days = sorted(
        d for d, ws in by_date.items() if any(roles[id(w)] in ("quality", "race") for w in ws)
    )
    stacked = [
        f"{a.isoformat()} + {b.isoformat()}" for a, b in pairwise(hard_days) if (b - a).days == 1
    ]
    if stacked:
        report.add(
            "back-to-back-quality", "warn", "Two quality sessions on consecutive days", stacked
        )

    no_recovery = []
    for day in hard_days:
        nxt = by_date.get(day + dt.timedelta(days=1), [])
        if any(roles[id(w)] in ("long", "medium-long") for w in nxt):
            no_recovery.append(f"{day.isoformat()} -> {(day + dt.timedelta(days=1)).isoformat()}")
    if no_recovery:
        report.add(
            "no-recovery-after-quality",
            "warn",
            "A long run follows a quality session with no easy day between",
            no_recovery,
        )


def _check_monotony(report: Report, weeks: list[WeekStats]) -> None:
    hits = [
        f"week of {w.start.isoformat()} ({w.monotony()})"
        for w in weeks
        if (w.monotony() or 0) > MONOTONY_LIMIT
    ]
    if hits:
        report.add(
            "monotony",
            "warn",
            "Daily load barely varies across the week (Foster monotony > 2); vary the days -- same load every day precedes overtraining",
            hits,
        )


# --- taper and structure ----------------------------------------------------


def _check_taper(report: Report, plan: Plan, weeks: list[WeekStats]) -> None:
    a = plan.a_race
    if not a or len(weeks) < 3:
        return
    # The taper is the final two ISO weeks up to and including race week.
    upto = [w for w in weeks if w.start <= a.date]
    taper = upto[-2:]
    before = upto[:-2]
    if not before or not taper:
        return
    peak = max(w.metres for w in before)
    if peak <= 0:
        return
    taper_avg = sum(w.metres for w in taper) / len(taper)
    cut = 1 - taper_avg / peak
    when = [w.start.isoformat() for w in taper]
    if cut < TAPER_MIN_CUT:
        report.add(
            "taper-too-shallow",
            "warn",
            f"Final two weeks cut volume only {cut:.0%} from peak; the evidence says 41-60%",
            when,
        )
    elif cut > TAPER_MAX_CUT:
        report.add(
            "taper-too-deep",
            "warn",
            f"Final two weeks cut volume {cut:.0%} from peak; more than 60% performs worse than 41-60%",
            when,
        )
    peak_sessions = max(w.sessions for w in before)
    if any(w.sessions < peak_sessions * 0.6 for w in taper):
        report.add(
            "taper-frequency",
            "warn",
            "Taper drops the number of sessions; keep frequency, cut duration",
            when,
        )
    if all(w.hard_seconds == 0 for w in taper) and any(w.hard_seconds > 0 for w in before):
        report.add(
            "taper-intensity",
            "warn",
            "Taper removes all intensity; keep some quality, shorter",
            when,
        )


def _check_structure(report: Report, plan: Plan, weeks: list[WeekStats], profile: Profile) -> None:
    base_weeks = [w for w in weeks if "base" in w.phases and not w.strides]
    if base_weeks:
        report.add(
            "no-strides",
            "info",
            "Base-phase weeks without strides; a few 20-second strides keep leg speed cheaply",
            [w.start.isoformat() for w in base_weeks],
        )
    goal = plan.a_race
    distance = (
        goal.distance
        if goal and goal.distance
        else (profile.goal_race.distance if profile.goal_race else "")
    ) or ""
    is_marathon = any(x in distance.lower() for x in MARATHON_WORDS) and not any(
        x in distance.lower() for x in HALF_WORDS
    )
    if is_marathon:
        build = [w for w in weeks if "build" in w.phases and not w.medium_long]
        if build:
            report.add(
                "no-medium-long",
                "info",
                "Marathon build weeks without a midweek medium-long run (Pfitzinger's ~90-120 min)",
                [w.start.isoformat() for w in build],
            )
    rest_free = [w for w in weeks if w.sessions >= 7]
    if rest_free:
        report.add(
            "no-rest-day",
            "warn",
            "Weeks with no rest day at all",
            [w.start.isoformat() for w in rest_free],
        )


def check(plan: Plan, profile: Profile) -> Report:
    """Public entry point."""
    return check_plan(plan, profile)


def week_of(day: dt.date) -> dt.date:
    return week_start(day)
