"""Chunked generation for long plans: an outline, then one call per phase.

A 20-week plan is a few hundred sessions of JSON. Asked for in one go it
overflows the output limit or gets the model's attention spread thin. Asked
for as an outline (phases, week counts, weekly volume targets) and then one
call per phase -- each carrying the outline and a summary of the previous
phase -- every phase gets the full context and the full attention, and no
single answer is longer than a few weeks.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable

from . import checks
from .compile import CompileError, compile_plan
from .generate import RETRY_TEMPLATE, GenerationResult
from .load import weekly_stats
from .plan import PHASES, PLAN_SCHEMA, Plan, PlanError, Week
from .profile import Profile, ProfileError
from .prompt import build_prompt
from .providers import Provider, ProviderError, extract_json, was_truncated

OUTLINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "minLength": 1, "maxLength": 80},
        "summary": {"type": "string", "maxLength": 2000},
        "race_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "races": PLAN_SCHEMA["properties"]["races"],
        "phases": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 60},
                    "phase": {"enum": list(PHASES)},
                    "start": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                    "weeks": {"type": "integer", "minimum": 1, "maximum": 8},
                    "weekly_km": {"type": "array", "items": {"type": "number"}, "maxItems": 8},
                    "focus": {"type": "string", "maxLength": 300},
                },
                "required": ["name", "phase", "start", "weeks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["plan", "phases"],
    "additionalProperties": False,
}

OUTLINE_REQUEST = """\
OUTLINE ONLY. Do not write workouts yet. Return one JSON object with:
"plan" (a name), "summary" (3-5 sentences on the block's logic), optional
"race_date" and "races", and "phases": an ordered list of mesocycles, each with
"name", "phase" (base, build, peak, taper or recovery), "start" (a Monday,
ISO date), "weeks" (1-8) and "weekly_km" (one number per week). Phases must
be contiguous and cover the whole block; the taper is its own phase. The
athlete's request:

{request}
"""

PHASE_REQUEST = """\
Write the workouts for ONE phase of a plan whose outline is:
{outline}

{previous}
THIS CALL: phase {index} of {total} -- "{name}" ({phase}), {weeks} week(s)
starting Monday {start}, ending Sunday {end}. Weekly volume targets: {targets}.
Return a JSON object with "plan" set to "{plan}" and "workouts" containing
only the sessions of these {weeks} week(s), dated {start} to {end}. No other
top-level keys. The athlete's request, for context:

{request}
"""


def _phase_end(phase: dict) -> dt.date:
    return dt.date.fromisoformat(phase["start"]) + dt.timedelta(days=7 * phase["weeks"] - 1)


def _validate_outline(data: dict, today: dt.date) -> list[dict]:
    import jsonschema

    errors = sorted(
        jsonschema.Draft202012Validator(OUTLINE_SCHEMA).iter_errors(data),
        key=lambda e: list(e.path),
    )
    if errors:
        raise PlanError(f"outline schema error: {errors[0].message}")
    phases = data["phases"]
    for index, phase in enumerate(phases):
        start = dt.date.fromisoformat(phase["start"])
        if start.weekday() != 0:
            raise PlanError(
                f"phase {phase['name']!r} must start on a Monday, not {start.strftime('%A')}"
            )
        if index and start != _phase_end(phases[index - 1]) + dt.timedelta(days=1):
            raise PlanError(
                f"phase {phase['name']!r} does not start the day after the previous phase ends"
            )
    if dt.date.fromisoformat(phases[0]["start"]) < today - dt.timedelta(days=6):
        raise PlanError("the first phase starts in the past")
    return phases


def _previous_summary(workouts: list, profile: Profile) -> str:
    if not workouts:
        return ""
    stats = weekly_stats(Plan(plan="_", workouts=workouts), profile)[-1]
    return (
        "PREVIOUS PHASE, last week: "
        f"{stats.km:.0f} km over {stats.sessions} sessions, longest run "
        f"{stats.longest_run_metres / 1000:.0f} km, {stats.hard_share:.0%} hard. Continue from there.\n"
    )


def _ask(
    provider: Provider,
    system: str,
    user: str,
    schema: dict,
    accept: Callable[[dict], object],
    attempts: int,
    log: Callable[[str], None],
) -> tuple[object, str, list[str]]:
    """Ask, validate with `accept`, feed errors back; the generic inner loop."""
    history: list[dict[str, str]] = []
    corrections: list[str] = []
    for attempt in range(1, attempts + 1):
        raw = provider.complete(system, user, schema, history=history or None)
        try:
            if was_truncated(provider):
                raise ProviderError("the answer was cut off; this phase is too long for one call")
            data = extract_json(raw)
            return accept(data), raw, corrections
        except (ProviderError, PlanError, CompileError, ProfileError) as exc:
            corrections.append(str(exc))
            log(f"  rejected: {exc}")
            if attempt == attempts:
                raise
            history += [{"role": "user", "content": user}, {"role": "assistant", "content": raw}]
            user = RETRY_TEMPLATE.format(error=exc)
    raise ProviderError("unreachable")  # pragma: no cover


def generate_plan_chunked(
    provider: Provider,
    profile: Profile,
    request: str,
    attempts: int = 3,
    log: Callable[[str], None] = lambda _: None,
    today: dt.date | None = None,
    previous: Plan | None = None,
) -> GenerationResult:
    today = today or dt.date.today()
    system = build_prompt(profile, today=today, previous=previous)
    corrections: list[str] = []

    log(f"asking {provider.name} for an outline...")
    outline, _, fixes = _ask(
        provider,
        system,
        OUTLINE_REQUEST.format(request=request),
        OUTLINE_SCHEMA,
        lambda data: (_validate_outline(data, today), data)[1],
        attempts,
        log,
    )
    corrections += fixes
    phases = outline["phases"]
    log(
        f"  outline: {len(phases)} phase(s) -- "
        + ", ".join(f"{p['name']} ({p['weeks']}w)" for p in phases)
    )

    workouts = []
    outline_text = json.dumps({k: v for k, v in outline.items() if k != "summary"}, indent=1)
    for index, phase in enumerate(phases, start=1):
        start, end = dt.date.fromisoformat(phase["start"]), _phase_end(phase)
        targets = (
            ", ".join(f"{km:.0f} km" for km in phase.get("weekly_km") or [])
            or "as the outline implies"
        )
        user = PHASE_REQUEST.format(
            outline=outline_text,
            previous=_previous_summary(workouts, profile),
            index=index,
            total=len(phases),
            name=phase["name"],
            phase=phase["phase"],
            weeks=phase["weeks"],
            start=start.isoformat(),
            end=end.isoformat(),
            targets=targets,
            plan=outline["plan"],
            request=request,
        )

        def accept(data: dict, start=start, end=end) -> Plan:
            chunk = Plan.from_dict({"plan": outline["plan"], "workouts": data.get("workouts", [])})
            outside = [w for w in chunk.workouts if not (start <= w.date <= end)]
            if outside:
                raise PlanError(
                    f"{len(outside)} session(s) fall outside {start} to {end}, e.g. "
                    f"{outside[0].date} {outside[0].name}"
                )
            compile_plan(chunk, profile)
            return chunk

        log(f"asking {provider.name} for phase {index}/{len(phases)}: {phase['name']}...")
        chunk, _, fixes = _ask(provider, system, user, PLAN_SCHEMA, accept, attempts, log)
        corrections += fixes
        workouts += chunk.workouts

    plan = Plan(
        plan=outline["plan"],
        workouts=sorted(workouts, key=lambda w: (w.date, w.name)),
        race_date=dt.date.fromisoformat(outline["race_date"]) if outline.get("race_date") else None,
        races=[
            __import__("gpp.plan", fromlist=["Race"]).Race.from_dict(r)
            for r in outline.get("races", [])
        ],
        weeks=[
            Week(start=dt.date.fromisoformat(p["start"]), phase=p["phase"], note=p.get("focus"))
            for p in phases
        ],
        summary=outline.get("summary"),
    )
    data = plan.to_dict()
    Plan.from_dict(data)  # the merged plan must validate as one document
    report = checks.check(plan, profile, today=today)
    if report.blocks:
        log("  blocked by sanity checks: " + "; ".join(f.code for f in report.blocks))
        plan, report, fixes = _repair(
            provider, profile, system, plan, phases, report, attempts, log, today
        )
        corrections += fixes
    else:
        log(f"  accepted: {len(plan.workouts)} workout(s) across {len(phases)} phase(s)")
    return GenerationResult(plan.to_dict(), plan, len(phases) + 1, corrections, report)


_DATE_IN_TEXT = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _repair(provider, profile, system, plan, phases, report, attempts, log, today):
    """Regenerate only the phases that a blocking finding points at, once."""
    dates = {
        dt.date.fromisoformat(m)
        for f in report.blocks
        for text in f.dates
        for m in _DATE_IN_TEXT.findall(text)
    }
    hit = [
        p
        for p in phases
        if any(dt.date.fromisoformat(p["start"]) <= d <= _phase_end(p) for d in dates)
    ]
    if not hit:
        return plan, report, []
    fixes: list[str] = []
    workouts = list(plan.workouts)
    outline_text = json.dumps([dict(p) for p in phases], indent=1)
    for phase in hit:
        start, end = dt.date.fromisoformat(phase["start"]), _phase_end(phase)
        before = [w for w in workouts if w.date < start]
        user = PHASE_REQUEST.format(
            outline=outline_text,
            previous=_previous_summary(before, profile),
            index=phases.index(phase) + 1,
            total=len(phases),
            name=phase["name"],
            phase=phase["phase"],
            weeks=phase["weeks"],
            start=start.isoformat(),
            end=end.isoformat(),
            targets="as before",
            plan=plan.plan,
            request="Rewrite this phase so that these problems go away:\n"
            + report.feedback(include_warns=False),
        )

        def accept(data: dict, start=start, end=end) -> Plan:
            chunk = Plan.from_dict({"plan": plan.plan, "workouts": data.get("workouts", [])})
            if any(not (start <= w.date <= end) for w in chunk.workouts):
                raise PlanError(f"sessions fall outside {start} to {end}")
            compile_plan(chunk, profile)
            return chunk

        log(f"re-asking for phase {phase['name']} to clear blocking findings...")
        try:
            chunk, _, more = _ask(provider, system, user, PLAN_SCHEMA, accept, attempts, log)
        except (ProviderError, PlanError, CompileError, ProfileError) as exc:
            fixes.append(str(exc))
            continue
        fixes += more
        workouts = [w for w in workouts if not (start <= w.date <= end)] + chunk.workouts
    repaired = Plan(
        plan=plan.plan,
        workouts=sorted(workouts, key=lambda w: (w.date, w.name)),
        race_date=plan.race_date,
        races=plan.races,
        weeks=plan.weeks,
        summary=plan.summary,
    )
    return repaired, checks.check(repaired, profile, today=today), fixes
