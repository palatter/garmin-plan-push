"""A plan-quality harness (`gpp eval`) and a local-model bench (`gpp providers --bench`).

The rubric is deliberately a set of discrete, mechanical checks -- the sanity
report's codes, structural numbers, correction turns, tokens and time --
rather than one holistic score. LLM-as-judge work keeps finding that
holistic rubrics drift and prefer their own style; counting what the checks
found does neither.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from .generate import generate_plan
from .load import plan_dashboard
from .profile import Profile
from .providers import Provider, ProviderError

# Three athletes the BMB study says AI plans fail to tell apart, plus the
# short case the bench uses. Profiles are complete enough for every check.
DEFAULT_CASES: list[dict] = [
    {
        "name": "beginner-10k",
        "profile": {
            "name": "Beginner",
            "pace": {"threshold": "5:45/km"},
            "athlete": {"longest_recent_run_km": 8, "recent_weekly_km": 20},
            "availability": {"days": ["tue", "thu", "sat", "sun"], "sessions_per_week": 4},
            "goal_race": {"name": "Park 10k", "date": "2026-11-15", "distance": "10k"},
        },
        "request": "Eight weeks to my first 10k on 15 November 2026. Four runs a week, nothing scary.",
    },
    {
        "name": "intermediate-half",
        "profile": {
            "name": "Intermediate",
            "pace": {"threshold": "4:30/km"},
            "athlete": {"longest_recent_run_km": 16, "recent_weekly_km": 45},
            "goal_race": {
                "name": "City half",
                "date": "2026-11-22",
                "distance": "half marathon",
                "goal_time": "1:40:00",
            },
        },
        "request": "Nine weeks to a half marathon on 22 November 2026, five runs a week, two quality sessions, a long run on Sundays.",
    },
    {
        "name": "experienced-marathon",
        "profile": {
            "name": "Experienced",
            "pace": {"threshold": "3:50/km"},
            "athlete": {"longest_recent_run_km": 24, "recent_weekly_km": 75},
            "goal_race": {
                "name": "Marathon",
                "date": "2026-12-06",
                "distance": "marathon",
                "goal_time": "2:55:00",
            },
        },
        "request": "Eleven weeks to a marathon on 6 December 2026, six runs a week, Pfitzinger-style with a midweek medium-long run and a proper taper.",
    },
]

BENCH_CASES: list[dict] = [
    {
        "name": "one-week",
        "profile": {"name": "Bench", "pace": {"threshold": "4:30/km"}},
        "request": "One week of running: an easy run Tuesday, a threshold session Thursday, a long run Sunday.",
    },
    {
        "name": "two-weeks-strides",
        "profile": {"name": "Bench", "pace": {"threshold": "5:00/km"}},
        "request": "Two easy weeks, four runs a week, with strides after two of the easy runs.",
    },
]


@dataclass
class CaseResult:
    name: str
    ok: bool
    attempts: int = 0
    corrections: int = 0
    findings: list[str] = field(default_factory=list)
    blocks: int = 0
    warns: int = 0
    sessions: int = 0
    weeks: int = 0
    hard_share: float = 0.0
    peak_week_km: float = 0.0
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    error: str | None = None
    first_try_valid: bool = False


def run_case(
    provider: Provider,
    case: dict,
    attempts: int = 3,
    today: dt.date | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> CaseResult:
    profile = Profile.from_dict(case["profile"])
    started = time.perf_counter()
    try:
        result = generate_plan(
            provider, profile, case["request"], attempts=attempts, log=log, today=today
        )
    except ProviderError as exc:
        return CaseResult(
            case["name"], False, seconds=time.perf_counter() - started, error=str(exc)
        )
    elapsed = time.perf_counter() - started
    dash = plan_dashboard(result.plan, profile)
    usage = result.usage
    return CaseResult(
        name=case["name"],
        ok=result.report.ok,
        attempts=result.attempts,
        corrections=len(result.corrections),
        findings=[f.code for f in result.report.findings],
        blocks=len(result.report.blocks),
        warns=len(result.report.warns),
        sessions=dash["sessions"],
        weeks=len(dash["weeks"]),
        hard_share=dash["hard_share"],
        peak_week_km=dash["peak_week_km"],
        seconds=round(elapsed, 1),
        tokens_in=usage.input_tokens if usage else 0,
        tokens_out=usage.output_tokens if usage else 0,
        cost_usd=usage.cost_usd if usage else None,
        first_try_valid=result.attempts == 1 and not result.corrections,
    )


def evaluate(
    provider_factory: Callable[[], Provider],
    cases: list[dict],
    attempts: int = 3,
    today: dt.date | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> list[CaseResult]:
    """A fresh provider per case, so a raised token limit does not leak between them."""
    results = []
    for case in cases:
        log(f"== {case['name']}")
        results.append(run_case(provider_factory(), case, attempts, today, log))
    return results


def load_cases(path: str | None) -> list[dict]:
    if not path:
        return DEFAULT_CASES
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    cases = data["cases"] if isinstance(data, dict) else data
    for case in cases:
        for key in ("name", "profile", "request"):
            if key not in case:
                raise ValueError(f"each case needs {key!r}")
    return cases


def table(results: list[CaseResult]) -> str:
    head = f"{'case':<24} {'ok':<4} {'try':>3} {'fix':>3} {'blk':>3} {'wrn':>3} {'wks':>3} {'sess':>4} {'hard':>5} {'peak':>6} {'sec':>6} {'tokens':>12} {'cost':>7}"
    lines = [head, "-" * len(head)]
    for r in results:
        cost = f"${r.cost_usd:.2f}" if r.cost_usd is not None else "-"
        lines.append(
            f"{r.name:<24} {'yes' if r.ok else 'no':<4} {r.attempts:>3} {r.corrections:>3} {r.blocks:>3} "
            f"{r.warns:>3} {r.weeks:>3} {r.sessions:>4} {r.hard_share:>5.0%} {r.peak_week_km:>6.1f} "
            f"{r.seconds:>6.1f} {r.tokens_in:>5}/{r.tokens_out:<6} {cost:>7}"
        )
        if r.error:
            lines.append(f"    error: {r.error}")
        elif r.findings:
            lines.append("    findings: " + ", ".join(r.findings))
    passed = sum(1 for r in results if r.ok)
    lines.append(f"{passed}/{len(results)} plans passed the checks")
    return "\n".join(lines)


def verdict(results: list[CaseResult]) -> str:
    """The bench's one-word summary, from what was measured."""
    if not results or all(r.error for r in results):
        return "unusable"
    first_try = sum(1 for r in results if r.first_try_valid) / len(results)
    ok = sum(1 for r in results if r.ok) / len(results)
    if first_try >= 0.5 and ok == 1.0:
        return "good"
    if ok >= 0.5:
        return "usable"
    return "weak"


def to_dict(results: list[CaseResult]) -> dict:
    return {"results": [asdict(r) for r in results], "verdict": verdict(results)}
