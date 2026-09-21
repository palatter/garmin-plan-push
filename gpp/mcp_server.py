"""An MCP server, so any assistant can drive the tool conversationally.

`gpp mcp` exposes the pipeline as tools over stdio: check a plan, preview it,
write a one-line workout, run the sanity report, adapt it, and push it. That
means Claude, ChatGPT, Gemini or a local model can *be* the interface --
"push my Thursday session to the watch" -- without this project having to
integrate each of them.

Nothing here can read the Garmin password: pushing requires the same
environment variables the CLI uses (GARMIN_EMAIL / GARMIN_PASSWORD), or a
token already cached by a previous login. An assistant never sees either.

Requires the optional `mcp` extra:  uv sync --extra mcp
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from . import adapt, checks, oneline
from .compile import compile_plan
from .load import plan_dashboard
from .plan import Plan, PlanError
from .profile import Profile, ProfileError, find_profile
from .render import render_plan


def _profile() -> Profile:
    found = find_profile()
    if found is None:
        raise ProfileError("no profile yet; run `gpp web` or `gpp init` first")
    return Profile.load(found)


def _plan(plan_json: str | dict) -> Plan:
    data = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    return Plan.from_dict(data)


# --- tool implementations (plain functions, so they are testable) -----------


def tool_check(plan_json: str) -> dict[str, Any]:
    """Validate a plan and return the sanity report and dashboard."""
    profile = _profile()
    plan = _plan(plan_json)
    compile_plan(plan, profile)
    return {
        "ok": True,
        "report": checks.check(plan, profile).to_dict(),
        "dashboard": plan_dashboard(plan, profile),
        "text": render_plan(plan, compile_plan(plan, profile), profile),
    }


def tool_oneline(text: str, date: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Turn a sentence like "20min warmup, 6x3m @ T w/ 2m jog, 10min cooldown" into a workout."""
    workout = oneline.parse_workout(text, name=name, date=date or dt.date.today().isoformat())
    return {"workout": workout.to_dict()}


def tool_zones() -> dict[str, Any]:
    """The athlete's resolved pace and heart-rate zones."""
    profile = _profile()
    from .units import format_pace

    return {
        "profile": profile.name,
        "zones": {
            name: {
                "slow": format_pace(slow, profile.imperial),
                "fast": format_pace(fast, profile.imperial),
            }
            for name, (slow, fast) in profile.zone_table().items()
        },
        "description": profile.describe(),
    }


def tool_prompt() -> dict[str, Any]:
    """The full generation prompt, for an assistant that wants to write the plan itself."""
    from .prompt import build_prompt

    return {"prompt": build_prompt(_profile())}


def tool_pause(plan_json: str, start: str, days: int, reason: str = "break") -> dict[str, Any]:
    """Shift a plan for an illness or holiday and scale the return week."""
    result = adapt.pause_plan(_plan(plan_json), dt.date.fromisoformat(start), days, reason)
    return result.to_dict()


def tool_missed(plan_json: str, dates: list[str]) -> dict[str, Any]:
    """Replan around missed sessions, with a reason for every change."""
    result = adapt.replan_missed(
        _plan(plan_json), [dt.date.fromisoformat(d) for d in dates], _profile()
    )
    return result.to_dict()


def tool_push(plan_json: str, dry_run: bool = True) -> dict[str, Any]:
    """Push a plan to Garmin Connect. dry_run=True (the default) only renders it.

    A real push needs GARMIN_EMAIL and GARMIN_PASSWORD in the environment,
    or a cached login; the assistant never handles them.
    """
    profile = _profile()
    plan = _plan(plan_json)
    compiled = compile_plan(plan, profile)
    if dry_run:
        return {
            "dry_run": True,
            "workouts": len(compiled),
            "text": render_plan(plan, compiled, profile),
        }
    from .client import GarminClient, PushError

    email = os.environ.get("GARMIN_EMAIL")
    if not email:
        raise PushError(
            "GARMIN_EMAIL is not set; a real push needs it (and GARMIN_PASSWORD or a cached login)"
        )
    client = GarminClient(email, os.environ.get("GARMIN_PASSWORD") or None)
    client.connect()
    results = client.push(compiled)
    return {"dry_run": False, "results": [r.to_dict() for r in results]}


TOOLS = {
    "check_plan": tool_check,
    "oneline_workout": tool_oneline,
    "zones": tool_zones,
    "generation_prompt": tool_prompt,
    "pause_plan": tool_pause,
    "replan_missed": tool_missed,
    "push_plan": tool_push,
}


# --- the server -------------------------------------------------------------


def build_server():
    """Construct the FastMCP server; imported lazily so the extra is optional."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - env dependent
        raise ProfileError("the MCP server needs the `mcp` package: uv sync --extra mcp") from exc

    server = FastMCP("garmin-plan-push")
    for name, fn in TOOLS.items():
        server.tool(name=name, description=(fn.__doc__ or "").strip())(fn)
    return server


def serve() -> None:
    build_server().run()


def describe_tools() -> list[dict]:
    """For `gpp mcp --list` and the tests: what an assistant will see."""
    return [
        {"name": n, "description": (f.__doc__ or "").strip().splitlines()[0]}
        for n, f in TOOLS.items()
    ]


__all__ = ["TOOLS", "PlanError", "build_server", "describe_tools", "serve"]
