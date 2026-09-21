"""Ask a model for a plan, then hold it to the contract.

The loop is the point. A single-shot LLM call produces a valid-looking plan
most of the time; "most of the time" is not good enough when the output ends
up as beeping instructions on your wrist. So every response is validated AND
compiled before it is accepted, and any failure is handed straight back to the
model as a correction turn.

Compiling during validation matters: schema validation catches a malformed
step, but only compilation catches a zone name that does not exist in your
profile, or a pace range that resolves backwards.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from .compile import CompileError, compile_plan
from .plan import PLAN_SCHEMA, Plan, PlanError
from .profile import Profile, ProfileError
from .prompt import build_prompt
from .providers import Provider, ProviderError, extract_json

MAX_ATTEMPTS = 3

RETRY_TEMPLATE = """\
That plan was rejected by the validator:

    {error}

Fix only that problem and return the complete corrected JSON object. Same
format as before: one JSON object, no prose, no markdown fence.
"""


@dataclass
class GenerationResult:
    data: dict
    plan: Plan
    attempts: int
    corrections: list[str]


def generate_plan(
    provider: Provider,
    profile: Profile,
    request: str,
    attempts: int = MAX_ATTEMPTS,
    log: Callable[[str], None] = lambda _: None,
) -> GenerationResult:
    system = build_prompt(profile)
    user = request
    corrections: list[str] = []
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        log(f"asking {provider.name} (attempt {attempt}/{attempts})...")
        raw = provider.complete(system, user, PLAN_SCHEMA)

        try:
            data = extract_json(raw)
            plan = Plan.from_dict(data)
            compile_plan(plan, profile)  # surfaces zone + pace errors
        except (ProviderError, PlanError, CompileError, ProfileError) as exc:
            last_error = exc
            corrections.append(str(exc))
            log(f"  rejected: {exc}")
            if attempt == attempts:
                break
            user = _correction_turn(request, raw, exc)
            continue

        log(f"  accepted: {len(plan.workouts)} workout(s)")
        return GenerationResult(data, plan, attempt, corrections)

    raise ProviderError(
        f"{provider.name} could not produce a valid plan in {attempts} attempts. "
        f"Last error: {last_error}"
    )


def _correction_turn(original_request: str, raw: str, error: Exception) -> str:
    return (
        f"{original_request}\n\nYour previous answer was:\n{raw.strip()[:4000]}\n\n"
        + RETRY_TEMPLATE.format(error=error)
    )


def dump_plan(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"
