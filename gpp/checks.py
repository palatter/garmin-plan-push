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

from .load import MONOTONY_LIMIT, WeekStats, roles_for, week_start, weekly_stats
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
LONG_RUN_SHARE_MARATHON = 0.35
LONG_RUN_SHARE_OTHER = 0.40
PRE_RACE_QUIET_DAYS = 3
REST_STREAK_DAYS = 10
CADENCE_RANGE = (150, 200)
MARATHON_LONG_RUN_METRES = 30_000
PLAN_SPAN_LIMIT_DAYS = 366

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
# Words that name the activity rather than a thing to avoid: "no running
# Mondays" is a day rule, not a ban on running.
GENERIC_WORDS = {
    "run",
    "runs",
    "running",
    "session",
    "sessions",
    "workout",
    "workouts",
    "training",
    "more",
    "longer",
    "harder",
    "hard",
    "day",
    "days",
    "than",
}
_DAY_RULE = re.compile(
    r"\bno\s+(?:([a-z][a-z\- ]{1,30}?)\s+)?(?:on\s+)?"
    r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|"
    r"sat(?:urday)?|sun(?:day)?)s?\b"
)
_KEYWORD_RULE = re.compile(r"\bno\s+([a-z][a-z\-]{2,30})")
_FOR_WEEKS = re.compile(r"\bfor\s+(\d+)\s+weeks?\b")


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


def check_plan(plan: Plan, profile: Profile, today: dt.date | None = None) -> Report:
    report = Report()
    workouts = plan.sorted_workouts()
    if not workouts:
        return report
    summaries = {id(w): workout_summary(w, profile) for w in workouts}
    weeks = weekly_stats(plan, profile)
    roles = roles_for(workouts, summaries)
    # Race day is not training volume: a marathon in race week would make
    # every taper look shallow and every ramp look steep.
    training = [w for w in workouts if roles[id(w)] != "race"]
    training_weeks = weekly_stats_for(training, profile) if training else weeks

    _check_dates(report, plan, workouts, today)
    _check_availability(report, workouts, summaries, profile)
    _check_availability_envelope(report, weeks, profile)
    _check_constraints(report, workouts, profile)
    _check_races(report, plan)
    _check_around_races(report, plan, workouts, roles, summaries)
    _check_targets(report, workouts, profile)
    _check_long_run_spike(report, workouts, summaries, profile)
    _check_weekly_ramp(report, training_weeks, profile)
    _check_deload(report, training_weeks)
    _check_intensity(report, workouts, summaries, weeks, profile)
    _check_hard_days(report, workouts, roles)
    _check_strength_placement(report, workouts, roles)
    _check_rest_streak(report, workouts)
    _check_monotony(report, weeks)
    _check_progression(report, training_weeks)
    _check_long_run_share(report, training_weeks, plan, profile)
    _check_two_long_runs(report, workouts, roles)
    _check_taper(report, plan, training_weeks)
    _check_structure(report, plan, weeks, profile)
    _check_goal_race(report, plan, profile, workouts, summaries, roles)
    return report


# --- dates and targets --------------------------------------------------------


def _check_dates(
    report: Report, plan: Plan, workouts: list[Workout], today: dt.date | None
) -> None:
    first, last = workouts[0].date, workouts[-1].date
    if today is not None:
        past = [w for w in workouts if w.date < today]
        if past and len(past) < len(workouts):
            report.add(
                "in-the-past",
                "warn",
                f"{len(past)} session(s) are dated before today ({today.isoformat()}); "
                "the plan may have been written without knowing the date",
                [w.date.isoformat() for w in past[:3]],
            )
    if (last - first).days > PLAN_SPAN_LIMIT_DAYS:
        report.add(
            "too-long",
            "warn",
            f"The plan spans {(last - first).days} days; blocks longer than a year are rarely followed",
            [first.isoformat(), last.isoformat()],
        )
    a = plan.a_race
    if a and a.date < first:
        report.add(
            "race-before-plan",
            "warn",
            f"The A race ({a.name}) is dated before the first session",
            [a.date.isoformat()],
        )


