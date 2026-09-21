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

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import checks
from ..compile import compile_plan
from ..estimate import EstimateError, lthr_from_max, threshold_from_race
from ..generate import generate_plan
from ..load import plan_dashboard
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
from ..render import render_plan
from ..timeline import ZONE_INTENSITY, workout_summary, workout_timeline
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
        existing = self.try_profile()
        raw = dict(existing.raw) if existing else {}

        data: dict[str, Any] = {
            "name": (body.get("name") or "athlete").strip() or "athlete",
            "units": "imperial" if body.get("imperial") else "metric",
            "pace": {"threshold": body.get("threshold") or "5:00/km"},
        }
        hr: dict[str, Any] = {}
        if body.get("lthr"):
            hr["lthr"] = int(body["lthr"])
        if body.get("hr_max"):
            hr["max"] = int(body["hr_max"])
        if hr:
            data["hr"] = hr
        if raw.get("ai"):
            data["ai"] = raw["ai"]
        if body.get("providers"):
            data["ai"] = body["providers"]

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
            return self._describe(Plan.from_dict(raw), profile)
        except (PlanError, ProfileError) as exc:
            raise AppError(str(exc)) from exc

    def _describe(self, plan: Plan, profile: Profile) -> dict:
        compiled = compile_plan(plan, profile)
        workouts = []
        for workout, item in zip(plan.workouts, compiled, strict=True):
            summary = workout_summary(workout, profile)
            workouts.append(
                {
                    "name": workout.name,
                    "date": workout.date.isoformat(),
                    "notes": workout.notes,
                    "sport": workout.sport,
                    "role": workout.role,
                    "phase": workout.phase,
                    "estimate": format_duration(item.estimated_seconds),
                    "seconds": item.estimated_seconds,
                    "summary": summary,
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
            return self._describe(result.plan, profile)

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
            results = client.push(compiled, replace=replace, verify=True, log=job.say)
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
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gpp"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # the UI is the log

        # --- helpers ---

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
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

        def _serve_static(self, name: str) -> None:
            target = (STATIC / name).resolve()
            if not target.is_file() or STATIC.resolve() not in target.parents:
                self._send(404, b"not found", "text/plain")
                return
            content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), content_type)

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
