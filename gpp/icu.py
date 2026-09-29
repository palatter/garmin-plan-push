"""intervals.icu (#179): push a plan to an intervals.icu calendar over its API.

intervals.icu is a Garmin partner, so a workout on its calendar syncs to
Garmin Connect as a structured workout. For anyone who would rather not use
the unofficial Connect API this is the approved route, and it comes with
intervals.icu's own planning tools. Each event carries the text that
``gpp export --format icu`` produces, and an ``external_id`` made of the
plan's tag prefix, the date and the session's place on that day, so a
re-push -- edited or not -- updates the same event instead of adding one.

API reference: https://intervals.icu/api-docs.html -- HTTP Basic with the
user name ``API_KEY`` and the athlete's key as the password. Athlete ids look
like ``i12345`` (intervals.icu -> Settings -> Developer). Nothing here has
been run against a live account; the transport is injectable so the tests
cover everything up to the socket.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .compile import compile_plan, legacy_slug
from .constants import TAG_PREFIX
from .formats import FormatError, export_icu
from .plan import Plan
from .profile import Profile

BASE_URL = "https://intervals.icu/api/v1"
KEY_ENV = "ICU_API_KEY"
ATHLETE_ENV = "ICU_ATHLETE_ID"
ATHLETE_ID = re.compile(r"^i\d+$")
SPORT_TYPES = {
    "running": "Run",
    "cycling": "Ride",
    "swimming": "Swim",
    "strength": "WeightTraining",
    "cardio": "Workout",
}

# (method, url, json body or None, headers) -> (status, body bytes)
Transport = Callable[[str, str, object, dict[str, str]], tuple[int, bytes]]


class IcuError(RuntimeError):
    """intervals.icu said no, or could not be reached."""


@dataclass(frozen=True)
class IcuResult:
    date: str
    name: str
    event_id: int | None
    action: str  # sent | would-send | deleted

    def describe(self) -> str:
        suffix = f"  (event {self.event_id})" if self.event_id is not None else ""
        return f"{self.date}  {self.action:<10} {self.name}{suffix}"


def http(method: str, url: str, body: object, headers: dict[str, str]) -> tuple[int, bytes]:
    """The real transport: stdlib only, https, thirty-second timeout."""
    if not url.startswith("https://"):
        raise IcuError(f"refusing a non-https intervals.icu URL: {url}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https only
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IcuError(f"intervals.icu is unreachable: {exc}") from exc


def _headers(key: str) -> dict[str, str]:
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def tag_prefix(plan: Plan) -> str:
    """The start of this plan's event ids on intervals.icu.

    Still the slug gpp 0.2.1 used: the bulk endpoint upserts by id, so a new
    slug would put a second copy of every session already sent.
    """
    return f"[{TAG_PREFIX}:{legacy_slug(plan.plan)}:"


def events(plan: Plan, profile: Profile) -> list[dict]:
    """The plan as intervals.icu calendar events, one per session."""
    out = []
    prefix = tag_prefix(plan)
    seen_on: dict[str, int] = {}
    compile_plan(plan, profile)  # the same validation a Garmin push gets
    for workout in plan.workouts:
        day = workout.date.isoformat()
        seen_on[day] = seen_on.get(day, 0) + 1
        try:
            description = export_icu(workout, profile)
        except FormatError:
            description = workout.notes or workout.name
        out.append(
            {
                "category": "WORKOUT",
                "start_date_local": f"{workout.date.isoformat()}T00:00:00",
                "type": SPORT_TYPES.get(workout.sport, "Workout"),
                "name": workout.name,
                "description": description,
                # No content hash in it: the bulk endpoint upserts by this
                # id, so an edited session must keep the id it was pushed
                # with. The date and the session's place on that day keep
                # identical sessions on different days, or twice a day, apart.
                "external_id": f"{prefix}{day}:{seen_on[day]}",
            }
        )
    return out


def _check(athlete: str | None, key: str | None) -> None:
    if not ATHLETE_ID.match(athlete or ""):
        raise IcuError(
            'the athlete id looks like "i12345" (intervals.icu -> Settings -> Developer); '
            f"pass --athlete or set {ATHLETE_ENV}"
        )
    if not key:
        raise IcuError(f"no API key: set {KEY_ENV}, or run in a terminal to be asked for it")


def _raise_for(status: int, raw: bytes) -> None:
    if status in (401, 403):
        raise IcuError("intervals.icu rejected the key: check the API key and the athlete id")
    if status == 404:
        raise IcuError("intervals.icu: athlete not found; the id looks like i12345")
    if status >= 400:
        detail = raw[:200].decode("utf-8", "replace")
        raise IcuError(f"intervals.icu returned {status}: {detail}")


def _json(raw: bytes) -> object:
    if not raw:
        return []
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise IcuError("intervals.icu returned something that is not JSON") from exc


def push(
    plan: Plan,
    profile: Profile,
    athlete: str | None,
    key: str | None,
    *,
    transport: Transport = http,
    base_url: str = BASE_URL,
    dry_run: bool = False,
) -> list[IcuResult]:
    """Send every session as a calendar event; existing events with the same
    external id are updated (the bulk endpoint's upsert)."""
    body = events(plan, profile)
    if dry_run:
        return [IcuResult(e["start_date_local"][:10], e["name"], None, "would-send") for e in body]
    _check(athlete, key)
    url = f"{base_url}/athlete/{athlete}/events/bulk?upsert=true"
    status, raw = transport("POST", url, body, _headers(key or ""))
    _raise_for(status, raw)
    made = _json(raw)
    created = made if isinstance(made, list) else []
    results = []
    for i, event in enumerate(body):
        item = created[i] if i < len(created) and isinstance(created[i], dict) else {}
        event_id = item.get("id")
        results.append(
            IcuResult(
                event["start_date_local"][:10],
                event["name"],
                int(event_id) if isinstance(event_id, int) else None,
                "sent",
            )
        )
    return results


def remove(
    plan: Plan,
    profile: Profile,
    athlete: str | None,
    key: str | None,
    *,
    transport: Transport = http,
    base_url: str = BASE_URL,
) -> list[IcuResult]:
    """Delete this plan's events from the calendar, matched by the plan's tag
    prefix so sessions edited since the push are still found."""
    _check(athlete, key)
    dates = sorted(w.date for w in plan.workouts)
    if not dates:
        return []
    headers = _headers(key or "")
    url = f"{base_url}/athlete/{athlete}/events?oldest={dates[0]}&newest={dates[-1]}"
    status, raw = transport("GET", url, None, headers)
    _raise_for(status, raw)
    listed = _json(raw)
    prefix = tag_prefix(plan)
    out = []
    for event in listed if isinstance(listed, list) else []:
        if not isinstance(event, dict) or event.get("id") is None:
            continue
        if not str(event.get("external_id") or "").startswith(prefix):
            continue
        status, raw = transport(
            "DELETE", f"{base_url}/athlete/{athlete}/events/{event['id']}", None, headers
        )
        _raise_for(status, raw)
        out.append(
            IcuResult(
                str(event.get("start_date_local", ""))[:10],
                str(event.get("name", "")),
                int(event["id"]),
                "deleted",
            )
        )
    return out
