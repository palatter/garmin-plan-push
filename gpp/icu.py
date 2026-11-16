"""intervals.icu (#179): push a plan to an intervals.icu calendar over its API.

intervals.icu is a Garmin partner, so a workout on its calendar syncs to
Garmin Connect as a structured workout. For anyone who would rather not use
the unofficial Connect API this is the approved route, and it comes with
intervals.icu's own planning tools. Each event carries the text that
``gpp export --format icu`` produces, and an ``external_id`` made of the
plan's tag prefix, the date and the session's place on that day, so a
re-push -- edited or not -- updates the same event instead of adding one.
A session moved to another day gets a new id, so the old event is found
by the prefix and removed with ``--prune``.

API reference: https://intervals.icu/api-docs.html -- HTTP Basic with the
user name ``API_KEY`` and the athlete's key as the password. Athlete ids look
like ``i12345`` (intervals.icu -> Settings -> Developer). Nothing here has
been run against a live account; the transport is injectable so the tests
cover everything up to the socket.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from . import keychain
from .compile import compile_plan, legacy_slug, plan_slug
from .constants import TAG_PREFIX
from .formats import FormatError, export_icu
from .plan import Plan
from .profile import Profile

BASE_URL = "https://intervals.icu/api/v1"
# How far either side of a plan's dates a session moved since a push is
# looked for. Only from today on: past events are the record.
MOVED_MARGIN_DAYS = 28
KEY_ENV = "ICU_API_KEY"
ATHLETE_ENV = "ICU_ATHLETE_ID"
# Where the profile keeps the athlete id once the web app has been given it.
PROFILE_SECTION = "intervals_icu"
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


def athlete_id(given: str | None, profile: Profile | None) -> str | None:
    """The athlete id: as given, else ICU_ATHLETE_ID, else the profile's."""
    if given:
        return given.strip()
    if env := os.environ.get(ATHLETE_ENV):
        return env.strip()
    section = profile.raw.get(PROFILE_SECTION) if profile else None
    saved = str(section.get("athlete") or "").strip() if isinstance(section, dict) else ""
    return saved or None


def api_key(given: str | None = None) -> str | None:
    """The API key: as given, else ICU_API_KEY, else the OS keychain's."""
    return given or keychain.lookup(KEY_ENV)


def _headers(key: str) -> dict[str, str]:
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def tag_prefix(plan: Plan) -> str:
    """The start of this plan's event ids on intervals.icu: the slug the
    Garmin tags use, different for every plan name."""
    return f"[{TAG_PREFIX}:{plan_slug(plan.plan)}:"


def legacy_prefix(plan: Plan) -> str:
    """The start of the ids gpp 0.2.2 and earlier gave this plan's events: the
    first 16 letters of its name, which "Marathon Build Block 1" and "...
    Block 2" share. Sending one overwrote the other's events on shared
    dates, and pruning or removing one deleted the other's."""
    return f"[{TAG_PREFIX}:{legacy_slug(plan.plan)}:"


def _where(event: dict) -> tuple[str, str]:
    return str(event.get("start_date_local") or "")[:10], str(event.get("name") or "").strip()


def _adopted(plan: Plan, found: list[dict] | None) -> dict[tuple[str, str], str]:
    """(date, name) -> the old-style id of that session's event, when it is on
    the calendar. The bulk endpoint upserts by id, so sending the session
    under a new id would leave the old event beside it; it keeps the old one."""
    old = legacy_prefix(plan)
    sessions = {(w.date.isoformat(), w.name.strip()) for w in plan.workouts}
    kept: dict[tuple[str, str], str] = {}
    for event in found or []:
        external = str(event.get("external_id") or "")
        if external.startswith(old) and _where(event) in sessions:
            kept.setdefault(_where(event), external)
    return kept


def events(plan: Plan, profile: Profile, found: list[dict] | None = None) -> list[dict]:
    """The plan as intervals.icu calendar events, one per session.

    `found` is what `find` returned: a session sent by an older gpp keeps the
    id it has there, so it is updated and not doubled.
    """
    out = []
    prefix = tag_prefix(plan)
    kept = _adopted(plan, found)
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
                "external_id": kept.get(
                    (day, workout.name.strip()), f"{prefix}{day}:{seen_on[day]}"
                ),
            }
        )
    return out


def check_credentials(athlete: str | None, key: str | None) -> None:
    if not ATHLETE_ID.match(athlete or ""):
        raise IcuError(
            'the athlete id looks like "i12345" (intervals.icu -> Settings -> Developer); '
            f"pass --athlete, set {ATHLETE_ENV}, or give it once in the web app"
        )
    if not key:
        raise IcuError(
            f"no API key: add it in the web app, or with  gpp keys set {KEY_ENV},"
            " or run in a terminal to be asked for it"
        )


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
    found: list[dict] | None = None,
) -> list[IcuResult]:
    """Send every session as a calendar event; existing events with the same
    external id are updated (the bulk endpoint's upsert). Pass what `find`
    returned as `found`, so sessions an older gpp sent are updated too."""
    body = events(plan, profile, found)
    if dry_run:
        return [IcuResult(e["start_date_local"][:10], e["name"], None, "would-send") for e in body]
    check_credentials(athlete, key)
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


def find(
    plan: Plan,
    athlete: str | None,
    key: str | None,
    *,
    transport: Transport = http,
    base_url: str = BASE_URL,
    today: dt.date | None = None,
) -> list[dict]:
    """This plan's events on the calendar: over the plan's dates and, from
    today on, four weeks either side of them.

    Matched by the plan's own id prefix. An event with the prefix gpp 0.2.2
    and earlier used, which other plans can share, counts only when it is one
    of this plan's sessions by date and name; any other is left alone, never
    listed for pruning or removal.
    """
    check_credentials(athlete, key)
    dates = sorted(w.date for w in plan.workouts)
    if not dates:
        return []
    today = today or dt.date.today()
    margin = dt.timedelta(days=MOVED_MARGIN_DAYS)
    oldest = min(dates[0], max(dates[0] - margin, today))
    newest = dates[-1] + margin
    url = f"{base_url}/athlete/{athlete}/events?oldest={oldest}&newest={newest}"
    status, raw = transport("GET", url, None, _headers(key or ""))
    _raise_for(status, raw)
    listed = _json(raw)
    prefix, old = tag_prefix(plan), legacy_prefix(plan)
    sessions = {(w.date.isoformat(), w.name.strip()) for w in plan.workouts}
    found = []
    for event in listed if isinstance(listed, list) else []:
        if not isinstance(event, dict) or event.get("id") is None:
            continue
        external = str(event.get("external_id") or "")
        if not external.startswith(prefix) and not (
            external.startswith(old) and _where(event) in sessions
        ):
            continue
        day = str(event.get("start_date_local") or "")[:10]
        if dates[0].isoformat() <= day <= dates[-1].isoformat() or day >= today.isoformat():
            found.append(event)
    return found


def stale(plan: Plan, profile: Profile, found: list[dict]) -> list[dict]:
    """Found events the plan no longer has: sessions moved to another day or
    dropped since they were sent. Upserting the plan never removes them."""
    wanted = {e["external_id"] for e in events(plan, profile, found)}
    return [e for e in found if e.get("external_id") not in wanted]


def delete(
    found: list[dict],
    athlete: str | None,
    key: str | None,
    *,
    transport: Transport = http,
    base_url: str = BASE_URL,
) -> list[IcuResult]:
    check_credentials(athlete, key)
    headers = _headers(key or "")
    out = []
    for event in found:
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
