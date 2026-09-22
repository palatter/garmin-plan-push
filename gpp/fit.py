"""FIT workout files, written and read without a dependency (#144).

Garmin Connect's web site cannot import a workout FIT file, but every
watch can: copy the file into GARMIN/NewFiles (older models: GARMIN/Workouts)
over USB and it appears under Training > Workouts. That is the sideload path
from the very first conversation, kept for anyone who would rather not use
the unofficial API at all.

The format is documented in Garmin's FIT SDK. A workout file is three
message types -- file_id, workout, workout_step -- each written as a
definition message (field layout) followed by data messages, inside a
14-byte header and a CRC-16. The encoder here writes exactly that and the
decoder reads it back, which is how the round trip is tested.
"""

from __future__ import annotations

import datetime as dt
import struct
from dataclasses import dataclass
from typing import Any

from .compile import resolve_pace_bounds
from .plan import Step, Target, Workout
from .profile import Profile
from .units import format_pace, mps_to_pace, pace_to_mps, parse_distance, parse_duration

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.UTC)
PROFILE_VERSION = 2160
PROTOCOL_VERSION = 0x20

# Global message numbers.
FILE_ID, WORKOUT, WORKOUT_STEP = 0, 26, 27

# Base types: (byte, size).
ENUM, UINT8, STRING, UINT16, UINT32, UINT32Z = 0x00, 0x02, 0x07, 0x84, 0x86, 0x8C
SIZES = {ENUM: 1, UINT8: 1, UINT16: 2, UINT32: 4, UINT32Z: 4}
INVALID = {ENUM: 0xFF, UINT8: 0xFF, UINT16: 0xFFFF, UINT32: 0xFFFFFFFF, UINT32Z: 0}

FILE_TYPE_WORKOUT = 5
MANUFACTURER_DEVELOPMENT = 255

SPORT = {"running": 1, "cycling": 2, "swimming": 5, "strength": 10, "cardio": 10}
SPORT_BACK = {1: "running", 2: "cycling", 5: "swimming", 10: "strength"}

# wkt_step_duration
DUR_TIME, DUR_DISTANCE, DUR_OPEN, DUR_REPEAT, DUR_REPS = 0, 1, 5, 6, 29
# wkt_step_target
TGT_SPEED, TGT_HEART_RATE, TGT_OPEN, TGT_CADENCE, TGT_POWER = 0, 1, 2, 3, 4
# intensity
INTENSITY = {
    "run": 0,
    "stride": 0,
    "exercise": 0,
    "rest": 1,
    "warmup": 2,
    "cooldown": 3,
    "recover": 4,
}
INTENSITY_BACK = {0: "run", 1: "rest", 2: "warmup", 3: "cooldown", 4: "recover", 5: "run"}

NAME_SIZE, STEP_NAME_SIZE, NOTES_SIZE = 64, 32, 128

CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


class FitError(ValueError):
    """The bytes are not a FIT workout file this reader understands."""


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc


# --- field layouts -------------------------------------------------------------

FILE_ID_FIELDS = [(0, ENUM), (1, UINT16), (2, UINT16), (3, UINT32Z), (4, UINT32)]
WORKOUT_FIELDS = [(4, ENUM), (5, UINT32Z), (6, UINT16), (8, STRING)]
STEP_FIELDS = [
    (254, UINT16),  # message_index
    (0, STRING),  # wkt_step_name
    (1, ENUM),  # duration_type
    (2, UINT32),  # duration_value
    (3, ENUM),  # target_type
    (4, UINT32),  # target_value
    (5, UINT32),  # custom_target_value_low
    (6, UINT32),  # custom_target_value_high
    (7, ENUM),  # intensity
    (8, STRING),  # notes
]
STRING_SIZES = {
    (WORKOUT, 8): NAME_SIZE,
    (WORKOUT_STEP, 0): STEP_NAME_SIZE,
    (WORKOUT_STEP, 8): NOTES_SIZE,
}


def _size(global_num: int, num: int, base: int) -> int:
    return STRING_SIZES[(global_num, num)] if base == STRING else SIZES[base]


def _definition(local: int, global_num: int, fields: list[tuple[int, int]]) -> bytes:
    out = bytearray([0x40 | local, 0, 0])
    out += struct.pack("<H", global_num)
    out.append(len(fields))
    for num, base in fields:
        out += bytes([num, _size(global_num, num, base), base])
    return bytes(out)


def _data(
    local: int, global_num: int, fields: list[tuple[int, int]], values: dict[int, Any]
) -> bytes:
    out = bytearray([local])
    for num, base in fields:
        value = values.get(num)
        if base == STRING:
            size = _size(global_num, num, base)
            raw = (value or "").encode("utf-8")[: size - 1]
            out += raw + b"\x00" * (size - len(raw))
        else:
            if value is None:
                value = INVALID[base]
            fmt = {1: "<B", 2: "<H", 4: "<I"}[SIZES[base]]
            out += struct.pack(fmt, int(value))
    return bytes(out)


