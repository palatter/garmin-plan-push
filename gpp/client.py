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
    PUT    /workout-service/workout/{id}         update in place
    GET    /workout-service/workout/{id}         read back
    DELETE /workout-service/workout/{id}         delete
    POST   /workout-service/schedule/{id}        put on the calendar
    GET    /calendar-service/year/{y}/month/{m}  what is already scheduled

Gotcha encoded below: the calendar endpoint's month is ZERO-indexed.

Anything beyond those -- push to a device, the exercise catalog, HR zones --
goes through the library's own methods by name, looked up with getattr so a
version that lacks one says so instead of crashing.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .compile import CompiledWorkout
from .constants import TAG_PREFIX

WORKOUT_CREATE = "/workout-service/workout"
WORKOUT_ITEM = "/workout-service/workout/{workout_id}"
WORKOUT_SCHEDULE = "/workout-service/schedule/{workout_id}"
CALENDAR_MONTH = "/calendar-service/year/{year}/month/{month}"

TAG_RE = re.compile(rf"\[{TAG_PREFIX}:([a-z0-9]+):([0-9a-f]{{8}})\]")

# HTTP statuses worth one more try on a read; writes are never retried blind.
TRANSIENT = ("429", "502", "503", "504", "timed out", "timeout")
RETRY_DELAYS = (1.0, 2.0, 4.0)


class PushError(RuntimeError):
    """Something went wrong talking to Garmin Connect."""


@dataclass
class PushResult:
    name: str
    date: str
    action: str  # created | replaced | updated | unchanged | removed | failed
    workout_id: int | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "date": self.date,
            "action": self.action,
            "workout_id": self.workout_id,
            "detail": self.detail,
        }


