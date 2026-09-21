"""Estimate threshold pace from a recent race.

"What is your threshold pace?" is a question most runners cannot answer, and
guessing it wrong skews every workout the tool will ever produce. "What is a
recent race time?" is a question everyone can answer.

Riegel's endurance formula predicts a time at one distance from a time at
another:

    T2 = T1 * (D2 / D1) ** 1.06

Threshold is conventionally about the pace you could hold in a one-hour race,
so we invert Riegel to find the distance you would cover in 3600 seconds and
take the pace that implies. The exponent is empirical and holds well between
roughly 1500 m and the marathon; outside that it drifts, so we say so.
"""

from __future__ import annotations

from dataclasses import dataclass

from .units import METRES_PER_MILE, UnitError, format_pace, parse_distance, parse_duration

RIEGEL_EXPONENT = 1.06
THRESHOLD_RACE_SECONDS = 3600.0

# Distances the formula is honest about.
RELIABLE_MIN_METRES = 1500.0
RELIABLE_MAX_METRES = 42_195.0

COMMON_RACES: dict[str, float] = {
    "1500m": 1500.0,
    "1 mile": METRES_PER_MILE,
    "3k": 3000.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "15k": 15000.0,
    "10 miles": 10 * METRES_PER_MILE,
    "half marathon": 21_097.5,
    "marathon": 42_195.0,
}


class EstimateError(ValueError):
    """The race result given cannot produce a sensible estimate."""


@dataclass
class ThresholdEstimate:
    threshold_pace: float  # seconds per km
    race_metres: float
    race_seconds: float
    hour_distance_metres: float
    reliable: bool
    note: str = ""

    def describe(self, imperial: bool = False) -> str:
        pace = format_pace(self.threshold_pace, imperial)
        km = self.hour_distance_metres / 1000
        text = f"Estimated threshold pace {pace} (about {km:.1f} km in an hour)"
        if self.note:
            text += f"\n{self.note}"
        return text


def threshold_from_race(distance: str | float, time: str | float) -> ThresholdEstimate:
    """Derive threshold pace from a race distance and finishing time."""
    key = str(distance).strip().lower()
    if key in COMMON_RACES:
        metres = COMMON_RACES[key]
    else:
        try:
            metres = parse_distance(distance)
        except UnitError as exc:
            raise EstimateError(
                f"{exc}. Try a distance like '5k', '10k', 'half marathon', or '1600m'."
            ) from exc

    try:
        seconds = parse_duration(time)
    except UnitError as exc:
        raise EstimateError(f"{exc}. Try a time like '21:30', '1:45:00', or '45m'.") from exc

    if metres <= 0:
        raise EstimateError("race distance must be positive")
    if seconds <= 0:
        raise EstimateError("race time must be positive")

    pace = seconds / (metres / 1000)
    if pace < 120:
        raise EstimateError(
            f"that is {format_pace(pace)} — faster than the world record. "
            "Check the distance and time are the right way round."
        )
    if pace > 900:
        raise EstimateError(
            f"that is {format_pace(pace)}, which looks like a typo. Check the units on the time."
        )

    # Invert Riegel: what distance would take exactly one hour?
    hour_metres = metres * (THRESHOLD_RACE_SECONDS / seconds) ** (1 / RIEGEL_EXPONENT)
    threshold_pace = THRESHOLD_RACE_SECONDS / (hour_metres / 1000)

    reliable = RELIABLE_MIN_METRES <= metres <= RELIABLE_MAX_METRES
    note = ""
    if not reliable:
        note = (
            "Note: Riegel's formula is calibrated between 1500 m and the "
            "marathon, so this estimate is rougher than usual. Re-check it "
            "against how your easy runs feel."
        )

    return ThresholdEstimate(
        threshold_pace=threshold_pace,
        race_metres=metres,
        race_seconds=seconds,
        hour_distance_metres=hour_metres,
        reliable=reliable,
        note=note,
    )


def lthr_from_max(hr_max: int) -> int:
    """A rough LTHR when the athlete only knows their max.

    Lactate threshold sits around 88% of max HR for most trained runners. This
    is a starting point, not a measurement -- a 30-minute time trial gives a
    far better number.
    """
    if not 120 <= hr_max <= 230:
        raise EstimateError(f"{hr_max} bpm is not a plausible max heart rate")
    return round(hr_max * 0.88)