def _check_targets(report: Report, workouts: list[Workout], profile: Profile) -> None:
    hr_hits, cadence_hits, pace_hits = [], [], []
    fastest = min((fast for _, (_, fast) in profile.zone_table().items()), default=None)
    for w in workouts:
        for step in _all_steps(w.steps):
            t = step.target
            label = f"{w.date.isoformat()} {w.name}"
            if t.type == "hr" and t.high is not None and profile.hr_max and t.high > profile.hr_max:
                hr_hits.append(f"{label} ({t.high} > max {profile.hr_max})")
            if (
                t.type == "cadence"
                and t.low is not None
                and t.high is not None
                and (t.low < CADENCE_RANGE[0] or t.high > CADENCE_RANGE[1])
            ):
                cadence_hits.append(f"{label} ({t.low}-{t.high} spm)")
            if t.type == "pace" and t.zone is None and fastest and t.fast:
                from .units import UnitError, parse_pace

                try:
                    fast = parse_pace(t.fast, "mi" if profile.imperial else "km")
                except UnitError:
                    continue
                if fast < fastest * 0.95:
                    pace_hits.append(f"{label} ({t.fast})")
    if hr_hits:
        report.add("target-hr", "block", "Heart-rate targets above the athlete's max HR", hr_hits)
    if cadence_hits:
        report.add(
            "target-cadence",
            "warn",
            f"Cadence targets outside {CADENCE_RANGE[0]}-{CADENCE_RANGE[1]} spm",
            cadence_hits,
        )
    if pace_hits:
        report.add(
            "target-pace",
            "warn",
            "Explicit paces faster than the athlete's fastest zone -- probably a typo or a units mix-up",
            pace_hits,
        )


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


def parse_constraint(text: str) -> dict | None:
    """Turn "No running Mondays", "no hills for 6 weeks" or "no long runs on
    Sundays" into a rule the checker can apply, or None if it is not a rule.

    A day rule bans the day (or a kind of session on that day); a keyword rule
    bans a thing wherever it appears. "for N weeks" bounds either from the
    first session. Words that merely name the activity are never keywords, so
    "no running Mondays" cannot turn into a ban on running.
    """
    lowered = text.lower()
    weeks = _FOR_WEEKS.search(lowered)
    limit = int(weeks.group(1)) if weeks else None
    day = _DAY_RULE.search(lowered)
    if day:
        words = [x for x in (day.group(1) or "").split() if x not in GENERIC_WORDS]
        keyword = words[0].rstrip("s") if words else None
        return {
            "kind": "day",
            "weekday": DAY_NAMES[day.group(2)[:3]],
            "keyword": keyword,
            "weeks": limit,
            "source": text,
        }
    m = _KEYWORD_RULE.search(lowered)
    if m and m.group(1) not in GENERIC_WORDS:
        return {
            "kind": "keyword",
            "keyword": m.group(1).rstrip("s"),
            "weeks": limit,
            "source": text,
        }
    return None


def _blob(w: Workout) -> str:
    return " ".join(filter(None, [w.name, w.notes, *[s.note for s in _all_steps(w.steps)]])).lower()


def _mentions(w: Workout, keyword: str) -> bool:
    hilly = any((s.grade or 0) > 2 for s in _all_steps(w.steps))
    return keyword in _blob(w) or (keyword.startswith("hill") and hilly)


def _check_constraints(report: Report, workouts: list[Workout], profile: Profile) -> None:
    rules = [r for r in (parse_constraint(t) for t in profile.constraints + profile.injuries) if r]
    if not rules:
        return
    first_day = min(w.date for w in workouts)
    for rule in rules:
        horizon = first_day + dt.timedelta(weeks=rule["weeks"]) if rule["weeks"] else None
        scope = [w for w in workouts if horizon is None or w.date < horizon]
        if rule["kind"] == "day":
            hits = [
                f"{w.date.isoformat()} {w.name}"
                for w in scope
                if w.date.weekday() == rule["weekday"]
                and w.sport != "strength"
                and (rule["keyword"] is None or _mentions(w, rule["keyword"]))
            ]
            code = "constraint-day"
        else:
            hits = [
                f"{w.date.isoformat()} {w.name}" for w in scope if _mentions(w, rule["keyword"])
            ]
            code = "constraint"
        if hits:
            report.add(
                code,
                "block",
                f'Athlete said "{rule["source"]}", but these sessions break it',
                hits,
            )


