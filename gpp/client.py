"""The Garmin Connect push layer.

Everything that touches the network lives here, deliberately quarantined from
the compiler so the interesting logic stays testable offline.

A note on how this talks to Garmin
----------------------------------
`python-garminconnect` is a reverse-engineered client and its internal
transport has changed shape more than once (garth session, then a mobile SSO
flow). This module is written against the 0.3.x line, where the authenticated
transport is `Garmin.client.request(method, domain, path, **kw)`. That is
checked explicitly at connect time, and `tests/test_garmin_contract.py` drives
a real `Garmin` object over a stub HTTP session, so a library release that
moves it fails a test instead of every push.

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
import hashlib
import inspect
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
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
log = logging.getLogger("gpp.client")


# Where garminconnect keeps the login it saves after a successful sign-in, so
# the next run can skip the password and the two-factor code. One folder per
# Garmin account, under gpp's own subfolder: the library signs in with any
# login it finds and ignores the email and password it was given, so two
# athletes sharing a folder would both push to whoever signed in first.
DEFAULT_TOKEN_DIR = "~/.garminconnect"  # noqa: S105 - a folder, not a secret
ACCOUNTS_DIR = "gpp"
TOKEN_FILE = "garmin_tokens.json"  # noqa: S105 - a file name


class PushError(RuntimeError):
    """Something went wrong talking to Garmin Connect."""


class NeedsPassword(PushError):
    """No usable saved login, and no password to sign in with."""


def token_dir(folder: str | None = None, email: str | None = None) -> Path:
    """The folder a login is saved in: `folder` as given, else this account's own.

    Without either, the folder that holds every account's.
    """
    if folder:
        return Path(folder).expanduser()
    accounts = Path(DEFAULT_TOKEN_DIR).expanduser() / ACCOUNTS_DIR
    if not email:
        return accounts
    return accounts / hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def saved_login(folder: str | None = None, email: str | None = None) -> Path | None:
    """The login saved for this account (or in `folder`), or None when there is none yet."""
    if not folder and not email:
        return None
    path = token_dir(folder, email) / TOKEN_FILE
    return path if path.is_file() else None


def saved_logins() -> list[Path]:
    """Every login gpp has saved on this computer, one per Garmin account."""
    return sorted(p for p in token_dir().glob(f"*/{TOKEN_FILE}") if p.is_file())


def forget_login(folder: str | None = None) -> bool:
    """Sign out: delete the login in `folder`, else every login gpp saved here.

    That includes the single login 0.2.1 kept straight in ~/.garminconnect,
    which nothing reads any more. True when there was anything to delete.
    """
    if folder:
        paths = [token_dir(folder) / TOKEN_FILE]
    else:
        paths = [*saved_logins(), Path(DEFAULT_TOKEN_DIR).expanduser() / TOKEN_FILE]
    removed = False
    for path in paths:
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def sign_in(
    email: str,
    password: str | None,
    token_folder: str | None = None,
    prompt_mfa: Callable[[], str] | None = None,
    ask_password: Callable[[], str] | None = None,
) -> GarminClient:
    """Connect, preferring the saved login; ask for the password only when needed.

    With a saved login the password is not asked for at all. If that login
    has expired, `ask_password` is called once and the sign-in retried; with
    no `ask_password` (the web app) the NeedsPassword error goes back to the
    caller, which tells the user to type it.
    """
    if not password and saved_login(token_folder, email) is None and ask_password is not None:
        password = ask_password()
    client = GarminClient(email, password or None, token_dir=token_folder)
    try:
        client.connect(prompt_mfa=prompt_mfa)
    except NeedsPassword:
        if ask_password is None:
            raise
        client = GarminClient(email, ask_password() or None, token_dir=token_folder)
        client.connect(prompt_mfa=prompt_mfa)
    return client


def sign_in_at_terminal(email: str | None, token_folder: str | None = None) -> GarminClient:
    """The command-line sign-in: email from the flag, GARMIN_EMAIL or a prompt;
    password from GARMIN_PASSWORD, or asked for only when there is no usable
    saved login; the two-factor code asked for only if Garmin wants one."""
    import getpass
    import os

    email = email or os.environ.get("GARMIN_EMAIL") or input("Garmin Connect email: ").strip()
    return sign_in(
        email,
        os.environ.get("GARMIN_PASSWORD"),
        token_folder,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
        ask_password=lambda: getpass.getpass("Garmin Connect password (not stored): "),
    )


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


# Local file errors are the token store's, not the network's.
_LOCAL_ERRORS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def _unreachable(exc: BaseException) -> str | None:
    """What to tell the user when a failed sign-in was really Garmin being
    unreachable or refusing for now, or None when it was the login itself.

    Walks the exception chain: garminconnect wraps a dropped connection or a
    5xx while loading the profile as an authentication error. Both requests'
    and curl_cffi's network errors are OSErrors; an HTTP error carries a
    response with a status instead.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        text = str(current)
        if (
            type(current).__name__ == "GarminConnectTooManyRequestsError"
            or status == 429
            or "API Error 429" in text
        ):
            return "Garmin Connect is limiting sign-ins right now; wait a few minutes and try again"
        if (isinstance(status, int) and status >= 500) or re.search(r"API Error 5\d\d", text):
            return "Garmin Connect is having trouble right now; try again in a few minutes"
        if (
            isinstance(current, OSError)
            and status is None
            and not isinstance(current, _LOCAL_ERRORS)
        ):
            return "could not reach Garmin Connect; check the internet connection and try again"
        current = current.__cause__ or current.__context__
    return None


