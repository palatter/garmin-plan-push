"""Physiological models: VDOT, critical speed, grade adjustment, pace-power.

Pure functions, no I/O. Each one is small enough to check against the paper
it came from, and each cites it.

  * Daniels-Gilbert VDOT (Daniels & Gilbert 1979; Daniels, *Running
    Formula*): oxygen cost of running as a function of velocity, and the
    fraction of VO2max sustainable for a given duration. Note the published
    finding that VDOT *underestimates* VO2max in recreational runners
    (d = 3.44) -- so the paces are useful, the VO2 number should not be shown
    as if it were measured.
  * Critical speed (Monod & Scherrer; Hill 1993): distance = CS * t + D'.
    A 2-trial fit is as good as 3 (trivial bias), trials of 2-20 minutes.
  * Grade adjustment (Minetti et al. 2002): energy cost of running against
    gradient, a fifth-order polynomial fitted to treadmill data.
  * Riegel (1981): t2 = t1 * (d2 / d1) ** 1.06, for race-time prediction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RIEGEL_EXPONENT = 1.06


class ModelError(ValueError):
    """The inputs cannot produce a meaningful estimate."""


# --- VDOT -------------------------------------------------------------------


def oxygen_cost(velocity_m_per_min: float) -> float:
    """ml/kg/min needed to run at this velocity (Daniels-Gilbert)."""
    v = velocity_m_per_min
    return -4.60 + 0.182258 * v + 0.000104 * v * v


def fraction_of_vo2max(minutes: float) -> float:
    """Share of VO2max a runner can hold for a race of this duration."""
    t = minutes
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)


def vdot_from_race(distance_m: float, seconds: float) -> float:
    if distance_m <= 0 or seconds <= 0:
        raise ModelError("distance and time must be positive")
    minutes = seconds / 60.0
    velocity = distance_m / minutes
    return oxygen_cost(velocity) / fraction_of_vo2max(minutes)


def velocity_for_cost(cost: float) -> float:
    """Invert oxygen_cost: metres per minute at which running costs `cost`."""
    a, b, c = 0.000104, 0.182258, -4.60 - cost
    disc = b * b - 4 * a * c
    if disc < 0:
        raise ModelError("no velocity produces that oxygen cost")
    return (-b + math.sqrt(disc)) / (2 * a)


def race_time_for_vdot(vdot: float, distance_m: float) -> float:
    """Seconds to race `distance_m` at this VDOT, by bisection on duration."""
    if vdot <= 0 or distance_m <= 0:
        raise ModelError("vdot and distance must be positive")

    def gap(minutes: float) -> float:
        return vdot * fraction_of_vo2max(minutes) - oxygen_cost(distance_m / minutes)

    lo, hi = 1.0, 600.0  # minutes
    if (gap(lo) < 0 and gap(hi) < 0) or (gap(lo) > 0 and gap(hi) > 0):
        raise ModelError("distance is outside the range this model can predict")
    for _ in range(80):
        mid = (lo + hi) / 2
        if gap(lo) * gap(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2 * 60.0


# Daniels' training intensities as fractions of VDOT. (slow_bound, fast_bound),
# slower first to match the profile's convention. Recovery and steady are
# interpolated so the zone set lines up with the threshold model's names.
VDOT_ZONES: dict[str, tuple[float, float]] = {
    "recovery": (0.55, 0.62),
    "easy": (0.62, 0.74),
    "steady": (0.74, 0.78),
    "marathon": (0.78, 0.85),
    "threshold": (0.85, 0.89),
    "interval": (0.96, 1.00),
    "repetition": (1.05, 1.10),
}


def vdot_pace(vdot: float, fraction: float) -> float:
    """Seconds per km at `fraction` of VDOT."""
    velocity = velocity_for_cost(vdot * fraction)
    return 1000.0 / velocity * 60.0


def vdot_zones(vdot: float) -> dict[str, tuple[float, float]]:
    """(slower, faster) seconds/km for each zone name."""
    return {
        name: (vdot_pace(vdot, lo), vdot_pace(vdot, hi)) for name, (lo, hi) in VDOT_ZONES.items()
    }


# --- Critical speed ---------------------------------------------------------


@dataclass
class CriticalSpeed:
    cs_mps: float
    d_prime_m: float
    trials: int
    note: str = ""

    @property
    def cs_pace(self) -> float:
        return 1000.0 / self.cs_mps


def critical_speed(trials: list[tuple[float, float]]) -> CriticalSpeed:
    """Fit distance = CS * time + D' to (distance_m, seconds) trials.

    Two trials suffice (PubMed 30427230: trivial bias vs three). Durations
    should sit between roughly 2 and 20 minutes; outside that the linear
    model bends, so we say so rather than silently fitting.
    """
    if len(trials) < 2:
        raise ModelError("critical speed needs at least two all-out trials")
    times = [t for _, t in trials]
    if len({round(t, 3) for t in times}) < 2:
        raise ModelError("trials must have different durations")
    for d, t in trials:
        if d <= 0 or t <= 0:
            raise ModelError("trial distance and time must be positive")

    n = len(trials)
    mean_t = sum(times) / n
    mean_d = sum(d for d, _ in trials) / n
    sxx = sum((t - mean_t) ** 2 for t in times)
    sxy = sum((t - mean_t) * (d - mean_d) for d, t in trials)
    cs = sxy / sxx
    d_prime = mean_d - cs * mean_t
    if cs <= 0:
        raise ModelError("trials imply a non-positive critical speed; check the inputs")

    note = ""
    if any(t < 120 or t > 1200 for t in times):
        note = "One or more trials fall outside the 2-20 minute window the model is calibrated for."
    if d_prime < 0:
        note = (
            note + " " if note else ""
        ) + "Negative D' -- the shorter trial was likely not all-out."
    return CriticalSpeed(cs_mps=cs, d_prime_m=d_prime, trials=n, note=note.strip())


# Fractions of critical speed. CS sits a touch above sustained threshold, so
# threshold tops out at CS itself. (slow_fraction, fast_fraction).
CS_ZONES: dict[str, tuple[float, float]] = {
    "recovery": (0.68, 0.76),
    "easy": (0.76, 0.85),
    "steady": (0.85, 0.90),
    "marathon": (0.90, 0.95),
    "threshold": (0.95, 1.00),
    "interval": (1.00, 1.08),
    "repetition": (1.08, 1.20),
}


def cs_zones(cs_mps: float) -> dict[str, tuple[float, float]]:
    """(slower, faster) seconds/km per zone from a critical speed in m/s."""
    if cs_mps <= 0:
        raise ModelError("critical speed must be positive")
    return {
        name: (1000.0 / (cs_mps * lo), 1000.0 / (cs_mps * hi))
        for name, (lo, hi) in CS_ZONES.items()
    }


# --- Grade adjustment -------------------------------------------------------


def running_cost(gradient: float) -> float:
    """Minetti 2002: J/kg/m to run at `gradient` (fraction, e.g. 0.05 = 5%)."""
    i = max(-0.45, min(0.45, gradient))
    return 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6


def gap_factor(grade_percent: float) -> float:
    """How much harder a grade is than flat, as a multiplier on time.

    Running a given *flat-equivalent* pace up a 5% grade takes about 1.4x as
    long per metre. Downhill gets cheaper to about -20%, after which the
    cost rises again -- the polynomial captures that.
    """
    return running_cost(grade_percent / 100.0) / running_cost(0.0)


# --- Pace <-> power ---------------------------------------------------------


def power_for_pace(pace_spk: float, cp_watts: float, pace_at_cp_spk: float) -> float:
    """Watts for a pace, on a linear power-speed model through (CP, CP pace).

    Running power is close to proportional to speed at constant mass and
    grade, which is what Stryd's own pace-to-power conversion assumes.
    """
    if pace_spk <= 0 or cp_watts <= 0 or pace_at_cp_spk <= 0:
        raise ModelError("pace, CP and CP pace must be positive")
    k = cp_watts / (1000.0 / pace_at_cp_spk)  # watts per m/s
    return k * (1000.0 / pace_spk)


def pace_for_power(watts: float, cp_watts: float, pace_at_cp_spk: float) -> float:
    if watts <= 0 or cp_watts <= 0 or pace_at_cp_spk <= 0:
        raise ModelError("watts, CP and CP pace must be positive")
    k = cp_watts / (1000.0 / pace_at_cp_spk)
    return 1000.0 / (watts / k)


# --- Race prediction --------------------------------------------------------

# Typical Riegel error for a well-matched distance pair, from the literature
# on its use in recreational runners. Shown as a band, never a single number.
RIEGEL_TYPICAL_ERROR = 0.03


def riegel_time(known_distance_m: float, known_seconds: float, target_distance_m: float) -> float:
    if min(known_distance_m, known_seconds, target_distance_m) <= 0:
        raise ModelError("distances and time must be positive")
    return known_seconds * (target_distance_m / known_distance_m) ** RIEGEL_EXPONENT


def predict_from_threshold(
    threshold_pace_spk: float, target_distance_m: float
) -> tuple[float, float, float]:
    """(fast, likely, slow) seconds, treating threshold as a one-hour effort."""
    hour_distance = 3600.0 / threshold_pace_spk * 1000.0
    likely = riegel_time(hour_distance, 3600.0, target_distance_m)
    return likely * (1 - RIEGEL_TYPICAL_ERROR), likely, likely * (1 + RIEGEL_TYPICAL_ERROR)


def predict_from_cs(cs: CriticalSpeed, target_distance_m: float) -> float:
    """Seconds to cover a distance from the CS model: t = (d - D') / CS."""
    if target_distance_m <= cs.d_prime_m:
        raise ModelError("distance is shorter than D'; the CS model does not apply")
    return (target_distance_m - cs.d_prime_m) / cs.cs_mps
