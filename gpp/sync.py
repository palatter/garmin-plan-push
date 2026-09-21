"""Pull activities and daily metrics from Garmin into the history store.

Written against `python-garminconnect`'s method names, looked up with
getattr so a version that lacks one degrades to "not available" rather than
an AttributeError. The response shapes are the library's, and they are
undocumented, so every field read is defensive. The parsing is pure and
tested with recorded-shape fakes; the network call is one line.

Nothing here writes to Garmin.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from .history import History


@dataclass
class SyncResult:
    activities: int = 0
    days: int = 0
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _call(api, name: str, *args, skipped: list[str] | None = None):
    fn = getattr(api, name, None)
    if not callable(fn):
        if skipped is not None:
            skipped.append(name)
        return None
    try:
        return fn(*args)
    except Exception as exc:
        if skipped is not None:
            skipped.append(f"{name}: {exc}")
        return None


# --- activities -------------------------------------------------------------

RUN_TYPES = {
    "running",
    "trail_running",
    "treadmill_running",
    "track_running",
    "street_running",
    "indoor_running",
    "virtual_run",
}


def parse_activity(raw: dict) -> dict | None:
    """One activity from get_activities() -> our record, or None if not a run."""
    kind = (
        (raw.get("activityType") or {}).get("typeKey") or raw.get("activityTypeKey") or ""
    ).lower()
    if kind and kind not in RUN_TYPES:
        return None
    start = raw.get("startTimeLocal") or raw.get("startTimeGMT") or ""
    date = start[:10]
    if len(date) != 10:
        return None
    return {
        "id": str(
            raw.get("activityId") or raw.get("id") or f"{date}-{raw.get('activityName', '')}"
        ),
        "date": date,
        "name": raw.get("activityName") or "Run",
        "sport": "running",
        "distance_m": float(raw.get("distance") or 0.0),
        "seconds": float(raw.get("duration") or raw.get("movingDuration") or 0.0),
        "avg_hr": int(raw["averageHR"]) if raw.get("averageHR") else None,
        "source": "garmin",
    }


def sync_activities(
    api,
    store: History,
    days: int = 90,
    page: int = 100,
    log: Callable[[str], None] = lambda _: None,
) -> SyncResult:
    result = SyncResult()
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    start = 0
    while True:
        batch = _call(api, "get_activities", start, page, skipped=result.skipped)
        if not batch:
            break
        stop = False
        for raw in batch:
            record = parse_activity(raw)
            if record is None:
                continue
            if record["date"] < cutoff:
                stop = True
                break
            store.upsert_activity(record)
            result.activities += 1
        log(f"  {result.activities} runs so far")
        if stop or len(batch) < page:
            break
        start += page
    return result


# --- daily metrics ----------------------------------------------------------


def parse_readiness(raw) -> int | None:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        for key in ("score", "trainingReadinessScore", "readinessScore"):
            if raw.get(key) is not None:
                return int(raw[key])
    return None


def parse_hrv(raw) -> tuple[str | None, int | None]:
    if not isinstance(raw, dict):
        return None, None
    summary = raw.get("hrvSummary") or raw
    status = summary.get("status") or summary.get("hrvStatus")
    weekly = summary.get("weeklyAvg") or summary.get("lastNightAvg")
    return (str(status).lower() if status else None), (int(weekly) if weekly else None)


def parse_sleep(raw) -> int | None:
    if not isinstance(raw, dict):
        return None
    daily = raw.get("dailySleepDTO") or raw
    scores = daily.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    value = overall.get("value") if isinstance(overall, dict) else overall
    if value is None:
        value = daily.get("sleepScore")
    return int(value) if value is not None else None


def parse_resting_hr(raw) -> int | None:
    if not isinstance(raw, dict):
        return None
    for key in ("restingHeartRate", "restingHR"):
        if raw.get(key) is not None:
            return int(raw[key])
    all_metrics = raw.get("allMetrics") or {}
    for series in (all_metrics.get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE", []):
        if series.get("value") is not None:
            return int(series["value"])
    return None


def parse_vo2max(raw) -> float | None:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    generic = raw.get("generic") or {}
    value = (
        generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue") or raw.get("vo2MaxValue")
    )
    return float(value) if value else None


def parse_training_status(raw) -> str | None:
    if not isinstance(raw, dict):
        return None
    latest = raw.get("mostRecentTrainingStatus") or raw
    devices = latest.get("latestTrainingStatusData") if isinstance(latest, dict) else None
    if isinstance(devices, dict):
        for entry in devices.values():
            if isinstance(entry, dict) and entry.get("trainingStatusFeedbackPhrase"):
                return str(entry["trainingStatusFeedbackPhrase"]).lower()
    phrase = latest.get("trainingStatusFeedbackPhrase") if isinstance(latest, dict) else None
    return str(phrase).lower() if phrase else None


def sync_metrics(
    api, store: History, day: dt.date | None = None, log: Callable[[str], None] = lambda _: None
) -> SyncResult:
    day = day or dt.date.today()
    result = SyncResult()
    iso = day.isoformat()
    readiness = parse_readiness(_call(api, "get_training_readiness", iso, skipped=result.skipped))
    hrv_status, hrv_weekly = parse_hrv(_call(api, "get_hrv_data", iso, skipped=result.skipped))
    sleep = parse_sleep(_call(api, "get_sleep_data", iso, skipped=result.skipped))
    rhr = parse_resting_hr(_call(api, "get_rhr_day", iso, skipped=result.skipped))
    vo2 = parse_vo2max(_call(api, "get_max_metrics", iso, skipped=result.skipped))
    status = parse_training_status(_call(api, "get_training_status", iso, skipped=result.skipped))
    store.upsert_metrics(
        iso,
        readiness=readiness,
        hrv_status=hrv_status,
        hrv_weekly_avg=hrv_weekly,
        sleep_score=sleep,
        resting_hr=rhr,
        vo2max=vo2,
        training_status=status,
    )
    result.days = 1
    got = [
        k
        for k, v in (
            ("readiness", readiness),
            ("hrv", hrv_status),
            ("sleep", sleep),
            ("resting HR", rhr),
            ("VO2max", vo2),
            ("status", status),
        )
        if v is not None
    ]
    log(f"  {iso}: " + (", ".join(got) if got else "no metrics available"))
    return result


# --- one-off reads ----------------------------------------------------------


def race_predictions(api) -> dict[str, float]:
    """Garmin's own race predictor, as {distance: seconds}, if the account has it."""
    raw = _call(api, "get_race_predictions")
    out: dict[str, float] = {}
    entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key, label in (
            ("time5K", "5k"),
            ("time10K", "10k"),
            ("timeHalfMarathon", "half"),
            ("timeMarathon", "marathon"),
        ):
            if entry.get(key):
                out[label] = float(entry[key])
    return out


def hr_zones(api) -> list[tuple[int, int]] | None:
    """Garmin's configured HR zones as [(low, high), ...], if exposed."""
    raw = _call(api, "get_heart_rate_zones") or _call(api, "get_hr_zones")
    if not raw:
        return None
    entries = raw if isinstance(raw, list) else raw.get("zones") if isinstance(raw, dict) else None
    if not entries:
        return None
    zones = []
    for z in entries:
        if isinstance(z, dict) and z.get("zoneLowBoundary") is not None:
            zones.append(
                (int(z["zoneLowBoundary"]), int(z.get("zoneHighBoundary") or z["zoneLowBoundary"]))
            )
    return zones or None
