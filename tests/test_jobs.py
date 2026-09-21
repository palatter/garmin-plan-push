"""The job registry, and specifically the ask/provide handshake.

The interesting case is the race: the worker parks waiting for input while the
browser polls on its own schedule. If the Event is cleared after the job
advertises that it is waiting, a value supplied in that gap is lost and the
worker hangs until its timeout.
"""

import threading
import time

import pytest

from gpp.web import jobs
from gpp.web.jobs import MAX_JOBS, JobRegistry


@pytest.fixture
def registry():
    return JobRegistry()


@pytest.fixture(autouse=True)
def quick_timeouts(monkeypatch):
    """Fail fast instead of parking for the real five minutes."""
    monkeypatch.setattr(jobs, "INPUT_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(jobs, "PASTE_TIMEOUT_SECONDS", 2.0)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_runs_work_and_records_the_result(registry):
    job = registry.start("t", lambda j: {"answer": 42})
    assert wait_for(lambda: job.snapshot()["status"] == "done")
    assert job.snapshot()["result"] == {"answer": 42}


def test_failures_are_captured_not_raised(registry):
    def boom(_):
        raise ValueError("nope")

    job = registry.start("t", boom)
    assert wait_for(lambda: job.snapshot()["status"] == "error")
    assert "nope" in job.snapshot()["error"]


def test_log_lines_reach_the_snapshot(registry):
    def work(job):
        job.say("one")
        job.say("two")
        return None

    job = registry.start("t", work)
    assert wait_for(lambda: job.snapshot()["status"] == "done")
    assert job.snapshot()["log"] == ["one", "two"]


def test_ask_receives_the_provided_value(registry):
    job = registry.start("t", lambda j: j.ask("code"))
    assert wait_for(lambda: job.snapshot()["status"] == "awaiting_input")
    assert job.snapshot()["prompt"] == "code"
    assert job.provide("123456")
    assert wait_for(lambda: job.snapshot()["status"] == "done")
    assert job.snapshot()["result"] == "123456"


def test_multiline_ask_carries_the_relay_text(registry):
    job = registry.start("t", lambda j: j.ask("Paste it", multiline=True, relay="THE PROMPT"))
    assert wait_for(lambda: job.snapshot()["status"] == "awaiting_input")
    snap = job.snapshot()
    assert snap["multiline"] is True
    assert snap["relay"] == "THE PROMPT"
    job.provide("pasted")
    assert wait_for(lambda: job.snapshot()["status"] == "done")


def test_provide_is_rejected_when_nothing_is_waiting(registry):
    job = registry.start("t", lambda j: "done immediately")
    assert wait_for(lambda: job.snapshot()["status"] == "done")
    assert job.provide("late") is False


def test_ask_times_out_rather_than_hanging_forever(registry):
    job = registry.start("t", lambda j: j.ask("code"))
    assert wait_for(lambda: job.snapshot()["status"] == "error", timeout=8)
    assert "timed out" in job.snapshot()["error"]


def test_accepted_input_is_never_lost(registry):
    """Regression: the Event must be cleared BEFORE status flips.

    The invariant under test is simple and total: if provide() returns True,
    that value must reach the worker. The bug this guards against violated it
    — the job advertised "awaiting_input", accepted a value, and then cleared
    the Event out from under it, hanging until the timeout.

    Polling for the window is useless (it is nanoseconds wide), so the
    interleaving is forced: clear() is wrapped so a provide() lands at exactly
    that instant. Under the old ordering provide() returns True and the value
    vanishes; under the fixed ordering it returns False because the job is not
    advertising yet, and the normal path below delivers instead.
    """
    outcome: dict[str, bool] = {}

    def work(job):
        real_clear = job._input_ready.clear

        def racing_clear():
            outcome["accepted"] = job.provide("racer")
            real_clear()

        job._input_ready.clear = racing_clear
        return job.ask("code")

    job = registry.start("t", work)
    assert wait_for(lambda: "accepted" in outcome), "worker never reached ask()"

    if outcome["accepted"]:
        assert wait_for(lambda: job.snapshot()["status"] == "done", timeout=4), (
            "provide() accepted a value and then lost it — the Event was "
            "cleared after the job began advertising awaiting_input"
        )
        assert job.snapshot()["result"] == "racer"
    else:
        # Declined because the job was not advertising yet. That is correct;
        # the worker must still be reachable the normal way.
        assert wait_for(lambda: job.snapshot()["status"] == "awaiting_input")
        assert job.provide("proper")
        assert wait_for(lambda: job.snapshot()["status"] == "done")
        assert job.snapshot()["result"] == "proper"


def test_concurrent_jobs_do_not_cross_wires(registry):
    started = threading.Barrier(4)

    def work(job):
        started.wait()
        return job.ask("code")

    running = [registry.start("t", work) for _ in range(4)]
    for job in running:
        assert wait_for(lambda j=job: j.snapshot()["status"] == "awaiting_input")
    for index, job in enumerate(running):
        job.provide(f"v{index}")
    for index, job in enumerate(running):
        assert wait_for(lambda j=job: j.snapshot()["status"] == "done")
        assert job.snapshot()["result"] == f"v{index}"


def test_registry_evicts_old_jobs(registry):
    jobs_made = [registry.start("t", lambda j: None) for _ in range(MAX_JOBS + 5)]
    for job in jobs_made:
        wait_for(lambda j=job: j.snapshot()["status"] == "done")
    assert registry.get(jobs_made[0].id) is None
    assert registry.get(jobs_made[-1].id) is not None
