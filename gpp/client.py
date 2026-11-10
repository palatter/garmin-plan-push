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

How a push recognises its own earlier work
------------------------------------------
Every workout gpp makes carries a tag, `[gpp:<plan>:<content hash>]`, at the
start of its description. A push lists the calendar and looks for that tag
on the plan's dates: same tag and title, nothing to do; same plan, other
hash, update in place; no tag, not ours, never touched.

Whether the calendar listing includes a workout's description has not been
confirmed against the live service, so nothing depends on it. When no
workout in the listing has one, each workout on the dates in question is
read by its id (`GET /workout-service/workout/{id}`, the same call the
read-back uses) and the tag taken from there. The push receipts are a
second witness: a calendar entry that cannot be read, but whose id an
earlier push of this plan recorded, is known to be ours, and its session is
held back instead of being sent twice. One that cannot be read and is in no
receipt cannot be ruled out as a copy: the session is sent, and its result
(and so the receipt) says duplicates were not ruled out.

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

from . import keychain
from .compile import CompiledWorkout
from .constants import TAG_PREFIX
from .receipts import Earlier

WORKOUT_CREATE = "/workout-service/workout"
WORKOUT_ITEM = "/workout-service/workout/{workout_id}"
WORKOUT_SCHEDULE = "/workout-service/schedule/{workout_id}"
CALENDAR_MONTH = "/calendar-service/year/{year}/month/{month}"

TAG_RE = re.compile(rf"\[{TAG_PREFIX}:([a-z0-9]+):([0-9a-f]{{8}})\]")

# HTTP statuses worth one more try on a read; writes are never retried blind.
TRANSIENT = ("429", "502", "503", "504", "timed out", "timeout")
RETRY_DELAYS = (1.0, 2.0, 4.0)
log = logging.getLogger("gpp.client")
_log = log  # push() and friends take a `log` callback that hides the name above


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


class LoginRejected(PushError):
    """Garmin turned the password sign-in down."""


