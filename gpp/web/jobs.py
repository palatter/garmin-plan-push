"""A tiny background-job registry for the local web UI.

Generating a plan takes tens of seconds and pushing to Garmin can pause
mid-way to ask for an MFA code. Both are wrong to do inside a request handler,
so they run on worker threads and the UI polls for progress.

The MFA case is why this exists in the shape it does: `garminconnect` asks for
the code through a *blocking callback*. The worker parks on an Event, the job
flips to `awaiting_input`, the UI collects the code and posts it back, and the
callback returns. No polling of Garmin, no re-login, no code in a config file.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# Jobs are kept so the UI can read the final result, but a long-lived browser
# tab should not grow the registry without bound.
MAX_JOBS = 32

# A short code typed from a phone.
INPUT_TIMEOUT_SECONDS = 300.0
# Relaying a prompt to another chat window, waiting for a multi-week plan to
# be written, reading it over, and pasting it back. Five minutes is not
# enough; losing the paste after all that work is the worst possible failure.
PASTE_TIMEOUT_SECONDS = 3600.0


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | awaiting_input | done | error
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    prompt: str | None = None  # what we are waiting for, when awaiting_input
    multiline: bool = False  # a pasted document rather than a short code
    relay: str | None = None  # text the user must copy elsewhere, if any

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _input_ready: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    _input_value: str | None = field(default=None, repr=False)

    # --- worker side ---

    def say(self, message: str) -> None:
        with self._lock:
            self.log.append(message)

    def ask(
        self, prompt: str, multiline: bool = False, relay: str | None = None
    ) -> str:
        """Block the worker until the UI supplies a value.

        `relay` is text the user has to carry somewhere else — the generation
        prompt, when they are relaying it to a chat window by hand.
        """
        # Clear BEFORE advertising that we are waiting. The other order loses
        # a value supplied in the gap: the UI polls, sees awaiting_input,
        # calls provide() which sets the Event, and we then wipe it and block
        # until the timeout.
        self._input_ready.clear()
        with self._lock:
            self.prompt = prompt
            self.multiline = multiline
            self.relay = relay
            self.status = "awaiting_input"

        timeout = PASTE_TIMEOUT_SECONDS if multiline else INPUT_TIMEOUT_SECONDS
        if not self._input_ready.wait(timeout=timeout):
            with self._lock:
                self.status = "running"
                self.prompt = self.relay = None
            raise TimeoutError(
                f"timed out waiting for {prompt.lower()} — start again when ready"
            )
        with self._lock:
            value = self._input_value or ""
            self._input_value = None
            self.prompt = self.relay = None
            self.multiline = False
            self.status = "running"
        return value

    # --- UI side ---

    def provide(self, value: str) -> bool:
        with self._lock:
            if self.status != "awaiting_input":
                return False
            self._input_value = value
        self._input_ready.set()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "log": list(self.log),
                "result": self.result,
                "error": self.error,
                "prompt": self.prompt,
                "multiline": self.multiline,
                "relay": self.relay,
            }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def start(self, kind: str, work: Callable[[Job], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > MAX_JOBS:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)

        def run() -> None:
            try:
                result = work(job)
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                with job._lock:
                    job.error = str(exc) or exc.__class__.__name__
                    job.status = "error"
                    job.log.append(f"error: {job.error}")
                traceback.print_exc()
            else:
                with job._lock:
                    job.result = result
                    job.status = "done"

        threading.Thread(target=run, daemon=True, name=f"gpp-{kind}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)
