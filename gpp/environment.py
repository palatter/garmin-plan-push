"""Weather and daylight: heat-adjusted pace, WBGT bands, sunrise/sunset.

Heat is the environmental factor with a usable rule of thumb behind it, so
that is the one that adjusts pace. Everything else here informs rather than
prescribes.

  * Dew-point rule (Runners Connect, widely used by coaches): add 0.025
    min/mile of pace for every degree Fahrenheit of dew point above 60 F.
    Converted to SI below. The combined temperature + dew point rule gives
    the bands: under 100 nothing, 100-180 adjust, 180 and up no hard running.
  * Simplified WBGT (Australian Bureau of Meteorology): 0.567 T + 0.393 e +
    3.94 with e the vapour pressure in hPa. Reported against the ACSM flag
    bands. We do not claim a pace formula from WBGT -- the studies fit
    marathon fields, not individuals -- so it is shown as a risk band.
  * Sunrise/sunset: the NOAA solar calculator, accurate to a few minutes,
    for scheduling early or late runs.
  * Altitude is deliberately absent: no formula was found with evidence
    behind it (ROADMAP #75).

The only network call is `fetch_forecast`, which uses Open-Meteo because it
needs no API key. It is optional; every function accepts numbers you type.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .units import METRES_PER_MILE

DEW_POINT_THRESHOLD_F = 60.0
DEW_POINT_MIN_PER_MILE_PER_F = 0.025
COMBINED_ADJUST_FROM = 100.0
COMBINED_NO_HARD_FROM = 180.0


class EnvironmentError_(ValueError):
    """Bad weather inputs, or the forecast could not be fetched."""


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Magnus formula; good to ~0.4 C over ordinary outdoor ranges."""
    if not 0 < humidity_pct <= 100:
        raise EnvironmentError_("humidity must be between 0 and 100%")
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(humidity_pct / 100.0)
    return (b * gamma) / (a - gamma)


def heat_slowdown_spk(dew_point_celsius: float) -> float:
    """Seconds per km to ADD to a target pace for this dew point."""
    excess_f = max(0.0, c_to_f(dew_point_celsius) - DEW_POINT_THRESHOLD_F)
    minutes_per_mile = excess_f * DEW_POINT_MIN_PER_MILE_PER_F
    return minutes_per_mile * 60.0 / (METRES_PER_MILE / 1000.0)


def adjust_pace(pace_spk: float, temp_c: float, humidity_pct: float) -> float:
    """A pace target, slowed for the conditions."""
    return pace_spk + heat_slowdown_spk(dew_point_c(temp_c, humidity_pct))


def combined_band(temp_c: float, dew_point_celsius: float) -> str:
    """The coaches' temperature + dew point rule, as a band."""
    total = c_to_f(temp_c) + c_to_f(dew_point_celsius)
    if total >= COMBINED_NO_HARD_FROM:
        return "no-hard-running"
    if total >= COMBINED_ADJUST_FROM:
        return "adjust"
    return "normal"


def vapour_pressure_hpa(temp_c: float, humidity_pct: float) -> float:
    return humidity_pct / 100.0 * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))


def simplified_wbgt(temp_c: float, humidity_pct: float) -> float:
    return 0.567 * temp_c + 0.393 * vapour_pressure_hpa(temp_c, humidity_pct) + 3.94


def wbgt_band(wbgt_c: float) -> str:
    """ACSM flag categories for WBGT."""
    if wbgt_c < 18:
        return "low"
    if wbgt_c < 23:
        return "moderate"
    if wbgt_c < 28:
        return "high"
    return "extreme"


