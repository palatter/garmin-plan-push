"""Ask a model for a plan, then hold it to the contract.

The loop is the point. A single-shot LLM call produces a valid-looking plan
most of the time; "most of the time" is not good enough when the output ends
up as beeping instructions on your wrist. So every response is validated,
compiled AND sanity-checked before it is accepted, and any failure is handed
straight back to the model as a correction turn.

Three gates, in order of strictness:

  1. schema + semantics (plan.validate)   -> always sent back
  2. compilation (zones, paces, sports)   -> always sent back
  3. the sanity report (checks.check)     -> "block" findings always sent
     back; "warn" findings sent back once, then the athlete decides. A plan
     that is merely warned about is still a valid plan; refusing it forever
     would just make the model thrash.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from . import checks
from .compile import CompileError, compile_plan
from .plan import PLAN_SCHEMA, WORKOUT_SCHEMA, Plan, PlanError, Workout
from .profile import Profile, ProfileError
from .prompt import build_prompt, build_rewrite_prompt
from .providers import (
    MAX_TOKENS_CEILING,
    Provider,
    ProviderError,
    Usage,
    extract_json,
    was_truncated,
)

MAX_ATTEMPTS = 3

RETRY_TEMPLATE = """\
Your previous answer was rejected by the validator:

    {error}

Fix only that problem and return the complete corrected JSON object. Same
format as before: one JSON object, no prose, no markdown fence.
"""

REPORT_TEMPLATE = """\
Your previous plan is valid but the coaching checks flagged it:

{feedback}

