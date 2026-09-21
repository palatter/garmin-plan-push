"""The generation loop's third gate: the sanity report."""

import json

from gpp.generate import generate_plan, regenerate_workout
from gpp.plan import Plan
from gpp.profile import Profile
from gpp.providers import ProviderError

PROFILE = Profile.from_dict(
    {"pace": {"threshold": "4:00/km"}, "availability": {"days": ["tue", "thu", "sat"]}}
)


def wk(date, name, zone="easy", role="easy"):
    return {
        "name": name,
        "date": date,
        "role": role,
        "steps": [{"kind": "run", "duration": "40m", "target": {"type": "pace", "zone": zone}}],
    }


BLOCKED = json.dumps(
    {"plan": "p", "workouts": [wk("2026-09-21", "Mon run")]}
)  # Monday: not allowed
WARNED = json.dumps(
    {
        "plan": "p",
        "workouts": [
            wk("2026-09-22", "Q1", "threshold", "quality"),
            wk("2026-09-24", "Q2", "interval", "quality"),
            wk("2026-09-26", "Q3", "interval", "quality"),
        ],
    }
)
CLEAN = json.dumps(
    {
        "plan": "p",
        "workouts": [
            wk("2026-09-22", "Easy"),
            wk("2026-09-24", "Easy 2"),
            wk("2026-09-26", "Easy 3"),
        ],
    }
)


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user, schema, on_delta=None):
        self.prompts.append(user)
        return self.replies.pop(0)


def test_blocked_plan_is_sent_back_with_the_finding():
    provider = Scripted([BLOCKED, CLEAN])
    result = generate_plan(provider, PROFILE, "go")
    assert result.attempts == 2
    assert "cannot run" in provider.prompts[1]
    assert result.report.ok


def test_warned_plan_is_sent_back_once_then_accepted():
    provider = Scripted([WARNED, WARNED])
    result = generate_plan(provider, PROFILE, "go", attempts=2)
    assert result.attempts == 2  # the revised plan, still warned, is accepted
    assert result.report.warns
    assert "coaching checks flagged" in provider.prompts[1]


def test_blocked_forever_raises():
    provider = Scripted([BLOCKED, BLOCKED])
    try:
        generate_plan(provider, PROFILE, "go", attempts=2)
    except ProviderError as exc:
        assert "acceptable plan" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_regenerate_one_workout_keeps_the_rest():
    plan = Plan.from_dict({"plan": "p", "workouts": [wk("2026-09-22", "A"), wk("2026-09-24", "B")]})
    replacement = json.dumps(wk("2026-09-24", "B rewritten", "steady"))
    provider = Scripted([replacement])
    new = regenerate_workout(provider, PROFILE, plan, "2026-09-24", "make it steady")
    assert [w.name for w in new.workouts] == ["A", "B rewritten"]
    assert "make it steady" in provider.prompts[0]