@dataclass
class Conditions:
    temp_c: float
    humidity_pct: float

    @property
    def dew_point_c(self) -> float:
        return dew_point_c(self.temp_c, self.humidity_pct)

    @property
    def wbgt(self) -> float:
        return simplified_wbgt(self.temp_c, self.humidity_pct)

    def describe(self, pace_spk: float | None = None) -> dict:
        dp = self.dew_point_c
        out = {
            "temp_c": round(self.temp_c, 1),
            "humidity_pct": round(self.humidity_pct),
            "dew_point_c": round(dp, 1),
            "wbgt_c": round(self.wbgt, 1),
            "wbgt_band": wbgt_band(self.wbgt),
            "band": combined_band(self.temp_c, dp),
            "slowdown_s_per_km": round(heat_slowdown_spk(dp), 1),
        }
        if pace_spk:
            out["adjusted_pace_spk"] = round(pace_spk + heat_slowdown_spk(dp), 1)
        return out


# --- forecast (optional, no key) --------------------------------------------

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def fetch_forecast(
    latitude: float, longitude: float, when: dt.datetime, timeout: float = 8.0
) -> Conditions:
    """Temperature and humidity at the hour of `when`, from Open-Meteo."""
    day = when.date().isoformat()
    query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "hourly": "temperature_2m,relative_humidity_2m",
            "start_date": day,
            "end_date": day,
            "timezone": "auto",
        }
    )
    try:
        with urllib.request.urlopen(f"{OPEN_METEO}?{query}", timeout=timeout) as resp:  # noqa: S310 - fixed https host
            payload = json.load(resp)
    except Exception as exc:
        raise EnvironmentError_(f"could not fetch the forecast: {exc}") from exc
    return parse_forecast(payload, when.hour)


def parse_forecast(payload: dict, hour: int) -> Conditions:
    hourly = payload.get("hourly") or {}
    temps = hourly.get("temperature_2m") or []
    hums = hourly.get("relative_humidity_2m") or []
    if not temps or not hums:
        raise EnvironmentError_("forecast had no hourly data")
    index = min(max(hour, 0), len(temps) - 1)
    return Conditions(temp_c=float(temps[index]), humidity_pct=float(hums[index]))


# --- daylight (NOAA) --------------------------------------------------------


def sun_times(latitude: float, longitude: float, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(sunrise, sunset) as timezone-aware UTC datetimes, NOAA method."""
    n = day.toordinal() - dt.date(2000, 1, 1).toordinal() + 0.5
    jd = 2451545.0 + n
    t = (jd - 2451545.0) / 36525.0
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anom)
    eq_centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = mean_long + eq_centre
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    obliq0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliq = obliq0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(obliq / 2)) ** 2
    eq_time = 4 * math.degrees(
        y * math.sin(2 * math.radians(mean_long))
        - 2 * ecc * math.sin(m)
        + 4 * ecc * y * math.sin(m) * math.cos(2 * math.radians(mean_long))
        - 0.5 * y * y * math.sin(4 * math.radians(mean_long))
        - 1.25 * ecc * ecc * math.sin(2 * m)
    )
    lat = math.radians(latitude)
    dec = math.radians(decl)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(dec))) - math.tan(
        lat
    ) * math.tan(dec)
    if cos_ha > 1 or cos_ha < -1:
        raise EnvironmentError_("no sunrise/sunset on this day at this latitude")
    ha = math.degrees(math.acos(cos_ha))
    noon_min = 720 - 4 * longitude - eq_time
    rise_min = noon_min - 4 * ha
    set_min = noon_min + 4 * ha
    base = dt.datetime.combine(day, dt.time(0), tzinfo=dt.UTC)
    return base + dt.timedelta(minutes=rise_min), base + dt.timedelta(minutes=set_min)


def daylight_note(
    latitude: float, longitude: float, day: dt.date, start_local: dt.time, utc_offset_hours: float
) -> str | None:
    """A one-line warning if a session starts before sunrise or ends after sunset."""
    try:
        rise, sett = sun_times(latitude, longitude, day)
    except EnvironmentError_:
        return None
    offset = dt.timedelta(hours=utc_offset_hours)
    rise_local, set_local = (rise + offset).time(), (sett + offset).time()
    if start_local < rise_local:
        return f"Starts before sunrise ({rise_local.strftime('%H:%M')}); bring a light."
    if start_local > set_local:
        return f"Starts after sunset ({set_local.strftime('%H:%M')}); bring a light."
    return None