def _check_availability_envelope(report: Report, weeks: list[WeekStats], profile: Profile) -> None:
    avail = profile.availability
    if not avail or not (avail.weekday_max_minutes and avail.weekend_max_minutes):
        return
    cap = 5 * avail.weekday_max_minutes + 2 * avail.weekend_max_minutes
    heavy = [w for w in weeks if w.seconds / 60 > cap * 1.05]
    if heavy:
        report.add(
            "availability-week",
            "block",
            f"Weeks need more time than the athlete has ({cap} min at most across the days they gave)",
            [f"week of {w.start.isoformat()} (~{round(w.seconds / 60)} min)" for w in heavy],
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


def _check_around_races(
    report: Report, plan: Plan, workouts: list[Workout], roles: dict, summaries: dict
) -> None:
    race_days = {r.date for r in plan.races} | {w.date for w in workouts if roles[id(w)] == "race"}
    after = []
    for day in sorted(race_days):
        nxt = day + dt.timedelta(days=1)
        after += [
            f"{w.date.isoformat()} {w.name}"
            for w in workouts
            if w.date == nxt and roles[id(w)] in ("quality", "long", "medium-long")
        ]
    if after:
        report.add("post-race-hard", "warn", "A hard or long session the day after a race", after)
    a = plan.a_race
    if a:
        quiet = [
            f"{w.date.isoformat()} {w.name}"
            for w in workouts
            if 0 < (a.date - w.date).days <= PRE_RACE_QUIET_DAYS
            and roles[id(w)] == "quality"
            and summaries[id(w)]["hard_seconds"] > QUALITY_MINUTES * 60
        ]
        if quiet:
            report.add(
                "pre-race-hard",
                "warn",
                f"A real quality session inside the last {PRE_RACE_QUIET_DAYS} days before the A race; keep race week to short sharpeners",
                quiet,
            )


def _is_marathon(distance: str) -> bool:
    lowered = (distance or "").lower()
    return any(x in lowered for x in MARATHON_WORDS) and not any(x in lowered for x in HALF_WORDS)


def _goal_distance(plan: Plan, profile: Profile) -> str:
    goal = plan.a_race
    if goal and goal.distance:
        return goal.distance
    return profile.goal_race.distance if profile.goal_race and profile.goal_race.distance else ""


def _check_goal_race(
    report: Report,
    plan: Plan,
    profile: Profile,
    workouts: list[Workout],
    summaries: dict,
    roles: dict,
) -> None:
    goal_date = (
        plan.a_race.date if plan.a_race else (profile.goal_race.date if profile.goal_race else None)
    )
    if goal_date is None:
        return
    name = plan.a_race.name if plan.a_race else profile.goal_race.name
    if goal_date >= workouts[0].date and not any(
        w.date == goal_date and roles[id(w)] == "race" for w in workouts
    ):
        report.add(
            "no-race-day",
            "info",
            f"No race-day session for {name}; a race workout with pacing targets can be pushed like any other",
            [goal_date.isoformat()],
        )
    if _is_marathon(_goal_distance(plan, profile)):
        longest = max(
            (summaries[id(w)]["metres"] for w in workouts if w.sport == "running"), default=0.0
        )
        if 0 < longest < MARATHON_LONG_RUN_METRES:
            report.add(
                "marathon-long-run",
                "info",
                f"Marathon goal but the longest run is {longest / 1000:.0f} km; most plans reach 30-35 km",
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
    model = profile.intensity_distribution
    limit = 0.35 if model == "polarized" else HARD_SHARE_LIMIT
    if share > limit:
        report.add(
            "hard-share",
            "warn",
            f"{share:.0%} of running time is at threshold or harder; mostly-easy is the well-supported pattern (aim under ~{limit:.0%})",
        )
    if model == "singles" and share > 0.10:
        report.add(
            "singles-too-hard",
            "warn",
            f"The athlete trains sub-threshold singles, but {share:.0%} of running is at threshold or harder; keep the work at steady to marathon pace",
        )
    # Middle gear: any steady/marathon-intensity time at all?
    from .timeline import ZONE_INTENSITY, workout_timeline

    moderate = 0.0
    for w in workouts:
        for b in workout_timeline(w, profile):
            if ZONE_INTENSITY["steady"] - 0.05 <= b["intensity"] < ZONE_INTENSITY["threshold"]:
                moderate += b["seconds"]
    if model != "polarized" and len(weeks) >= 2 and share >= MIDDLE_GEAR_MIN_HARD and moderate == 0:
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


def _check_long_run_share(
    report: Report, weeks: list[WeekStats], plan: Plan, profile: Profile
) -> None:
    limit = (
        LONG_RUN_SHARE_MARATHON
        if _is_marathon(_goal_distance(plan, profile))
        else LONG_RUN_SHARE_OTHER
    )
    hits = [
        f"week of {w.start.isoformat()} ({w.longest_run_metres / w.metres:.0%})"
        for w in weeks
        if w.sessions >= 3 and w.metres > 0 and w.longest_run_metres / w.metres > limit
    ]
    if hits:
        report.add(
            "long-run-share",
            "warn",
            f"The long run is more than {limit:.0%} of the week's volume -- one huge run and little else; spread the load across the week",
            hits,
        )


def _check_two_long_runs(report: Report, workouts: list[Workout], roles: dict) -> None:
    by_week: dict[dt.date, int] = {}
    for w in workouts:
        if roles[id(w)] == "long":
            by_week[week_start(w.date)] = by_week.get(week_start(w.date), 0) + 1
    hits = [f"week of {d.isoformat()}" for d, n in sorted(by_week.items()) if n >= 2]
    if hits:
        report.add("two-long-runs", "warn", "Two long runs in one week", hits)


def _check_strength_placement(report: Report, workouts: list[Workout], roles: dict) -> None:
    by_date: dict[dt.date, list[Workout]] = {}
    for w in workouts:
        by_date.setdefault(w.date, []).append(w)
    hits = []
    for w in workouts:
        if w.sport != "strength" and roles[id(w)] != "strength":
            continue
        nxt = by_date.get(w.date + dt.timedelta(days=1), [])
        if any(roles[id(o)] in ("quality", "long", "medium-long") for o in nxt):
            hits.append(f"{w.date.isoformat()} {w.name}")
    if hits:
        report.add(
            "strength-before-hard",
            "info",
            "Strength the day before a quality or long session; heavy legs blunt the key run -- put it after, or on an easy day",
            hits,
        )


def _check_rest_streak(report: Report, workouts: list[Workout]) -> None:
    days = sorted({w.date for w in workouts})
    streak, start, hits = 1, days[0], []
    for prev, day in pairwise(days):
        if (day - prev).days == 1:
            streak += 1
        else:
            if streak >= REST_STREAK_DAYS:
                hits.append(f"{start.isoformat()} to {prev.isoformat()} ({streak} days)")
            streak, start = 1, day
    if streak >= REST_STREAK_DAYS:
        hits.append(f"{start.isoformat()} to {days[-1].isoformat()} ({streak} days)")
    if hits:
        report.add(
            "no-rest-streak",
            "warn",
            f"{REST_STREAK_DAYS} or more consecutive days with a session and no day off",
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
    if _is_marathon(_goal_distance(plan, profile)):
        build = [w for w in weeks if "build" in w.phases and not w.medium_long]
        if build:
            report.add(
                "no-medium-long",
                "info",
                "Marathon build weeks without a midweek medium-long run (Pfitzinger's ~90-120 min)",
                [w.start.isoformat() for w in build],
            )
    rest_free = [w for w in weeks if len(w.daily_load) >= 7]
    if rest_free:
        report.add(
            "no-rest-day",
            "warn",
            "Weeks with no rest day at all",
            [w.start.isoformat() for w in rest_free],
        )


def check(plan: Plan, profile: Profile, today: dt.date | None = None) -> Report:
    """Public entry point. Pass `today` to get the date checks (the tests do not)."""
    return check_plan(plan, profile, today)


def week_of(day: dt.date) -> dt.date:
    return week_start(day)