class GarminClient:
    """Thin adapter over python-garminconnect with an explicit transport probe."""

    def __init__(self, email: str, password: str | None = None, token_dir: str | None = None):
        self.email = email
        self._password = password
        self._token_dir = token_dir
        self._api: Any = None
        self._request: Callable[..., Any] | None = None
        self.transport: str = "unconnected"
        # Whose calendar this is, as Garmin names the account once signed in.
        self.account: str = ""

    # --- connection ---

    def connect(self, prompt_mfa: Callable[[], str] | None = None) -> None:
        """Sign in, from the saved login when there is one.

        garminconnect loads the token file, refreshes it when it is close to
        expiry, and falls back to the password when Garmin rejects it. After a
        password sign-in it writes the new token owner-only (0600 in a 0700
        folder) and refuses symlinked paths, so the next run needs neither the
        password nor a two-factor code.
        """
        try:
            from garminconnect import Garmin
        except ImportError as exc:  # pragma: no cover - env dependent
            from . import REINSTALL

            raise PushError(f"python-garminconnect is missing; reinstall gpp: {REINSTALL}") from exc

        kwargs: dict[str, Any] = {}
        if prompt_mfa is not None:
            # Checked, not probed: silently dropping the MFA prompt would turn
            # a two-factor account into a login that cannot finish.
            if "prompt_mfa" not in inspect.signature(Garmin).parameters:
                raise PushError(
                    "this version of python-garminconnect cannot ask for a two-factor "
                    "code; gpp needs garminconnect 0.3.x"
                )
            kwargs["prompt_mfa"] = prompt_mfa
        try:
            self._api = Garmin(self.email, self._password, **kwargs)
        except Exception as exc:
            raise PushError(f"could not set up the Garmin client: {exc}") from exc

        store = token_dir(self._token_dir, self.email)
        had_saved = saved_login(self._token_dir, self.email) is not None
        try:
            self._api.login(str(store))
        except Exception as exc:
            # The library reports "cannot reach Garmin" as a failed sign-in
            # too; asking for the password then only sends someone through a
            # full sign-in (and a two-factor code) that cannot work either.
            trouble = _unreachable(exc)
            if trouble:
                raise PushError(trouble) from exc
            if not self._password:
                raise NeedsPassword(
                    "your Garmin sign-in has expired; enter your Garmin password to sign in again"
                    if had_saved
                    else "no saved Garmin sign-in yet; enter your Garmin password to sign in"
                ) from exc
            raise PushError(f"Garmin login failed: {exc}") from exc
        finally:
            self._password = None  # never kept past the sign-in

        self._request = self._resolve_transport()
        name = getattr(self._api, "full_name", None) or getattr(self._api, "display_name", None)
        self.account = name if isinstance(name, str) else ""

    @property
    def api(self) -> Any:
        """The underlying library client, for the read-side (sync.py)."""
        if self._api is None:
            raise PushError("not connected; call connect() first")
        return self._api

    def _resolve_transport(self) -> Callable[..., Any]:
        """The authenticated request method of garminconnect 0.3.x.

        `Garmin.connectapi` is GET-only in 0.3.x (it fixes the method and
        rejects a `method=` keyword), so writes go through the lower-level
        client, which takes the method as its first argument.
        """
        request = getattr(getattr(self._api, "client", None), "request", None)
        if not callable(request):
            raise PushError(
                "this version of python-garminconnect has no client.request(); "
                "gpp needs garminconnect 0.3.x. Reinstall gpp to get a supported version."
            )
        self.transport = "Garmin.client.request"

        def _via_client(method: str, path: str, **kw: Any) -> Any:
            return _json_or_none(request(method, "connectapi", path, **kw))

        return _via_client

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._request is None:
            raise PushError("not connected; call connect() first")
        delays = RETRY_DELAYS if method == "GET" else ()
        for delay in (*delays, None):
            try:
                result = self._request(method, path, **kwargs)
                log.debug("%s %s ok", method, path)
                return result
            except Exception as exc:
                log.debug("%s %s failed: %s", method, path, exc)
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
            raise PushError(f"this version of python-garminconnect has no {name}(); run gpp doctor")
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

    def _match(self, item, by_date, names_today) -> tuple[dict | None, list[dict]]:
        """This plan's own sessions on the item's date: the exact match, and
        the stale versions this item may replace. Matched rows are consumed so
        a second session on the same day cannot claim them.

        Only rows tagged with this plan's slug are ever candidates: a hand-made
        workout has no tag, another plan's has another slug.
        """
        _, want_slug, want_hash = item.tag.strip("[]").split(":")
        title_wanted = item.name.strip()
        candidates = by_date.get(item.date, [])
        own: list[tuple[dict, str, str]] = []
        legacy: list[dict] = []
        for candidate in candidates:
            title = (candidate.get("title") or "").strip()
            tag = parse_tag(candidate.get("description")) or parse_tag(title)
            if tag is None:
                continue
            if tag[0] == want_slug:
                own.append((candidate, title, tag[1]))
            elif tag[0] == item.legacy_slug and title == title_wanted:
                # Pushed by 0.2.1 or earlier, whose slug other plans can share:
                # only the same session on the same day is taken as this one,
                # and updating it moves it to this plan's tag.
                legacy.append(candidate)
        for candidate, title, digest in own:
            if digest == want_hash and title == title_wanted:
                candidates.remove(candidate)
                return candidate, []
        # A stale row with our title is ours to update; one titled like
        # ANOTHER session being pushed today belongs to that session.
        mine = [c for c, title, _ in own if title == title_wanted] + legacy
        loose = [c for c, title, _ in own if title != title_wanted and title not in names_today]
        stale = mine + loose
        for c in stale:
            candidates.remove(c)
        return None, stale

    def _push_one(
        self, item, by_date, names_today, replace, verify, update_in_place, device_id, log
    ) -> PushResult:
        matched, stale_items = self._match(item, by_date, names_today)
        if matched is not None:
            log(f"  unchanged  {item.date}  {item.name}")
            return PushResult(item.name, item.date, "unchanged", _workout_id(matched))
        stale = [i for i in (_workout_id(c) for c in stale_items) if i]

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

    def preview(self, compiled: list[CompiledWorkout]) -> list[PushResult]:
        """A live dry run (#154): what a push would do, from the calendar, writing nothing."""
        results: list[PushResult] = []
        if not compiled:
            return results
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        by_date = _index_by_date(self.scheduled_between(min(dates), max(dates)))
        for item in compiled:
            names_today = {c.name.strip() for c in compiled if c.date == item.date}
            matched, stale_items = self._match(item, by_date, names_today)
            if matched is not None:
                results.append(PushResult(item.name, item.date, "unchanged", _workout_id(matched)))
                continue
            ids = [i for i in (_workout_id(c) for c in stale_items) if i]
            if ids:
                extra = f", delete {ids[1:]}" if len(ids) > 1 else ""
                results.append(
                    PushResult(
                        item.name,
                        item.date,
                        "would-update",
                        ids[0],
                        f"update {ids[0]} in place{extra}",
                    )
                )
            else:
                results.append(PushResult(item.name, item.date, "would-create"))
        return results

    def conflicts(self, compiled: list[CompiledWorkout]) -> list[dict]:
        """Other things already on the plan's dates (#145): hand-made workouts,
        other plans, Garmin Coach sessions. Listed, never touched."""
        if not compiled:
            return []
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        slugs = {c.tag.strip("[]").split(":")[1] for c in compiled}
        legacy = {c.legacy_slug for c in compiled}
        sessions = {(c.date, c.name.strip()) for c in compiled}
        wanted = {c.date for c in compiled}
        out = []
        for item in self.scheduled_between(min(dates), max(dates)):
            date = (item.get("date") or "")[:10]
            if date not in wanted:
                continue
            title = (item.get("title") or "").strip() or "(untitled)"
            tag = parse_tag(item.get("description")) or parse_tag(title)
            if tag is not None and (
                tag[0] in slugs or (tag[0] in legacy and (date, title) in sessions)
            ):
                continue
            if tag is not None:
                source = f"another gpp plan ({tag[0]})"
            elif item.get("trainingPlanId") or item.get("trainingPlanPk"):
                source = "a Garmin training plan"
            else:
                source = "hand-made or synced"
            out.append({"date": date, "title": title, "source": source, "id": _id_of(item)})
        return out

    def orphans(
        self,
        compiled: list[CompiledWorkout],
        margin_days: int = 28,
        today: dt.date | None = None,
    ) -> list[dict]:
        """This plan's own workouts on dates the plan no longer uses.

        Matching is per date, so a session moved to another day is pushed
        fresh and its old copy stays behind. These are those copies: tagged
        with this plan's slug, on a date with no session in the plan, from
        today on and within `margin_days` either side of the plan. Sessions
        already past are history, never offered for removal. Listed; the
        caller removes them with `unpush_ids` when asked to prune.
        """
        if not compiled:
            return []
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        slugs = {c.tag.strip("[]").split(":")[1] for c in compiled}
        wanted = {c.date for c in compiled}
        margin = dt.timedelta(days=margin_days)
        start = max(min(dates) - margin, today or dt.date.today())
        end = max(dates) + margin
        if start > end:
            return []
        out = []
        for item in self.scheduled_between(start, end):
            date = (item.get("date") or "")[:10]
            tag = parse_tag(item.get("description")) or parse_tag(item.get("title") or "")
            if tag is None or tag[0] not in slugs or date in wanted or date < start.isoformat():
                continue
            out.append(
                {
                    "date": date,
                    "title": (item.get("title") or "").strip(),
                    "workout_id": _workout_id(item),
                }
            )
        return out

    def unpush_ids(
        self, ids: list[int], log: Callable[[str], None] = lambda _: None
    ) -> list[PushResult]:
        """Delete exactly these workouts, from a push receipt (#148)."""
        results = []
        for workout_id in ids:
            try:
                self.delete_workout(int(workout_id))
                log(f"  removed    id {workout_id}")
                results.append(PushResult(str(workout_id), "", "removed", int(workout_id)))
            except PushError as exc:
                results.append(PushResult(str(workout_id), "", "failed", int(workout_id), str(exc)))
        return results

    def remove_orphans(
        self, orphans: list[dict], log: Callable[[str], None] = lambda _: None
    ) -> list[PushResult]:
        """Delete the sessions `orphans()` listed, reporting each by date and title."""
        results = []
        for old in orphans:
            title, date, workout_id = old["title"], old["date"], old.get("workout_id")
            if not workout_id:
                results.append(PushResult(title, date, "failed", detail="no workout id on it"))
                continue
            try:
                self.delete_workout(int(workout_id))
                log(f"  removed    {date}  {title}")
                results.append(PushResult(title, date, "removed", int(workout_id)))
            except PushError as exc:
                results.append(PushResult(title, date, "failed", int(workout_id), str(exc)))
        return results

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
        # With the old slug too: the date and title still have to match.
        slugs = {c.tag.strip("[]").split(":")[1] for c in compiled}
        slugs |= {c.legacy_slug for c in compiled}
        wanted = {(c.date, c.name.strip()) for c in compiled}
        for item in self.scheduled_between(min(dates), max(dates)):
            tag = parse_tag(item.get("description")) or parse_tag(item.get("title") or "")
            if tag is None or tag[0] not in slugs:
                continue
            date = (item.get("date") or "")[:10]
            title = (item.get("title") or "").strip()
            if (date, title) not in wanted:
                continue
            workout_id = _workout_id(item)
            if workout_id is None:
                # A calendar item's own `id` is its schedule entry, not the
                # workout; deleting by it would hit the wrong object.
                results.append(PushResult(title, date, "failed", detail="no workout id on it"))
                continue
            try:
                self.delete_workout(workout_id)
                log(f"  removed    {date}  {title}")
                results.append(PushResult(title, date, "removed", workout_id))
            except PushError as exc:
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


def _workout_id(item: dict) -> int | None:
    """The workout behind a calendar item. Never the item's own `id`, which is
    the schedule entry: updating or deleting by it hits the wrong object."""
    try:
        return int(item["workoutId"]) if item.get("workoutId") is not None else None
    except (TypeError, ValueError):
        return None


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