# --- encoding ------------------------------------------------------------------


@dataclass
class FitStep:
    index: int
    name: str
    duration_type: int
    duration_value: int | None
    target_type: int
    target_value: int | None
    low: int | None
    high: int | None
    intensity: int
    notes: str | None


def _target(target: Target, profile: Profile) -> tuple[int, int | None, int | None, int | None]:
    if target.type == "pace":
        slow, fast = resolve_pace_bounds(target, profile)
        return TGT_SPEED, 0, round(pace_to_mps(slow) * 1000), round(pace_to_mps(fast) * 1000)
    if target.type == "hr":
        if target.zone is not None:
            return TGT_HEART_RATE, int(target.zone), None, None
        return TGT_HEART_RATE, 0, int(target.low) + 100, int(target.high) + 100
    if target.type == "cadence":
        return TGT_CADENCE, 0, int(target.low), int(target.high)
    if target.type == "power":
        if target.zone is not None:
            return TGT_POWER, int(target.zone), None, None
        return TGT_POWER, 0, int(target.low) + 1000, int(target.high) + 1000
    return TGT_OPEN, None, None, None


def _flatten(steps: list[Step], profile: Profile, out: list[FitStep]) -> None:
    for step in steps:
        if step.is_repeat:
            first = len(out)
            _flatten(step.steps, profile, out)
            out.append(
                FitStep(
                    len(out),
                    "Repeat",
                    DUR_REPEAT,
                    first,
                    TGT_OPEN,
                    int(step.reps or 1),
                    None,
                    None,
                    0,
                    step.note,
                )
            )
            continue
        if step.count is not None:
            dur_type, dur_value = DUR_REPS, int(step.count)
        elif step.duration is not None:
            dur_type, dur_value = DUR_TIME, round(parse_duration(step.duration) * 1000)
        elif step.distance is not None:
            dur_type, dur_value = DUR_DISTANCE, round(parse_distance(step.distance) * 100)
        else:
            dur_type, dur_value = DUR_OPEN, None
        tgt, value, low, high = _target(step.target, profile)
        name = step.exercise or step.kind
        note = step.note
        if step.target.type == "rpe" and step.target.value:
            note = f"RPE {step.target.value}/10" + (f" - {note}" if note else "")
        out.append(
            FitStep(
                len(out),
                name[:31],
                dur_type,
                dur_value,
                tgt,
                value,
                low,
                high,
                INTENSITY.get(step.kind, 0),
                note,
            )
        )


def encode_workout(workout: Workout, profile: Profile, created: dt.datetime | None = None) -> bytes:
    """A complete FIT workout file for `workout`, ready to copy to a watch."""
    steps: list[FitStep] = []
    _flatten(workout.steps, profile, steps)
    if not steps:
        raise FitError("a workout needs at least one step")
    created = created or dt.datetime.now(dt.UTC)
    stamp = int((created - FIT_EPOCH).total_seconds())

    body = bytearray()
    body += _definition(0, FILE_ID, FILE_ID_FIELDS)
    body += _data(
        0,
        FILE_ID,
        FILE_ID_FIELDS,
        {0: FILE_TYPE_WORKOUT, 1: MANUFACTURER_DEVELOPMENT, 2: 0, 3: 1, 4: stamp},
    )
    body += _definition(1, WORKOUT, WORKOUT_FIELDS)
    body += _data(
        1,
        WORKOUT,
        WORKOUT_FIELDS,
        {4: SPORT.get(workout.sport, 1), 5: 32, 6: len(steps), 8: workout.name},
    )
    body += _definition(2, WORKOUT_STEP, STEP_FIELDS)
    for s in steps:
        body += _data(
            2,
            WORKOUT_STEP,
            STEP_FIELDS,
            {
                254: s.index,
                0: s.name,
                1: s.duration_type,
                2: s.duration_value,
                3: s.target_type,
                4: s.target_value,
                5: s.low,
                6: s.high,
                7: s.intensity,
                8: s.notes,
            },
        )

    header = bytearray(
        struct.pack("<BBHI4s", 14, PROTOCOL_VERSION, PROFILE_VERSION, len(body), b".FIT")
    )
    header += struct.pack("<H", crc16(bytes(header)))
    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", crc16(out))


# --- decoding ------------------------------------------------------------------