class Unscheduled(PushError):
    """The workout exists at Garmin but could not be put on the calendar."""

    def __init__(self, workout_id: int, reason: str):
        super().__init__(
            f"workout {workout_id} was created but could not be put on the calendar "
            f"({reason}); push again to schedule it"
        )
        self.workout_id = workout_id


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
    """Sign out: delete the login in `folder`, else every login gpp saved here
    and every Garmin password in the keychain.

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
    if not folder:
        try:
            removed = keychain.forget_garmin_passwords() > 0 or removed
        except keychain.KeychainError as exc:
            log.warning("the saved Garmin passwords could not be removed: %s", exc)
    return removed


# An empty answer to the password prompt: easy to give by accident, since
# the password does not show as it is typed.
EMPTY_ANSWER = (
    "no password was entered; type your Garmin password and press Enter "
    "(nothing shows as you type it)"
)


def sign_in(
    email: str,
    password: str | None,
    token_folder: str | None = None,
    prompt_mfa: Callable[[], str] | None = None,
    ask_password: Callable[[], str] | None = None,
    remember: bool = False,
) -> GarminClient:
    """Connect, preferring the saved login; ask for the password only when needed.

    With a saved login the password is not asked for at all. When that login
    has expired, or there is none yet, the password saved in the OS keychain
    signs in; only without one, or when Garmin turns it down, is
    `ask_password` called. With no `ask_password` (the web app) the
    NeedsPassword error goes back to the caller, which tells the user to
    type it. With `remember`, a password typed for this sign-in (`password`,
    or what `ask_password` returns) goes into the keychain once Garmin has
    accepted it; `client.remembered` says whether it did.
    """
    typed = password or None
    stored = None if typed else keychain.garmin_password(email)
    if not (typed or stored) and saved_login(token_folder, email) is None and ask_password:
        typed = ask_password() or None
        if typed is None:  # with nothing saved, there is nothing to try
            raise NeedsPassword(EMPTY_ANSWER)
    client = GarminClient(email, typed or stored, token_dir=token_folder)
    try:
        client.connect(prompt_mfa=prompt_mfa)
    except (NeedsPassword, LoginRejected) as exc:
        # Asking again only helps when nothing was typed yet: there was no
        # password at all, or only the keychain's, which Garmin refused
        # (changed since it was saved, most likely).
        if typed or (isinstance(exc, LoginRejected) and not stored):
            raise
        if ask_password is None:
            if isinstance(exc, NeedsPassword):
                raise
            raise NeedsPassword(
                "Garmin did not accept the password saved on this computer; "
                "enter your Garmin password"
            ) from exc
        typed = ask_password() or None
        if typed is None:
            raise NeedsPassword(EMPTY_ANSWER) from exc
        client = GarminClient(email, typed, token_dir=token_folder)
        client.connect(prompt_mfa=prompt_mfa)
    if remember and typed:
        try:
            keychain.save_garmin_password(email, typed)
            client.remembered = True
        except keychain.KeychainError as exc:
            log.info("the Garmin password was not saved: %s", exc)
            client.remembered = False
    return client


def sign_in_at_terminal(email: str | None, token_folder: str | None = None) -> GarminClient:
    """The command-line sign-in: email from the flag, GARMIN_EMAIL or a prompt;
    password from GARMIN_PASSWORD, or asked for only when there is no usable
    saved login; the two-factor code asked for only if Garmin wants one."""
    import getpass
    import os
    import sys

    email = email or os.environ.get("GARMIN_EMAIL") or input("Garmin Connect email: ").strip()
    from_env = os.environ.get("GARMIN_PASSWORD")
    kept = f"kept in {keychain.where()}" if keychain.available() else "not stored"
    # With no terminal (a scheduled task, a pipe) nobody can answer: on
    # Windows the prompt would wait on the console forever. Say so instead.
    terminal = sys.stdin is not None and sys.stdin.isatty()
    try:
        return sign_in(
            email,
            from_env,
            token_folder,
            prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
            ask_password=(
                (lambda: getpass.getpass(f"Garmin Connect password ({kept}): "))
                if terminal
                else None
            ),
            remember=not from_env,
        )
    except NeedsPassword as exc:
        if terminal:
            raise
        what = str(exc).split(";")[0]
        raise NeedsPassword(
            f"{what}; there is no terminal here to ask for the password, "
            "so run gpp in one once to sign in"
        ) from exc


@dataclass
class PushResult:
    name: str
    date: str
    action: str  # created | replaced | updated | unchanged | removed | failed | not-sent
    workout_id: int | None = None
    detail: str = ""
    # Failed, but the workout exists at Garmin, off the calendar: the next
    # push schedules it instead of creating another.
    unscheduled: bool = False
    # Sent, though something else on that date could not be read: it may be
    # an earlier copy of this session.
    duplicates_unchecked: bool = False

    def to_dict(self) -> dict:
        row = {
            "name": self.name,
            "date": self.date,
            "action": self.action,
            "workout_id": self.workout_id,
            "detail": self.detail,
        }
        if self.unscheduled:
            row["unscheduled"] = True
        if self.duplicates_unchecked:
            row["duplicates_unchecked"] = True
        return row


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
        # Whether sign_in() saved a typed password in the keychain (None: no
        # password was typed, or it was not asked to).
        self.remembered: bool | None = None
        # What this plan's push receipts recorded (receipts.earlier_pushes);
        # the caller sets it before a push. Empty is always safe.
        self.earlier = Earlier()
        # Tags read from workouts one by one, when the calendar did not say.
        self._tags: dict[int, tuple[str, str] | None] = {}
        self._unreadable: set[int] = set()
        self._read_each = False

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
            raise LoginRejected(f"Garmin login failed: {exc}") from exc
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

    def _calendar(self, start: dt.date, end: dt.date) -> list[dict]:
        """The calendar items, and whether their tags have to be read one by one.

        If any workout in the listing carries a description, the listing
        carries descriptions, and a workout without one has none. If none
        does, that proves nothing either way, so `_tag_of` asks Garmin for
        each workout it needs to know about.
        """
        items = self.scheduled_between(start, end)
        workouts = [i for i in items if _workout_id(i) is not None]
        self._read_each = bool(workouts) and not any(_described(i) for i in workouts)
        if self._read_each:
            log.debug(
                "no workout in the calendar listing has a description: "
                "reading each one to find gpp's tag (%d on the calendar)",
                len(workouts),
            )
        return items

    def _tag_of(self, item: dict) -> tuple[str, str] | None:
        """(plan slug, content hash) of a calendar item that is one of ours.

        From the listing when it is there; otherwise from the workout itself,
        when the listing has no descriptions at all, or when a receipt says
        this id is ours (a long note may have pushed the tag out of a
        shortened description).
        """
        tag = parse_tag(item.get("description")) or parse_tag(item.get("title") or "")
        if tag is not None:
            return tag
        workout_id = _workout_id(item)
        if workout_id is None:
            return None
        if workout_id in self.earlier.ids or (self._read_each and not _described(item)):
            return self._stored_tag(workout_id)
        return None

    def _stored_tag(self, workout_id: int) -> tuple[str, str] | None:
        if workout_id not in self._tags:
            tag = None
            try:
                stored = self.get_workout(workout_id)
                if not isinstance(stored, dict):
                    raise PushError("empty reply")
                tag = parse_tag(stored.get("description"))
            except PushError as exc:
                log.debug("could not read workout %s for its tag: %s", workout_id, exc)
                self._unreadable.add(workout_id)
            self._tags[workout_id] = tag
        return self._tags[workout_id]

    def _unknown_on(
        self, by_date: dict[str, list[dict]], item: CompiledWorkout
    ) -> tuple[list[int], bool]:
        """What is left unclaimed on the item's date that might be a copy of it.

        First, workouts that could not be read but that a receipt of this
        plan names: ours, for all we can tell. Second, whether anything else
        there leaves the question open: a workout that could not be read and
        is in no receipt, or an entry with this session's title and no
        workout id to read it by.
        """
        ours: set[int] = set()
        open_question = False
        for candidate in by_date.get(item.date, []):
            workout_id = _workout_id(candidate)
            if workout_id is None:
                open_question = open_question or (
                    candidate.get("itemType") in (None, "workout")
                    and (candidate.get("title") or "").strip() == item.name.strip()
                    and self._tag_of(candidate) is None
                )
            elif workout_id in self._unreadable:
                if workout_id in self.earlier.ids:
                    ours.add(workout_id)
                else:
                    open_question = True
        return sorted(ours), open_question

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
        results: list[PushResult] | None = None,
    ) -> list[PushResult]:
        """Upload and schedule, skipping anything already on the calendar unchanged.

        A tagged workout whose content changed is updated in place when the
        library allows it, else replaced. Only workouts carrying our tag are
        ever touched. Anything you built by hand in Garmin Connect is left
        strictly alone.

        One result per session, always: an error in one session, expected or
        not, is that session's result and the rest still go. Pass `results`
        to keep what was done when the push itself is cut short (Ctrl+C):
        the list is filled as it goes, and the sessions never reached are
        added as `not-sent` before the interruption is passed on.
        """
        results = [] if results is None else results
        if not compiled:
            return results
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        by_date = _index_by_date(self._calendar(min(dates), max(dates)))

        for index, item in enumerate(compiled):
            names_today = {c.name.strip() for c in compiled if c.date == item.date}
            try:
                results.append(
                    self._push_one(
                        item, by_date, names_today, replace, verify, update_in_place, device_id, log
                    )
                )
            except Unscheduled as exc:
                results.append(
                    PushResult(
                        item.name, item.date, "failed", exc.workout_id, str(exc), unscheduled=True
                    )
                )
            except PushError as exc:
                results.append(PushResult(item.name, item.date, "failed", detail=str(exc)))
            except Exception as exc:
                # A reply in a shape nobody expected, a bug: this session
                # failed, the others are still sent, and all of it is recorded.
                _log.exception("unexpected error pushing %s %s", item.date, item.name)
                results.append(
                    PushResult(
                        item.name,
                        item.date,
                        "failed",
                        detail=f"unexpected error ({type(exc).__name__}: {exc}); "
                        "check this date on the calendar before pushing again",
                    )
                )
            except BaseException:
                results.append(
                    PushResult(
                        item.name,
                        item.date,
                        "failed",
                        detail="interrupted part-way; check this date on the calendar",
                    )
                )
                results.extend(
                    PushResult(c.name, c.date, "not-sent", detail="the push stopped before it")
                    for c in compiled[index + 1 :]
                )
                raise
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
            tag = self._tag_of(candidate)
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
            return PushResult(item.name, item.date, "failed", detail=_kept(len(stale), item.date))

        detail = ""
        unsure = False
        if stale and update_in_place:
            workout_id = stale[0]
            try:
                self.update_workout(workout_id, item.payload)
                action = "updated"
                stale = stale[1:]
            except PushError as exc:
                log(f"  in-place update failed ({exc}); replacing instead")
                workout_id = self._create_scheduled(item, log)
                action = "replaced"
        else:
            if not stale:
                ours, unsure = self._unknown_on(by_date, item)
                if ours:
                    raise PushError(
                        f"could not read workout {ours[0]} on {item.date}, which an earlier "
                        "push of this plan put there, so this session was not sent "
                        "(it may be on the calendar already); push again"
                    )
            workout_id = self._create_scheduled(item, log)
            action = "replaced" if stale else "created"

        if verify:
            try:
                detail = self._verify(workout_id, item)
            except Exception as exc:  # the workout is sent; only the check failed
                detail = f"could not verify: {type(exc).__name__}: {exc}"
        if unsure:
            detail = "; ".join(filter(None, [detail, _not_ruled_out(item.date)]))

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
        return PushResult(
            item.name, item.date, action, workout_id, detail, duplicates_unchecked=unsure
        )

    def _create_scheduled(self, item: CompiledWorkout, log: Callable[[str], None]) -> int:
        """A workout for `item`, on the calendar; its id.

        The two calls are separate at Garmin. When the second fails the
        workout stays in the library, off the calendar, where no later push
        can see it: the error carries its id into the result and the
        receipt, and the next push picks it up here instead of creating
        another.
        """
        workout_id = self._waiting(item, log)
        if workout_id is None:
            workout_id = self.create_workout(item.payload)
        try:
            self.schedule_workout(workout_id, item.date)
        except PushError as exc:
            raise Unscheduled(workout_id, str(exc).split(" failed: ", 1)[-1]) from exc
        return workout_id

    def _waiting(self, item: CompiledWorkout, log: Callable[[str], None]) -> int | None:
        """The workout an earlier push created for this session and could not
        schedule, brought up to date, or None when there is none to use.

        The receipt only says where to look. The workout is read back and
        used only if Garmin still has it and it carries this plan's tag.
        """
        workout_id = self.earlier.unscheduled.get((item.date, item.name.strip()))
        if workout_id is None:
            return None
        try:
            stored = self.get_workout(workout_id)
        except PushError as exc:
            if _gone(exc):
                return None  # deleted by hand since: start again
            raise PushError(
                f"could not read workout {workout_id}, which an earlier push created for "
                f"this session and could not schedule ({exc}); not sent this time, push again"
            ) from exc
        tag = parse_tag(stored.get("description")) if isinstance(stored, dict) else None
        _, want_slug, want_hash = item.tag.strip("[]").split(":")
        if tag is None or tag[0] != want_slug:
            return None
        if tag[1] != want_hash:
            self.update_workout(workout_id, item.payload)
        log(f"  scheduling the workout an earlier push created (id {workout_id})")
        return workout_id

    def preview(self, compiled: list[CompiledWorkout], replace: bool = True) -> list[PushResult]:
        """A live dry run (#154): what a push would do, from the calendar, writing nothing."""
        results: list[PushResult] = []
        if not compiled:
            return results
        dates = [dt.date.fromisoformat(c.date) for c in compiled]
        by_date = _index_by_date(self._calendar(min(dates), max(dates)))
        for item in compiled:
            names_today = {c.name.strip() for c in compiled if c.date == item.date}
            matched, stale_items = self._match(item, by_date, names_today)
            if matched is not None:
                results.append(PushResult(item.name, item.date, "unchanged", _workout_id(matched)))
                continue
            ids = [i for i in (_workout_id(c) for c in stale_items) if i]
            if ids and not replace:
                results.append(
                    PushResult(
                        item.name, item.date, "would-fail", ids[0], _kept(len(ids), item.date)
                    )
                )
            elif ids:
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
            elif (item.date, item.name.strip()) in self.earlier.unscheduled:
                waiting = self.earlier.unscheduled[(item.date, item.name.strip())]
                results.append(
                    PushResult(
                        item.name,
                        item.date,
                        "would-create",
                        waiting,
                        f"schedule workout {waiting}, which an earlier push created",
                    )
                )
            else:
                _, unread = self._unknown_on(by_date, item)
                results.append(
                    PushResult(
                        item.name,
                        item.date,
                        "would-create",
                        detail=_not_ruled_out(item.date) if unread else "",
                        duplicates_unchecked=unread,
                    )
                )
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
        for item in self._calendar(min(dates), max(dates)):
            date = (item.get("date") or "")[:10]
            if date not in wanted:
                continue
            title = (item.get("title") or "").strip() or "(untitled)"
            tag = self._tag_of(item)
            if tag is not None and (
                tag[0] in slugs or (tag[0] in legacy and (date, title) in sessions)
            ):
                continue
            if tag is not None:
                source = f"another gpp plan ({tag[0]})"
            elif item.get("trainingPlanId") or item.get("trainingPlanPk"):
                source = "a Garmin training plan"
            elif _workout_id(item) in self._unreadable:
                source = "could not be read"
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
        for item in self._calendar(start, end):
            date = (item.get("date") or "")[:10]
            if date in wanted or date < start.isoformat():
                continue
            tag = self._tag_of(item)
            if tag is None or tag[0] not in slugs:
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
        for item in self._calendar(min(dates), max(dates)):
            date = (item.get("date") or "")[:10]
            title = (item.get("title") or "").strip()
            if (date, title) not in wanted:
                continue
            tag = self._tag_of(item)
            if tag is None or tag[0] not in slugs:
                if _workout_id(item) in self._unreadable:
                    results.append(
                        PushResult(
                            title,
                            date,
                            "failed",
                            detail="could not be read, so it was left: gpp only removes "
                            "workouts it can see are its own",
                        )
                    )
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
        if not isinstance(stored, dict):
            return "could not verify: Garmin sent back nothing to compare"

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


def _kept(count: int, date: str) -> str:
    """Why a session was not sent: the command line turns replacing off with
    --no-replace, the page with its Replace box."""
    return f"{count} earlier version(s) already on {date}, and replacing is turned off"


def _described(item: dict) -> bool:
    description = item.get("description")
    return isinstance(description, str) and bool(description.strip())


def _gone(exc: BaseException) -> bool:
    """Whether a failed read says the thing no longer exists (HTTP 404).

    Read from the library's own error, never from our message: that one
    holds the path, and a workout id can contain "404".
    """
    cause = exc.__cause__
    if cause is None:
        return False
    status = getattr(getattr(cause, "response", None), "status_code", None)
    return (
        status == 404
        or "NotFound" in type(cause).__name__
        or re.search(r"\bAPI Error 404\b", str(cause)) is not None
    )


def _not_ruled_out(date: str) -> str:
    return (
        f"something else on {date} could not be identified, so an earlier copy of this "
        "session was not ruled out: check that date for a duplicate"
    )


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