Revise the plan to address these and return the complete corrected JSON
object. Same format as before: one JSON object, no prose, no markdown fence.
"""

TRUNCATED_TEMPLATE = "the answer was cut off at {limit} output tokens before the JSON was complete"


@dataclass
class GenerationResult:
    data: dict
    plan: Plan
    attempts: int
    corrections: list[str]
    report: checks.Report = field(default_factory=checks.Report)
    usage: Usage | None = None  # summed over every attempt


def generate_plan(
    provider: Provider,
    profile: Profile,
    request: str,
    attempts: int = MAX_ATTEMPTS,
    log: Callable[[str], None] = lambda _: None,
    today: dt.date | None = None,
    previous: Plan | None = None,
    chunk_weeks: int | None = None,
) -> GenerationResult:
    if chunk_weeks:
        from .chunked import generate_plan_chunked

        return generate_plan_chunked(
            provider, profile, request, attempts=attempts, log=log, today=today, previous=previous
        )
    today = today or dt.date.today()
    system = build_prompt(profile, today=today, previous=previous)
    user = request
    history: list[dict[str, str]] = []
    corrections: list[str] = []
    last_error: Exception | None = None
    warned_once = False
    best: GenerationResult | None = None
    total: Usage | None = None

    for attempt in range(1, attempts + 1):
        log(f"asking {provider.name} (attempt {attempt}/{attempts})...")
        raw = provider.complete(
            system, user, PLAN_SCHEMA, on_delta=_progress(log), history=history or None
        )
        usage = getattr(provider, "last_usage", None)
        if usage:
            log(f"  {usage.describe()}")
            total = usage if total is None else total + usage

        try:
            if was_truncated(provider):
                raise ProviderError(TRUNCATED_TEMPLATE.format(limit=_max_tokens(provider)))
            data = extract_json(raw)
            plan = Plan.from_dict(data)
            compile_plan(plan, profile)  # surfaces zone + pace errors
        except (ProviderError, PlanError, CompileError, ProfileError) as exc:
            last_error = exc
            corrections.append(str(exc))
            log(f"  rejected: {exc}")
            if attempt == attempts:
                break
            if was_truncated(provider):
                _raise_max_tokens(provider, log)
                # A cut-off answer is not worth carrying as history.
                continue
            history += [{"role": "user", "content": user}, {"role": "assistant", "content": raw}]
            user = RETRY_TEMPLATE.format(error=exc)
            continue

        report = checks.check(plan, profile, today=today)
        result = GenerationResult(data, plan, attempt, list(corrections), report, total)
        if report.blocks or (report.warns and not warned_once):
            kind = "blocked" if report.blocks else "warned"
            summary = "; ".join(f.code for f in report.blocks + report.warns)
            log(f"  {kind} by sanity checks: {summary}")
            corrections.append(report.feedback(include_warns=True))
            if not report.blocks:
                warned_once = True
                best = result  # acceptable if the model cannot do better
            if attempt == attempts:
                if best is not None:
                    log("  accepting the best warned plan; the report is attached")
                    return best
                break
            history += [{"role": "user", "content": user}, {"role": "assistant", "content": raw}]
            user = REPORT_TEMPLATE.format(feedback=report.feedback(include_warns=True))
            continue

        log(f"  accepted: {len(plan.workouts)} workout(s)")
        if total is not None and attempt > 1:
            log(f"  total over {attempt} attempts: {total.describe()}")
        return result

    if best is not None:
        log("  accepting the best warned plan; the report is attached")
        return best
    raise ProviderError(
        f"{provider.name} could not produce an acceptable plan in {attempts} attempts. "
        f"Last problem: {last_error or 'sanity checks not satisfied'}"
    )


def regenerate_workout(
    provider: Provider,
    profile: Profile,
    plan: Plan,
    date: str,
    instruction: str,
    attempts: int = 2,
    log: Callable[[str], None] = lambda _: None,
) -> Plan:
    """Rewrite one workout in place ("rewrite just Thursday")."""
    current = [w for w in plan.workouts if w.date.isoformat() == date]
    if not current:
        raise PlanError(f"no workout on {date}")
    target = current[0]
    others = [w.to_dict() for w in plan.workouts if w is not target]
    system = build_prompt(profile)
    user = build_rewrite_prompt(target.to_dict(), others, instruction)
    history: list[dict[str, str]] = []

    for attempt in range(1, attempts + 1):
        log(f"asking {provider.name} to rewrite {date} (attempt {attempt}/{attempts})...")
        raw = provider.complete(system, user, WORKOUT_SCHEMA, history=history or None)
        try:
            data = extract_json(raw)
            if "workouts" in data and len(data["workouts"]) == 1:
                data = data["workouts"][0]
            data.setdefault("date", date)
            candidate = Plan.from_dict({"plan": plan.plan, "workouts": [data]})
            compile_plan(candidate, profile)
        except (ProviderError, PlanError, CompileError, ProfileError) as exc:
            log(f"  rejected: {exc}")
            if attempt == attempts:
                raise
            history += [{"role": "user", "content": user}, {"role": "assistant", "content": raw}]
            user = RETRY_TEMPLATE.format(error=exc)
            continue
        new_workout: Workout = candidate.workouts[0]
        new_workout.date = target.date
        replaced = [new_workout if w is target else w for w in plan.workouts]
        return Plan(
            plan=plan.plan,
            workouts=replaced,
            race_date=plan.race_date,
            races=plan.races,
            weeks=plan.weeks,
        )
    raise ProviderError("could not rewrite the workout")


def _max_tokens(provider: Provider) -> int:
    config = getattr(provider, "config", None)
    return int(getattr(config, "max_tokens", 0) or 0)


def _raise_max_tokens(provider: Provider, log: Callable[[str], None]) -> None:
    """Give a cut-off answer twice the room next time, up to the ceiling."""
    config = getattr(provider, "config", None)
    if config is None or not getattr(config, "max_tokens", None):
        return
    new = min(MAX_TOKENS_CEILING, config.max_tokens * 2)
    if new > config.max_tokens:
        log(f"  raising the output limit {config.max_tokens} -> {new} and trying again")
        config.max_tokens = new


def _progress(log: Callable[[str], None]) -> Callable[[str], None]:
    """Turn a token stream into a few log lines, not thousands."""
    seen = [0, 0]

    def on_delta(text: str) -> None:
        seen[0] += len(text)
        if seen[0] - seen[1] >= 800:
            seen[1] = seen[0]
            log(f"  writing... {seen[0] / 1000:.1f}k characters")

    return on_delta


def dump_plan(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"
