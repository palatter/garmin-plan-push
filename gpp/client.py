"""The Garmin Connect push layer.

Everything that touches the network lives here, deliberately quarantined from
the compiler so the interesting logic stays testable offline.

A note on how this talks to Garmin
----------------------------------
`python-garminconnect` is a reverse-engineered client and its internal
transport has changed shape more than once (garth session, then a mobile SSO
flow). Rather than pin this tool to one private attribute that may vanish, we
probe a short list of known transports at connect time and report which one we
got. If all of them fail you get one clear message naming the library version,
instead of an AttributeError from three frames deep.

The endpoints themselves have been stable for years:

    POST   /workout-service/workout              create
    GET    /workout-service/workout/{id}         read back
    DELETE /workout-service/workout/{id}         delete
    POST   /workout-service/schedule/{id}        put on the calendar
    GET    /calendar-service/year/{y}/month/{m}  what is already scheduled

Gotcha encoded below: the calendar endpoint's month is ZERO-indexed.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .compile import CompiledWorkout
from .constants import TAG_PREFIX

WORKOUT_CREATE = "/workout-service/workout"
WORKOUT_ITEM = "/workout-service/workout/{workout_id}"
WORKOUT_SCHEDULE = "/workout-service/schedule/{workout_id}"
CALENDAR_MONTH = "/calendar-service/year/{year}/month/{month}"

TAG_RE = re.compile(rf"\[{TAG_PREFIX}:([a-z0-9]+):([0-9a-f]{{8}})\]")


class PushError(RuntimeError):
    """Something went wrong talking to Garmin Connect."""


@dataclass
class PushResult:
    name: str
    date: str
    action: str  # created | replaced | unchanged | failed
    workout_id: int | None = None
    detail: str = ""


def parse_tag(description: str | None) -> tuple[str, str] | None:
    """Return (plan_slug, content_hash) if this is one of our workouts."""
    if not description:
        return None
    match = TAG_RE.search(description)
    return (match.group(1), match.group(2)) if match else None


class GarminClient:
    """Thin adapter over python-garminconnect with an explicit transport probe."""

    def __init__(self, email: str, password: str | None = None, token_dir: str | None = None):
        self.email = email
        self._password = password
        self._token_dir = token_dir
        self._api: Any = None
        self._request: Callable[..., Any] | None = None
        self.transport: str = "unconnected"

    # --- connection ---

    def connect(self, prompt_mfa: Callable[[], str] | None = None) -> None:
        try:
            from garminconnect import Garmin
        except ImportError as exc:  # pragma: no cover - env dependent
            raise PushError("python-garminconnect is not installed. Run: uv sync") from exc

        kwargs: dict[str, Any] = {}
        if self._token_dir:
            kwargs["tokenstore"] = self._token_dir
        if prompt_mfa is not None:
            kwargs["prompt_mfa"] = prompt_mfa

        try:
            self._api = Garmin(self.email, self._password, **kwargs)
            self._api.login()
        except TypeError:
            # Older/newer signatures differ in which kwargs they accept.
            self._api = Garmin(self.email, self._password)
            self._api.login()
        except Exception as exc:
            raise PushError(f"Garmin login failed: {exc}") from exc

        self._request = self._resolve_transport()

    def _resolve_transport(self) -> Callable[..., Any]:
        """Find a way to issue an authenticated request against connectapi."""
        api = self._api

        garth = getattr(api, "garth", None)
        if garth is not None and hasattr(garth, "connectapi"):
            self.transport = "garth.connectapi"
            return lambda method, path, **kw: garth.connectapi(path, method=method, **kw)

        if garth is not None and hasattr(garth, "request"):
            self.transport = "garth.request"

            def _via_garth(method: str, path: str, **kw: Any) -> Any:
                response = garth.request(method, "connectapi", path, api=True, **kw)
                return _json_or_none(response)

            return _via_garth

        for name in ("connectapi", "request", "_request", "modern_request"):
            fn = getattr(api, name, None)
            if callable(fn):
                self.transport = f"Garmin.{name}"
                return lambda method, path, _fn=fn, **kw: _fn(path, method=method, **kw)

        raise PushError(
            "could not find an authenticated request method on this version of "
            "python-garminconnect. Check `pip show garminconnect` and open "
            "gpp/client.py -- _resolve_transport() is the single place to add "
            "the new entry point."
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._request is None:
            raise PushError("not connected; call connect() first")
        try:
            return self._request(method, path, **kwargs)
        except Exception as exc:
            raise PushError(f"{method} {path} failed: {exc}") from exc

    # --- reads ---

    def scheduled_between(self, start: dt.date, end: dt.date) -> list[dict]:
        """Calendar items across a date range. Month is zero-indexed at Garmin."""
        items: list[dict] = []
        seen: set[Any] = set()
        cursor = dt.date(start.year, start.month, 1)
        while cursor <= end:
            payload = self._call(
                "GET",
                CALENDAR_MONTH.format(year=cursor.year, month=cursor.month - 1),
            )
            for item in (payload or {}).get("calendarItems", []) or []:
                key = (item.get("id"), item.get("date"))
                if key in seen:
                    continue
                seen.add(key)
                raw_date = item.get("date")
                if not raw_date:
                    continue
                try:
                    when = dt.date.fromisoformat(raw_date[:10])
                except ValueError:
                    continue
                if start <= when <= end:
                    items.append(item)
            cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
        return items

    def get_workout(self, workout_id: int) -> dict:
        return self._call("GET", WORKOUT_ITEM.format(workout_id=workout_id))

    # --- writes ---

    def create_workout(self, payload: dict) -> int:
        result = self._call("POST", WORKOUT_CREATE, json=payload)
        workout_id = (result or {}).get("workoutId")
        if not workout_id:
            raise PushError(f"Garmin did not return a workoutId (got {result!r})")
        return int(workout_id)

    def delete_workout(self, workout_id: int) -> None:
        self._call("DELETE", WORKOUT_ITEM.format(workout_id=workout_id))

    def schedule_workout(self, workout_id: int, date: str) -> None:
        self._call(
            "POST",
            WORKOUT_SCHEDULE.format(workout_id=workout_id),
            json={"date": date},
        )

    # --- the useful one ---

    def push(
        self,
        compiled: list[CompiledWorkout],
        replace: bool = True,
        verify: bool = True,
        log: Callable[[str], None] = lambda _: None,
    ) -> list[PushResult]:
        """Upload and schedule, skipping anything already on the calendar unchanged.

        Only workouts carrying our tag are ever deleted. Anything you built by
        hand in Garmin Connect is left strictly alone.
        """
        results: list[PushResult] = []
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        existing = self.scheduled_between(min(dates), max(dates))
        by_date = _index_by_date(existing)

        for item in compiled:
            try:
                results.append(
                    self._push_one(item, by_date, replace=replace, verify=verify, log=log)
                )
            except PushError as exc:
                results.append(PushResult(item.name, item.date, "failed", detail=str(exc)))
        return results

    def _push_one(
        self,
        item: CompiledWorkout,
        by_date: dict[str, list[dict]],
        replace: bool,
        verify: bool,
        log: Callable[[str], None],
    ) -> PushResult:
        _, _, want_hash = item.tag.strip("[]").split(":")
        stale: list[int] = []

        for candidate in by_date.get(item.date, []):
            existing_id = candidate.get("workoutId") or candidate.get("id")
            title = candidate.get("title") or ""
            tag = parse_tag(candidate.get("description")) or parse_tag(title)
            if tag is None:
                continue  # hand-made workout: never touch
            if tag[1] == want_hash and title.strip() == item.name.strip():
                log(f"  unchanged  {item.date}  {item.name}")
                return PushResult(item.name, item.date, "unchanged", existing_id)
            if existing_id:
                stale.append(int(existing_id))

        if stale and not replace:
            return PushResult(
                item.name,
                item.date,
                "failed",
                detail=f"{len(stale)} tagged workout(s) already on {item.date}; "
                "re-run with --replace to overwrite",
            )

        workout_id = self.create_workout(item.payload)
        self.schedule_workout(workout_id, item.date)

        if verify:
            detail = self._verify(workout_id, item)
        else:
            detail = ""

        for old_id in stale:
            try:
                self.delete_workout(old_id)
            except PushError as exc:
                detail = (detail + f" (could not delete old {old_id}: {exc})").strip()

        action = "replaced" if stale else "created"
        log(f"  {action:<10} {item.date}  {item.name}  (id {workout_id})")
        return PushResult(item.name, item.date, action, workout_id, detail)

    def _verify(self, workout_id: int, item: CompiledWorkout) -> str:
        """Read the workout back and check Garmin stored what we sent.

        This is what makes the reverse-engineered constants safe to rely on:
        if an ID is wrong, or Garmin swaps the pace bounds, it shows up here
        rather than on your wrist at 6am.
        """
        try:
            stored = self.get_workout(workout_id)
        except PushError as exc:
            return f"could not verify: {exc}"

        problems: list[str] = []
        sent_steps = _flatten(item.payload["workoutSegments"][0]["workoutSteps"])
        got_steps = _flatten((stored.get("workoutSegments") or [{}])[0].get("workoutSteps", []))
        if len(sent_steps) != len(got_steps):
            problems.append(f"sent {len(sent_steps)} steps, Garmin kept {len(got_steps)}")

        for index, (sent, got) in enumerate(zip(sent_steps, got_steps, strict=False), start=1):
            sent_key = (sent.get("stepType") or {}).get("stepTypeKey")
            got_key = (got.get("stepType") or {}).get("stepTypeKey")
            if sent_key != got_key:
                problems.append(f"step {index}: sent {sent_key}, stored {got_key}")
            one, two = sent.get("targetValueOne"), got.get("targetValueOne")
            if one is not None and two is not None and abs(one - two) > 0.05:
                problems.append(
                    f"step {index}: target value changed {one} -> {two} "
                    "(Garmin may order pace bounds the other way round)"
                )
        return "; ".join(problems)


def _flatten(steps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for step in steps or []:
        if step.get("type") == "RepeatGroupDTO":
            out.append(step)
            out.extend(_flatten(step.get("workoutSteps", [])))
        else:
            out.append(step)
    return out


def _index_by_date(items: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for item in items:
        raw = item.get("date")
        if raw:
            index.setdefault(raw[:10], []).append(item)
    return index


def _json_or_none(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, (dict, list)):
        return response
    for attr in ("json",):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                return None
    return None