def decode(data: bytes) -> dict[str, Any]:
    """Parse a FIT file written by `encode_workout` (or any single-definition
    workout file without developer fields) into plain dicts."""
    if len(data) < 16 or data[8:12] != b".FIT":
        raise FitError("not a FIT file")
    header_size = data[0]
    data_size = struct.unpack_from("<I", data, 4)[0]
    if crc16(data[:-2]) != struct.unpack_from("<H", data, len(data) - 2)[0]:
        raise FitError("FIT checksum does not match")
    pos, end = header_size, header_size + data_size
    definitions: dict[int, tuple[int, list[tuple[int, int, int]]]] = {}
    messages: list[tuple[int, dict[int, Any]]] = []
    while pos < end:
        head = data[pos]
        pos += 1
        local = head & 0x0F
        if head & 0x40:
            _, arch = data[pos], data[pos + 1]
            global_num = struct.unpack_from("<H" if arch == 0 else ">H", data, pos + 2)[0]
            count = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(count):
                num, size, base = data[pos], data[pos + 1], data[pos + 2]
                fields.append((num, size, base))
                pos += 3
            if head & 0x20:  # developer fields: skip their definitions
                dev = data[pos]
                pos += 1 + 3 * dev
            definitions[local] = (global_num, fields)
            continue
        if local not in definitions:
            raise FitError("data message before its definition")
        global_num, fields = definitions[local]
        values: dict[int, Any] = {}
        for num, size, base in fields:
            chunk = data[pos : pos + size]
            pos += size
            if base == STRING:
                values[num] = chunk.split(b"\x00", 1)[0].decode("utf-8", "replace")
            else:
                fmt = {1: "<B", 2: "<H", 4: "<I"}.get(size)
                if fmt is None:
                    continue
                raw = struct.unpack(fmt, chunk)[0]
                values[num] = None if raw == INVALID.get(base, -1) else raw
        messages.append((global_num, values))
    out: dict[str, Any] = {"file_id": {}, "workout": {}, "steps": []}
    for global_num, values in messages:
        if global_num == FILE_ID:
            out["file_id"] = values
        elif global_num == WORKOUT:
            out["workout"] = values
        elif global_num == WORKOUT_STEP:
            out["steps"].append(values)
    return out


def _dsl_target(values: dict[int, Any], profile: Profile | None) -> dict:
    kind, value, low, high = values.get(3), values.get(4), values.get(5), values.get(6)
    if kind == TGT_SPEED and low and high:
        slow, fast = mps_to_pace(low / 1000), mps_to_pace(high / 1000)
        if profile is not None:
            for name, (zs, zf) in profile.zone_table().items():
                if abs(zs - slow) < 1 and abs(zf - fast) < 1:
                    return {"type": "pace", "zone": name}
        imperial = bool(profile and profile.imperial)
        return {
            "type": "pace",
            "slow": format_pace(slow, imperial),
            "fast": format_pace(fast, imperial),
        }
    if kind == TGT_HEART_RATE:
        if value:
            return {"type": "hr", "zone": int(value)}
        if low and high:
            return {"type": "hr", "low": int(low) - 100, "high": int(high) - 100}
    if kind == TGT_CADENCE and low and high:
        return {"type": "cadence", "low": int(low), "high": int(high)}
    if kind == TGT_POWER:
        if value:
            return {"type": "power", "zone": int(value)}
        if low and high:
            return {"type": "power", "low": int(low) - 1000, "high": int(high) - 1000}
    return {"type": "none"}


def workout_from_fit(data: bytes, date: dt.date, profile: Profile | None = None) -> Workout:
    """The DSL workout a FIT file describes, repeats rebuilt from repeat steps."""
    parsed = decode(data)
    raw_steps = sorted(parsed["steps"], key=lambda v: v.get(254, 0))
    if not raw_steps:
        raise FitError("no workout steps in the file")

    def dsl(values: dict[int, Any]) -> dict:
        dur_type, dur_value = values.get(1), values.get(2)
        step: dict[str, Any] = {"kind": INTENSITY_BACK.get(values.get(7, 0), "run")}
        if dur_type == DUR_TIME and dur_value is not None:
            step["duration"] = f"{dur_value / 1000:g}s"
        elif dur_type == DUR_DISTANCE and dur_value is not None:
            step["distance"] = f"{dur_value / 100:g}m"
        elif dur_type == DUR_REPS and dur_value is not None:
            step = {
                "kind": "exercise",
                "exercise": values.get(0) or "exercise",
                "count": int(dur_value),
            }
        else:
            step["until"] = "lap"
        if step["kind"] != "exercise":
            target = _dsl_target(values, profile)
            if target["type"] != "none" or step["kind"] == "run":
                step["target"] = target
        if values.get(8):
            step["note"] = values[8]
        return step

    # Repeat steps refer back to the index their block starts at: fold from
    # the end so nested blocks resolve inside out.
    items: list[tuple[int, dict]] = []  # (start index, dsl step)
    for values in raw_steps:
        index = values.get(254, len(items))
        if values.get(1) == DUR_REPEAT:
            start = int(values.get(2) or 0)
            block = [s for i, s in items if i >= start]
            items = [(i, s) for i, s in items if i < start]
            items.append(
                (start, {"kind": "repeat", "reps": int(values.get(4) or 1), "steps": block})
            )
        else:
            items.append((index, dsl(values)))
    return Workout.from_dict(
        {
            "name": parsed["workout"].get(8) or "Workout",
            "date": date.isoformat(),
            "sport": SPORT_BACK.get(parsed["workout"].get(4), "running"),
            "steps": [s for _, s in items],
        }
    )
