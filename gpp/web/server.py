"""The local web UI's HTTP server.

Deliberately stdlib-only. This is a single-user app on loopback with a handful
of endpoints; adding a web framework would buy nothing and cost everyone an
extra install that can break.

Security posture, because this endpoint can be handed a Garmin password:

  * binds 127.0.0.1, never 0.0.0.0;
  * every /api/ call must carry a per-run token that is injected into the page
    at load, so a page on some other origin cannot drive it -- it can issue a
    cross-origin POST, but it cannot read the token to sign one;
  * the Host header is checked, which is what actually stops DNS rebinding
    (an attacker's DNS can point at 127.0.0.1, but the Host will not be ours);
  * the password is used for one login and never written to disk. Garmin's own
    OAuth token cache is the only thing that persists.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import secrets
import threading
import webbrowser
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import adapt, checks, library, oneline
from ..agenda import week_view
from ..compile import compile_plan
from ..diff import diff_plans
from ..education import ENTRIES as EDUCATION
from ..environment import combined_band, dew_point_c, heat_slowdown_spk
from ..estimate import EstimateError, lthr_from_max, threshold_from_race
from ..formats import FormatError, export_ics, export_share, export_workout
from ..generate import generate_plan, regenerate_workout
from ..history import DB_PATH, History
from ..load import plan_dashboard, session_load
from ..loadfocus import load_focus
from ..plan import Plan, PlanError
from ..profile import Profile, ProfileError, default_save_path, find_profile
from ..providers import (
    ProviderError,
    build_provider,
    is_manual,
    key_present,
    load_providers,
    pick_default,
    resolve,
)
from ..receipts import save_receipt
from ..recent import list_recent, load_recent, save_recent
from ..render import render_plan
from ..timeline import ZONE_INTENSITY, workout_summary, workout_timeline
from ..timeline import workout_summary as _summary
from ..units import format_duration, format_pace
from .jobs import JobRegistry

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
MAX_BODY_BYTES = 1_000_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
}


class AppError(Exception):
    """A user-facing error with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class App:
    """Everything the handlers need, with no global state."""

    def __init__(self, profile_path: Path | None):
        self.token = secrets.token_urlsafe(32)
        self.jobs = JobRegistry()
        self.profile_path = profile_path
        self.library_root: Path | None = None  # tests point this somewhere disposable
        self.recent_root: Path | None = None
        self.history_path: Path | None = None
        self._lock = threading.Lock()

    # --- profile ---

    def profile(self) -> Profile:
        path = self.profile_path or find_profile()
        if path is None:
            raise AppError("no profile yet", status=409)
        return Profile.load(path)

    def try_profile(self) -> Profile | None:
        try:
            return self.profile()
        except (AppError, ProfileError):
            return None

    # --- endpoints ---

    def state(self, _: dict) -> dict:
        profile = self.try_profile()
        if profile is None:
            return {"configured": False}

        configs, default = load_providers(profile.raw)
        return {
            "configured": True,
            "profile": {
                "name": profile.name,
                "imperial": profile.imperial,
                "threshold": format_pace(profile.threshold_pace, profile.imperial),
                "lthr": profile.lthr,
                "hr_max": profile.hr_max,
                "path": str(self.profile_path or find_profile()),
            },
            "zones": _zones_of(profile),
            "athlete": _athlete_of(profile),
            "providers": [
                {
                    "name": name,
                    "kind": config.kind,
                    "model": resolve(config).model,
                    "key_env": config.api_key_env,
                    # True/False when a key is needed, None when it is not --
                    # the UI uses this to warn before a doomed generate.
                    "key_present": key_present(config),
                }
                for name, config in configs.items()
            ],
            "default_provider": pick_default(configs, default),
            "education": EDUCATION,
            "recent": [r.to_dict() for r in list_recent(self.recent_root)[:8]],
        }

    def zones_preview(self, body: dict) -> dict:
        """Resolve zones for a threshold the user has not saved yet.

        The setup screen shows live zones as you type, and they must be the
        same numbers the compiler will use -- so they are derived here rather
        than duplicated in JavaScript.
        """
        imperial = bool(body.get("imperial"))
        threshold = (body.get("threshold") or "").strip()
        if not threshold:
            raise AppError("no threshold pace given")
        try:
            profile = Profile.from_dict(
                {"units": "imperial" if imperial else "metric", "pace": {"threshold": threshold}}
            )
        except ProfileError as exc:
            raise AppError(str(exc)) from exc
        return {"zones": _zones_of(profile)}

    def estimate(self, body: dict) -> dict:
        try:
            result = threshold_from_race(body.get("distance", ""), body.get("time", ""))
        except EstimateError as exc:
            raise AppError(str(exc)) from exc
        imperial = bool(body.get("imperial"))
        return {
            "threshold": format_pace(result.threshold_pace, imperial),
            "threshold_seconds": result.threshold_pace,
            "hour_distance_km": round(result.hour_distance_metres / 1000, 2),
            "reliable": result.reliable,
            "note": result.note,
        }

    def estimate_lthr(self, body: dict) -> dict:
        try:
            return {"lthr": lthr_from_max(int(body.get("hr_max", 0)))}
        except (EstimateError, ValueError) as exc:
            raise AppError(str(exc)) from exc

    def save_profile(self, body: dict) -> dict:
        """Write the form back as TOML, keeping every section the form does not own.

        Only keys present in the body are touched, so the setup screen can
        save paces without knowing about the athlete block and vice versa.
        """
        existing = self.try_profile()
        raw = deepcopy(existing.raw) if existing else {}
        data: dict[str, Any] = dict(raw)
        if "name" in body or "name" not in data:
            data["name"] = (body.get("name") or data.get("name") or "athlete").strip() or "athlete"
        if "imperial" in body:
            data["units"] = "imperial" if body.get("imperial") else "metric"
        pace = dict(raw.get("pace") or {})
        if body.get("threshold"):
            pace["threshold"] = body["threshold"]
        pace.setdefault("threshold", "5:00/km")
        data["pace"] = pace

        hr = dict(raw.get("hr") or {})
        for key, name in (("lthr", "lthr"), ("hr_max", "max")):
            if key in body:
                if body[key]:
                    hr[name] = int(body[key])
                else:
                    hr.pop(name, None)
        _put(data, "hr", hr)
        if body.get("providers"):
            data["ai"] = body["providers"]

        athlete = dict(raw.get("athlete") or {})
        for key in ("injuries", "constraints"):
            if key in body:
                athlete[key] = _lines(body[key])
        if "instructions" in body:
            athlete["instructions"] = str(body["instructions"] or "").strip()
        for key in ("longest_recent_run_km", "recent_weekly_km"):
            if key in body:
                athlete[key] = _number(body[key], key)
        _put(data, "athlete", {k: v for k, v in athlete.items() if v not in (None, "", [])})
        if "availability" in body:
            av = body.get("availability") or {}
            _put(data, "availability", {k: v for k, v in av.items() if v not in (None, "", [], 0)})
        if "goal_race" in body:
            goal = body.get("goal_race") or {}
            _put(
                data, "goal_race", {k: v for k, v in goal.items() if v} if goal.get("date") else {}
            )

        try:
            profile = Profile.from_dict(data)
        except ProfileError as exc:
            raise AppError(str(exc)) from exc

        path = self.profile_path or find_profile() or default_save_path()
        profile.save(path)
        with self._lock:
            self.profile_path = path
        return {"saved": str(path), "zones": self.state({})["zones"]}

    def preview(self, body: dict) -> dict:
        profile = self.profile()
        raw = body.get("plan")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AppError(f"that is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise AppError("no plan supplied")
        try:
            return self._describe(Plan.from_dict(raw), profile, remember="edited")
        except (PlanError, ProfileError) as exc:
            raise AppError(str(exc)) from exc

    def _describe(self, plan: Plan, profile: Profile, remember: str | None = None) -> dict:
        compiled = compile_plan(plan, profile)
        if remember:
            # Remembering is a convenience, never the reason a request fails.
            with contextlib.suppress(OSError):
                save_recent(plan.to_dict(), remember, root=self.recent_root)
        workouts = []
        for workout, item in zip(plan.workouts, compiled, strict=True):
            summary = workout_summary(workout, profile)
            workouts.append(
                {
                    "index": len(workouts),
                    "name": workout.name,
                    "date": workout.date.isoformat(),
                    "notes": workout.notes,
                    "sport": workout.sport,
                    "role": workout.role,
                    "phase": workout.phase,
                    "estimate": format_duration(item.estimated_seconds),
                    "seconds": item.estimated_seconds,
                    "summary": summary,
                    "hard_seconds": summary["hard_seconds"],
                    "load": round(session_load(workout, profile)),
                    "timeline": workout_timeline(workout, profile),
                    "tag": item.tag,
                }
            )
        return {
            "plan": plan.plan,
            "workouts": workouts,
            "text": render_plan(plan, compiled, profile),
            "json": plan.to_dict(),
            "race": plan.a_race.to_dict() if plan.a_race else None,
            "report": checks.check(plan, profile).to_dict(),
            "dashboard": plan_dashboard(plan, profile),
            "load_focus": load_focus(plan, profile),
            "agenda": week_view(plan, profile),
        }

    def generate(self, body: dict) -> dict:
        profile = self.profile()
        configs, default = load_providers(profile.raw)
        name = body.get("provider") or pick_default(configs, default)
        if name not in configs:
            raise AppError(f"unknown provider {name!r}")
        request = (body.get("request") or "").strip()
        if not request:
            raise AppError("describe the training you want first")
        attempts = max(1, min(5, int(body.get("attempts", 3))))
        config = configs[name]

        def work(job) -> dict:
            # A manual provider has no API to call: the user relays the prompt
            # to whatever chat window they like and pastes the answer back.
            ask = None
            if is_manual(config):

                def ask(prompt_text: str) -> str:
                    job.say("waiting for you to paste a plan back")
                    return job.ask(
                        "Paste the assistant's reply",
                        multiline=True,
                        relay=prompt_text,
                    )

            provider = build_provider(config, ask=ask)
            result = generate_plan(provider, profile, request, attempts=attempts, log=job.say)
            return self._describe(result.plan, profile, remember="generated")

        return {"job": self.jobs.start("generate", work).id}

    def push(self, body: dict) -> dict:
        from ..client import GarminClient

        profile = self.profile()
        raw = body.get("plan")
        if not isinstance(raw, dict):
            raise AppError("no plan to push")
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email:
            raise AppError("Garmin Connect email is required")

        try:
            plan = Plan.from_dict(raw)
        except PlanError as exc:
            raise AppError(str(exc)) from exc
        compiled = compile_plan(plan, profile)
        replace = bool(body.get("replace", True))

        def work(job) -> dict:
            job.say(f"signing in as {email}")
            client = GarminClient(email, password or None)
            client.connect(prompt_mfa=lambda: job.ask("Garmin MFA code"))
            job.say(f"connected via {client.transport}")
            for other in client.conflicts(compiled):
                job.say(
                    f"  also on {other['date']}: {other['title']} ({other['source']}) -- left alone"
                )
            results = client.push(compiled, replace=replace, verify=True, log=job.say)
            try:
                job.say(f"receipt saved: {save_receipt(plan.plan, results).name}")
            except OSError as exc:  # a receipt is a convenience, never the reason a push fails
                job.say(f"could not save the push receipt: {exc}")
            return {
                "results": [
                    {
                        "name": r.name,
                        "date": r.date,
                        "action": r.action,
                        "workout_id": r.workout_id,
                        "detail": r.detail,
                    }
                    for r in results
                ]
            }

        return {"job": self.jobs.start("push", work).id}

    def oneline(self, body: dict) -> dict:
        """A sentence -> a workout, for the edit dialog's fast path (#87)."""
        text = (body.get("text") or "").strip()
        if not text:
            raise AppError(
                "type the session first, e.g. 15m warm up, 5 x 1km @ T w/ 2m jog, 10m cool down"
            )
        date = str(body.get("date") or dt.date.today().isoformat())
        try:
            workout = oneline.parse_workout(text, name=body.get("name") or None, date=date)
        except (oneline.OneLineError, PlanError, ValueError) as exc:
            raise AppError(str(exc)) from exc
        return {"workout": workout.to_dict()}

    def diff(self, body: dict) -> dict:
        """What changed between two versions of a plan (#42)."""
        return diff_plans(_plan_of(body.get("before")), _plan_of(body.get("after"))).to_dict()

    def adapt_plan(self, body: dict) -> dict:
        """Pause, replan around missed sessions, or build a return-to-run ramp.

        Every change comes back with its reason; the UI shows them beside
        the diff rather than silently rewriting the calendar.
        """
        profile = self.profile()
        action = body.get("action")
        try:
            if action == "pause":
                start = _date_of(body.get("start"), "start")
                days = int(body.get("days") or 0)
                if days < 1:
                    raise AppError("how many days off?")
                reason = str(body.get("reason") or "break")
                result = adapt.pause_plan(_plan_of(body.get("plan")), start, days, reason)
            elif action == "missed":
                dates = [_date_of(d, "date") for d in body.get("dates") or []]
                if not dates:
                    raise AppError("which sessions were missed?")
                result = adapt.replan_missed(_plan_of(body.get("plan")), dates, profile)
            elif action == "return":
                start = _date_of(body.get("start"), "start")
                tier, why = adapt.layoff_tier(int(body.get("days_off") or 0))
                result = adapt.Adaptation(plan=library.return_to_run(start, tier), reasons=[why])
            else:
                raise AppError("action must be pause, missed or return")
        except (PlanError, library.LibraryError, ValueError) as exc:
            raise AppError(str(exc)) from exc
        described = self._describe(result.plan, profile)
        described["adaptation"] = {
            "reasons": result.reasons,
            "dropped": result.dropped,
            "moved": result.moved,
        }
        return described

    def export(self, body: dict) -> dict:
        """The plan as JSON or a share bundle, or one session in a cross-training format."""
        profile = self.profile()
        plan = _plan_of(body.get("plan"))
        fmt = str(body.get("format") or "json").lower()
        base = _slug(plan.plan)
        if fmt == "json":
            return {"text": plan.dumps(), "filename": f"{base}.json", "mime": "application/json"}
        if fmt == "share":
            text = export_share(plan, profile, note=(body.get("note") or None))
            return {"text": text, "filename": f"{base}.share.json", "mime": "application/json"}
        if fmt == "ics":
            return {
                "text": export_ics(plan, profile),
                "filename": f"{base}.ics",
                "mime": "text/calendar",
            }
        try:
            workout = plan.workouts[int(body.get("index"))]
        except (TypeError, ValueError, IndexError) as exc:
            raise AppError("which session? pass its index") from exc
        if fmt == "fit":
            import base64

            from ..fit import encode_workout

            data = encode_workout(workout, profile)
            return {
                "base64": base64.b64encode(data).decode("ascii"),
                "filename": f"{_slug(workout.name)}-{workout.date.isoformat()}.fit",
                "mime": "application/octet-stream",
            }
        try:
            text = export_workout(workout, profile, fmt)
        except FormatError as exc:
            raise AppError(str(exc)) from exc
        ext = {"icu": "txt", "zwo": "zwo", "mrc": "mrc", "erg": "erg"}[fmt]
        filename = f"{_slug(workout.name)}-{workout.date.isoformat()}.{ext}"
        return {"text": text, "filename": filename, "mime": "text/plain"}

    def library_action(self, body: dict) -> dict:
        """Saved sessions and plan templates on this machine (#5, #6)."""
        root = self.library_root
        action = body.get("action") or "list"
        try:
            if action == "list":
                return {"workouts": library.list_workouts(root), "plans": library.list_plans(root)}
            if action == "save_workout":
                workout = body.get("workout")
                if not isinstance(workout, dict):
                    raise AppError("no session to save")
                path = library.save_workout(workout, name=body.get("name") or None, root=root)
                return {"saved": str(path), "workouts": library.list_workouts(root)}
            if action == "save_plan":
                plan = _plan_of(body.get("plan"))
                path = library.save_plan(plan, name=body.get("name") or None, root=root)
                return {"saved": str(path), "plans": library.list_plans(root)}
            if action == "workout":
                date = _date_of(body.get("date"), "date")
                workout = library.load_workout(str(body.get("name") or ""), date, root=root)
                return {"workout": workout.to_dict()}
            if action == "plan":
                start = _date_of(body["start"], "start") if body.get("start") else _next_monday()
                race = _date_of(body["race_date"], "race date") if body.get("race_date") else None
                plan = library.apply_plan(str(body.get("name") or ""), start, race, root=root)
                return self._describe(plan, self.profile())
        except (library.LibraryError, PlanError) as exc:
            raise AppError(str(exc)) from exc
        raise AppError("unknown library action")

    def regenerate(self, body: dict) -> dict:
        """Rewrite one session with the model, in place (#41)."""
        profile = self.profile()
        configs, default = load_providers(profile.raw)
        name = body.get("provider") or pick_default(configs, default)
        if name not in configs:
            raise AppError(f"unknown provider {name!r}")
        config = configs[name]
        if is_manual(config):
            raise AppError(
                "rewriting one session needs an API provider; with paste, edit the session by hand"
            )
        plan = _plan_of(body.get("plan"))
        date = str(body.get("date") or "")
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            raise AppError("say what should change about this session")

        def work(job) -> dict:
            provider = build_provider(config)
            result = regenerate_workout(provider, profile, plan, date, instruction, log=job.say)
            return self._describe(result, profile)

        return {"job": self.jobs.start("regenerate", work).id}

    def heat(self, body: dict) -> dict:
        """Hot-day paces (#166): every zone slowed for the dew point, without editing the plan."""
        profile = self.profile()
        try:
            temp = float(body.get("temp_c"))
            humidity = float(body.get("humidity_pct"))
        except (TypeError, ValueError) as exc:
            raise AppError("give the temperature (C) and relative humidity (%)") from exc
        dew = dew_point_c(temp, humidity)
        band = combined_band(temp, dew)
        slowdown = max(0.0, heat_slowdown_spk(dew))  # cold never makes a target faster
        zones = {
            name: {
                "slow": format_pace(slow + slowdown, profile.imperial),
                "fast": format_pace(fast + slowdown, profile.imperial),
            }
            for name, (slow, fast) in profile.zone_table().items()
        }
        notes = {
            "normal": "Nothing to adjust today.",
            "adjust": "Hot enough to matter: use the slowed paces, or run by effort, and drink to thirst.",
            "no-hard-running": "Temperature plus dew point is in the range coaches call off hard running: keep today easy or move the quality session to the coolest hour.",
        }
        return {
            "dew_point_c": round(dew, 1),
            "band": band,
            "slowdown_spk": round(slowdown),
            "zones": zones,
            "note": notes[band],
        }

    def _history(self) -> History | None:
        path = self.history_path or DB_PATH
        return History(path) if Path(path).exists() else None

    def history(self, body: dict) -> dict:
        """Synced weekly volume (#167), for the mileage graph; empty without a history."""
        store = self._history()
        if store is None:
            return {"weeks": []}
        days = int(body.get("since_days") or 84)
        since = dt.date.today() - dt.timedelta(days=days)
        try:
            return {"weeks": store.weekly(since)}
        finally:
            store.close()

    def recap(self, body: dict) -> dict:
        """Recap lines for a plan's past sessions from synced runs (#155)."""
        profile = self.profile()
        plan = _plan_of(body.get("plan"))
        store = self._history()
        if store is None:
            return {"recaps": []}
        today = dt.date.today()
        planned = [
            {
                "date": w.date.isoformat(),
                "name": w.name,
                "seconds": _summary(w, profile)["seconds"],
                "metres": _summary(w, profile)["metres"],
            }
            for w in plan.sorted_workouts()
            if w.date < today
        ]
        try:
            return {"recaps": store.recap(planned, profile.lthr)}
        finally:
            store.close()

    def recent(self, body: dict) -> dict:
        """Recent plans (#165): list them, or open one by path."""
        path = body.get("path")
        if path:
            allowed = {str(r.path) for r in list_recent(self.recent_root)}
            if str(path) not in allowed:
                raise AppError("not a recent plan", status=404)
            try:
                plan = Plan.from_dict(load_recent(path))
            except (PlanError, OSError, ValueError) as exc:
                raise AppError(str(exc)) from exc
            return self._describe(plan, self.profile())
        return {"plans": [r.to_dict() for r in list_recent(self.recent_root)]}

    def job(self, body: dict) -> dict:
        job = self.jobs.get(body.get("id", ""))
        if job is None:
            raise AppError("no such job", status=404)
        return job.snapshot()

    def job_input(self, body: dict) -> dict:
        job = self.jobs.get(body.get("id", ""))
        if job is None:
            raise AppError("no such job", status=404)
        if not job.provide(str(body.get("value", ""))):
            raise AppError("that job is not waiting for input")
        return {"ok": True}


def _athlete_of(profile: Profile) -> dict:
    """The onboarding answers, for pre-filling the setup screen (#95)."""
    a = profile.availability
    g = profile.goal_race
    return {
        "injuries": list(profile.injuries),
        "constraints": list(profile.constraints),
        "instructions": profile.instructions or "",
        "longest_recent_run_km": profile.longest_recent_run_km,
        "recent_weekly_km": profile.recent_weekly_km,
        "availability": (
            {
                "days": list(a.days),
                "weekday_max_minutes": a.weekday_max_minutes,
                "weekend_max_minutes": a.weekend_max_minutes,
                "long_run_day": a.long_run_day,
                "sessions_per_week": a.sessions_per_week,
            }
            if a
            else None
        ),
        "goal_race": (
            {
                "name": g.name,
                "date": g.date.isoformat(),
                "distance": g.distance,
                "priority": g.priority,
                "goal_time": g.goal_time,
            }
            if g
            else None
        ),
    }


def _put(data: dict, key: str, value: dict) -> None:
    """A section is present or absent, never an empty table."""
    if value:
        data[key] = value
    else:
        data.pop(key, None)


def _lines(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    return [str(x).strip() for x in value or [] if str(x).strip()]


def _number(value: Any, key: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{key.replace('_', ' ')} must be a number") from exc


def _plan_of(raw: Any) -> Plan:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppError(f"that is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AppError("no plan supplied")
    try:
        return Plan.from_dict(raw)
    except PlanError as exc:
        raise AppError(str(exc)) from exc


def _date_of(value: Any, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise AppError(f"{label} must be a date like 2026-03-14") from exc


def _next_monday() -> dt.date:
    today = dt.date.today()
    return today + dt.timedelta(days=7 - today.weekday())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "plan"


def _zones_of(profile: Profile) -> list[dict]:
    """Pace zones with the same intensity scale the timeline uses for colour."""
    zones = []
    for name in profile.pace_zones:
        slow, fast = profile.pace_zone(name)
        zones.append(
            {
                "name": name,
                "slow": format_pace(slow, profile.imperial),
                "fast": format_pace(fast, profile.imperial),
                "intensity": ZONE_INTENSITY.get(name, 0.5),
            }
        )
    return zones


ROUTES = {
    "/api/state": "state",
    "/api/estimate": "estimate",
    "/api/zones-preview": "zones_preview",
    "/api/estimate-lthr": "estimate_lthr",
    "/api/profile": "save_profile",
    "/api/preview": "preview",
    "/api/generate": "generate",
    "/api/push": "push",
    "/api/job": "job",
    "/api/job-input": "job_input",
    "/api/oneline": "oneline",
    "/api/diff": "diff",
    "/api/adapt": "adapt_plan",
    "/api/export": "export",
    "/api/library": "library_action",
    "/api/regenerate": "regenerate",
    "/api/recent": "recent",
    "/api/heat": "heat",
    "/api/history": "history",
    "/api/recap": "recap",
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gpp"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # the UI is the log

        # --- helpers ---

        def _send(
            self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(
                status,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ALLOWED_HOSTS

        # --- routing ---

        def do_GET(self) -> None:
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
                return
            if path == "/manifest.webmanifest":
                self._serve_static("manifest.webmanifest")
                return
            if path == "/sw.js":
                # Served from the root so the worker's scope covers the app.
                self._serve_static("sw.js", extra={"Service-Worker-Allowed": "/"})
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "forbidden"})
                return
            path = self.path.split("?", 1)[0]
            method = ROUTES.get(path)
            if method is None:
                self._json(404, {"error": "no such endpoint"})
                return
            # Compare as bytes: header values arrive latin-1 decoded, and
            # compare_digest raises TypeError on non-ASCII str, which would
            # turn a rejected request into a 500 and a console traceback.
            supplied = self.headers.get("X-GPP-Token", "").encode("utf-8", "surrogateescape")
            if not secrets.compare_digest(supplied, app.token.encode("utf-8")):
                self._json(403, {"error": "bad or missing token; reload the page"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "request too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "malformed JSON body"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be a JSON object"})
                return

            try:
                self._json(200, getattr(app, method)(body) or {})
            except AppError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (PlanError, ProfileError, ProviderError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

        # --- static ---

        def _serve_index(self) -> None:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
            html = html.replace("__GPP_TOKEN__", app.token)
            self._send(200, html.encode("utf-8"), CONTENT_TYPES[".html"])

        def _serve_static(self, name: str, extra: dict[str, str] | None = None) -> None:
            target = (STATIC / name).resolve()
            if not target.is_file() or STATIC.resolve() not in target.parents:
                self._send(404, b"not found", "text/plain")
                return
            content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), content_type, extra)

    return Handler


class QuietServer(ThreadingHTTPServer):
    """A closed tab is not an error.

    The stdlib prints a full traceback whenever a client goes away mid-write,
    which on a desktop app means every time someone closes the window. Real
    faults still surface; dropped connections do not.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        import sys
        import traceback

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        traceback.print_exc()


def serve(port: int = 8765, open_browser: bool = True, profile_path: Path | None = None) -> None:
    app = App(profile_path)
    try:
        server = QuietServer(("127.0.0.1", port), make_handler(app))
    except OSError as exc:
        raise SystemExit(
            f"Could not listen on port {port}: {exc}\n"
            f"Something else is probably using it — try  gpp web --port {port + 1}"
        ) from exc
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\n  garmin-plan-push is running at {url}")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        server.server_close()
