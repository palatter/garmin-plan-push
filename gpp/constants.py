"""Garmin Connect internal workout-API constants.

PROVENANCE
----------
Garmin does not publish these IDs outside the Connect Developer Program, so
every value here comes from community reverse-engineering, cross-checked
against more than one independent implementation. They are *empirical*, not
authoritative.

This is deliberately the only module that knows the magic numbers. If Garmin
renumbers something, or a value below turns out to be wrong for your account,
this file is the single place to fix it -- and `gpp push --verify` will tell
you, because it re-reads the workout back from Garmin after upload and diffs
it against what we sent.

Confidence notes are inline. HIGH = seen identically in several independent
sources. MEDIUM = seen once, or inferred from adjacent values.
"""

from __future__ import annotations

# --- Sports -----------------------------------------------------------------
# HIGH: running/cycling; MEDIUM: the rest.
SPORT_TYPES: dict[str, dict] = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swimming": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 4},
    "strength": {
        "sportTypeId": 5,
        "sportTypeKey": "strength_training",
        "displayOrder": 5,
    },
    "cardio": {"sportTypeId": 8, "sportTypeKey": "cardio_training", "displayOrder": 8},
}

# --- Step types -------------------------------------------------------------
# HIGH. 1/2/3/5/6 are confirmed across sources; 4 (recovery) and 7 (other)
# follow the same sequence and match observed Connect payloads.
STEP_TYPES: dict[str, dict] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
    "other": {"stepTypeId": 7, "stepTypeKey": "other", "displayOrder": 7},
}

# --- End conditions ---------------------------------------------------------
# HIGH for lap.button/time/distance/iterations; MEDIUM for calories/power/hr.
END_CONDITIONS: dict[str, dict] = {
    "lap.button": {
        "conditionTypeId": 1,
        "conditionTypeKey": "lap.button",
        "displayOrder": 1,
        "displayable": True,
    },
    "time": {
        "conditionTypeId": 2,
        "conditionTypeKey": "time",
        "displayOrder": 2,
        "displayable": True,
    },
    "distance": {
        "conditionTypeId": 3,
        "conditionTypeKey": "distance",
        "displayOrder": 3,
        "displayable": True,
    },
    "calories": {
        "conditionTypeId": 4,
        "conditionTypeKey": "calories",
        "displayOrder": 4,
        "displayable": True,
    },
    "power": {
        "conditionTypeId": 5,
        "conditionTypeKey": "power",
        "displayOrder": 5,
        "displayable": True,
    },
    "heart.rate": {
        "conditionTypeId": 6,
        "conditionTypeKey": "heart.rate",
        "displayOrder": 6,
        "displayable": True,
    },
    "iterations": {
        "conditionTypeId": 7,
        "conditionTypeKey": "iterations",
        "displayOrder": 7,
        "displayable": False,
    },
    "fixed.rest": {
        "conditionTypeId": 8,
        "conditionTypeKey": "fixed.rest",
        "displayOrder": 8,
        "displayable": True,
    },
    "reps": {
        "conditionTypeId": 10,
        "conditionTypeKey": "reps",
        "displayOrder": 10,
        "displayable": True,
    },
}

# --- Target types -----------------------------------------------------------
# MEDIUM overall. `no.target` (1) and `pace.zone` (6) are the best attested.
# NOTE: for pace.zone, Garmin stores targetValueOne/Two in METRES PER SECOND,
# not seconds-per-km. See gpp.units.pace_to_mps. Which of One/Two is the fast
# bound is not consistently documented, so the compiler always writes
# One = slower (lower m/s), Two = faster (higher m/s) and the verifier will
# surface it if Garmin swaps them back.
TARGET_TYPES: dict[str, dict] = {
    "no.target": {
        "workoutTargetTypeId": 1,
        "workoutTargetTypeKey": "no.target",
        "displayOrder": 1,
    },
    "power.zone": {
        "workoutTargetTypeId": 2,
        "workoutTargetTypeKey": "power.zone",
        "displayOrder": 2,
    },
    "cadence.zone": {
        "workoutTargetTypeId": 3,
        "workoutTargetTypeKey": "cadence",
        "displayOrder": 3,
    },
    "heart.rate.zone": {
        "workoutTargetTypeId": 4,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
    },
    "speed.zone": {
        "workoutTargetTypeId": 5,
        "workoutTargetTypeKey": "speed.zone",
        "displayOrder": 5,
    },
    "pace.zone": {
        "workoutTargetTypeId": 6,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": 6,
    },
}

# Marker embedded in every workout description so we can recognise -- and
# safely replace -- workouts this tool created, without touching ones you
# built by hand in Garmin Connect.
TAG_PREFIX = "gpp"