@dataclass
class Device:
    id: str
    name: str
    raw: dict = field(default_factory=dict, repr=False)


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

        # Library versions differ in which keyword arguments the constructor
        # takes. They are dropped one at a time, the MFA prompt last, so an
        # MFA account is never silently downgraded to a login that cannot ask.
        api = None
        for attempt in (kwargs, {k: v for k, v in kwargs.items() if k == "prompt_mfa"}, {}):
            try:
                api = Garmin(self.email, self._password, **attempt)
                break
            except TypeError:
                continue
            except Exception as exc:
                raise PushError(f"could not set up the Garmin client: {exc}") from exc
        if api is None:
            raise PushError("could not construct the Garmin client; check `pip show garminconnect`")
        self._api = api
        try:
            self._api.login()
        except Exception as exc:
            where = self._token_dir or "~/.garminconnect"
            raise PushError(
                f"Garmin login failed: {exc}. If you signed in before, the cached token in "
                f"{where} may be stale -- Garmin expires them without warning -- so delete "
                "that folder and sign in again."
            ) from exc

        self._request = self._resolve_transport()

    @property
    def api(self) -> Any:
        """The underlying library client, for the read-side (sync.py)."""
        if self._api is None:
            raise PushError("not connected; call connect() first")
        return self._api

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
        delays = RETRY_DELAYS if method == "GET" else ()
        for delay in (*delays, None):
            try:
                return self._request(method, path, **kwargs)
            except Exception as exc:
                transient = any(code in str(exc).lower() for code in TRANSIENT)
                if delay is None or not transient:
                    raise PushError(f"{method} {path} failed: {exc}") from exc
                self._sleep(delay)
        return None  # pragma: no cover - the loop always returns or raises

    _sleep = staticmethod(time.sleep)

    def _library(self, name: str, *args: Any) -> Any:
        """Call a python-garminconnect method by name, or explain it is missing."""
        fn = getattr(self.api, name, None)
        if not callable(fn):
            raise PushError(
                f"this version of python-garminconnect has no {name}(); upgrade it (uv lock --upgrade)"
            )
        try:
            return fn(*args)
        except Exception as exc:
            raise PushError(f"{name} failed: {exc}") from exc

    # --- reads ---

    def scheduled_between(self, start: dt.date, end: dt.date) -> list[dict]:
        """Calendar items across a date range. Month is zero-indexed at Garmin."""
        items: list[dict] = []
        seen: set[Any] = set()
        cursor = dt.date(start.year, start.month, 1)
        while cursor <= end:
            payload = self._call(
                "GET", CALENDAR_MONTH.format(year=cursor.year, month=cursor.month - 1)
            )
            for item in (payload or {}).get("calendarItems", []) or []:
                key = (item.get("id") or item.get("workoutId"), item.get("date"), item.get("title"))
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

    def devices(self) -> list[Device]:
        raw = self._library("get_devices") or []
        out = []
        for d in raw:
            if isinstance(d, dict):
                out.append(
                    Device(
                        id=str(d.get("deviceId") or d.get("unitId") or ""),
                        name=str(d.get("displayName") or d.get("productDisplayName") or "device"),
                        raw=d,
                    )
                )
        return out

    def search_exercises(self, query: str) -> list[dict]:
        """Garmin's strength exercise catalog, for validating exercise names (#80)."""
        if hasattr(self.api, "search_exercises"):
            raw = self._library("search_exercises", query)
        else:
            raw = self._library("get_exercise_catalog")
        if isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict):
            entries = raw.get("exercises", [])
        else:
            entries = []
        q = query.replace(" ", "_").upper()
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or e.get("exerciseName") or "")
            if q in name.upper():
                out.append({"name": name, "category": e.get("category") or e.get("categoryKey")})
        return out

    def daily_suggestion(self, day: dt.date) -> dict | None:
        """Garmin's Daily Suggested Workout for a day, if the library exposes it (#83)."""
        fn = getattr(self.api, "get_daily_suggested_workout", None) or getattr(
            self.api, "get_workout_suggestion", None
        )
        if not callable(fn):
            return None
        try:
            return fn(day.isoformat())
        except Exception:  # purely advisory
            return None

    # --- writes ---

    def create_workout(self, payload: dict) -> int:
        result = self._call("POST", WORKOUT_CREATE, json=payload)
        workout_id = (result or {}).get("workoutId")
        if not workout_id:
            raise PushError(f"Garmin did not return a workoutId (got {result!r})")
        return int(workout_id)

    def update_workout(self, workout_id: int, payload: dict) -> None:
        """Edit in place (#78): keeps Garmin's id and anything attached to it."""
        body = dict(payload, workoutId=workout_id)
        self._call("PUT", WORKOUT_ITEM.format(workout_id=workout_id), json=body)

    def delete_workout(self, workout_id: int) -> None:
        self._call("DELETE", WORKOUT_ITEM.format(workout_id=workout_id))

    def schedule_workout(self, workout_id: int, date: str) -> None:
        self._call("POST", WORKOUT_SCHEDULE.format(workout_id=workout_id), json={"date": date})

    def push_to_device(self, workout_id: int, device_id: str) -> None:
        """Send a workout to a device now rather than at the next sync (#79)."""
        self._library("push_workout_to_device", workout_id, device_id)

    # --- the useful ones ---

    def push(
        self,
        compiled: list[CompiledWorkout],
        replace: bool = True,
        verify: bool = True,
        update_in_place: bool = True,
        device_id: str | None = None,
        log: Callable[[str], None] = lambda _: None,
    ) -> list[PushResult]:
        """Upload and schedule, skipping anything already on the calendar unchanged.

        A tagged workout whose content changed is updated in place when the
        library allows it, else replaced. Only workouts carrying our tag are
        ever touched. Anything you built by hand in Garmin Connect is left
        strictly alone.
        """
        results: list[PushResult] = []
        if not compiled:
            return results
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        existing = self.scheduled_between(min(dates), max(dates))
        by_date = _index_by_date(existing)

        for item in compiled:
            names_today = {c.name.strip() for c in compiled if c.date == item.date}
            try:
                results.append(
                    self._push_one(
                        item, by_date, names_today, replace, verify, update_in_place, device_id, log
                    )
                )
            except PushError as exc:
                results.append(PushResult(item.name, item.date, "failed", detail=str(exc)))
        return results

    def _push_one(
        self, item, by_date, names_today, replace, verify, update_in_place, device_id, log
    ) -> PushResult:
        _, want_slug, want_hash = item.tag.strip("[]").split(":")
        title_wanted = item.name.strip()
        candidates = by_date.get(item.date, [])

        # Only this plan's own sessions on this date are ever candidates. A
        # hand-made workout has no tag; another plan's has another slug.
        own: list[tuple[dict, str, str]] = []
        for candidate in candidates:
            title = (candidate.get("title") or "").strip()
            tag = parse_tag(candidate.get("description")) or parse_tag(title)
            if tag is None or tag[0] != want_slug:
                continue
            own.append((candidate, title, tag[1]))

        for candidate, title, digest in own:
            if digest == want_hash and title == title_wanted:
                # Claimed: a second session on the same day cannot take it too.
                candidates.remove(candidate)
                log(f"  unchanged  {item.date}  {item.name}")
                return PushResult(item.name, item.date, "unchanged", _id_of(candidate))

        # Stale versions on this date. One with our title is ours to update in
        # place; one titled like ANOTHER session being pushed today belongs to
        # that session and is left for it to claim.
        mine = [c for c, title, _ in own if title == title_wanted]
        loose = [c for c, title, _ in own if title != title_wanted and title not in names_today]
        stale_items = mine + loose
        for c in stale_items:
            candidates.remove(c)
        stale = [int(i) for i in (_id_of(c) for c in stale_items) if i]

        if stale and not replace:
            return PushResult(
                item.name,
                item.date,
                "failed",
                detail=f"{len(stale)} tagged workout(s) already on {item.date}; re-run with --replace to overwrite",
            )

        detail = ""
        if stale and update_in_place:
            workout_id = stale[0]
            try:
                self.update_workout(workout_id, item.payload)
                action = "updated"
                stale = stale[1:]
            except PushError as exc:
                log(f"  in-place update failed ({exc}); replacing instead")
                workout_id = self.create_workout(item.payload)
                self.schedule_workout(workout_id, item.date)
                action = "replaced"
        else:
            workout_id = self.create_workout(item.payload)
            self.schedule_workout(workout_id, item.date)
            action = "replaced" if stale else "created"

        if verify:
            detail = self._verify(workout_id, item)

        for old_id in stale:
            try:
                self.delete_workout(old_id)
            except PushError as exc:
                detail = (detail + f" (could not delete old {old_id}: {exc})").strip()

        if device_id:
            try:
                self.push_to_device(workout_id, device_id)
                detail = (detail + " sent to device").strip()
            except PushError as exc:
                detail = (detail + f" (device push failed: {exc})").strip()

        log(f"  {action:<10} {item.date}  {item.name}  (id {workout_id})")
        return PushResult(item.name, item.date, action, workout_id, detail)

    def unpush(
        self, compiled: list[CompiledWorkout], log: Callable[[str], None] = lambda _: None
    ) -> list[PushResult]:
        """Undo a push (#2): delete this plan's tagged workouts on those dates.

        The tag's plan slug is the key, so other plans' workouts on the same
        calendar survive, and hand-made workouts are never candidates.
        """
        results: list[PushResult] = []
        if not compiled:
            return results
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        slugs = {c.tag.strip("[]").split(":")[1] for c in compiled}
        wanted = {(c.date, c.name.strip()) for c in compiled}
        for item in self.scheduled_between(min(dates), max(dates)):
            tag = parse_tag(item.get("description")) or parse_tag(item.get("title") or "")
            if tag is None or tag[0] not in slugs:
                continue
            date = (item.get("date") or "")[:10]
            title = (item.get("title") or "").strip()
            if (date, title) not in wanted:
                continue
            workout_id = item.get("workoutId") or item.get("id")
            try:
                self.delete_workout(int(workout_id))
                log(f"  removed    {date}  {title}")
                results.append(PushResult(title, date, "removed", int(workout_id)))
            except (PushError, TypeError, ValueError) as exc:
                results.append(PushResult(title, date, "failed", detail=str(exc)))
        return results

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
        sent_sport = (item.payload.get("sportType") or {}).get("sportTypeKey")
        got_sport = (stored.get("sportType") or {}).get("sportTypeKey")
        if sent_sport and got_sport and sent_sport != got_sport:
            problems.append(f"sport sent {sent_sport}, stored {got_sport}")

        sent_steps = _flatten(item.payload["workoutSegments"][0]["workoutSteps"])
        got_steps = _flatten((stored.get("workoutSegments") or [{}])[0].get("workoutSteps", []))
        if len(sent_steps) != len(got_steps):
            problems.append(f"sent {len(sent_steps)} steps, Garmin kept {len(got_steps)}")

        for index, (sent, got) in enumerate(zip(sent_steps, got_steps, strict=False), start=1):
            sent_key = (sent.get("stepType") or {}).get("stepTypeKey")
            got_key = (got.get("stepType") or {}).get("stepTypeKey")
            if sent_key != got_key:
                problems.append(f"step {index}: sent {sent_key}, stored {got_key}")
            sent_end = (sent.get("endCondition") or {}).get("conditionTypeKey")
            got_end = (got.get("endCondition") or {}).get("conditionTypeKey")
            if sent_end and got_end and sent_end != got_end:
                problems.append(f"step {index}: end condition sent {sent_end}, stored {got_end}")
            a, b = sent.get("endConditionValue"), got.get("endConditionValue")
            if a is not None and b is not None and abs(float(a) - float(b)) > 0.5:
                problems.append(f"step {index}: end value changed {a} -> {b}")
            for key, label in (
                ("targetValueOne", "target value"),
                ("targetValueTwo", "second target value"),
            ):
                one, two = sent.get(key), got.get(key)
                if one is not None and two is not None and abs(one - two) > 0.05:
                    problems.append(
                        f"step {index}: {label} changed {one} -> {two} "
                        "(Garmin may order pace bounds the other way round)"
                    )
            if sent.get("type") == "RepeatGroupDTO" and sent.get("numberOfIterations") != got.get(
                "numberOfIterations"
            ):
                problems.append(
                    f"step {index}: repeat count sent {sent.get('numberOfIterations')}, "
                    f"stored {got.get('numberOfIterations')}"
                )
            if sent.get("exerciseName") and got.get("exerciseName") != sent.get("exerciseName"):
                problems.append(
                    f"step {index}: exercise {sent['exerciseName']} not recognised by Garmin's catalog"
                )
        return "; ".join(problems)


def _id_of(item: dict) -> int | None:
    raw = item.get("workoutId") or item.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


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
    fn = getattr(response, "json", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            return None
    return None
