"""
Weather gatherer: today's forecast from Open-Meteo (free, no API key).

Contract (see the ``weather`` object in ``render/sample_data.json``)::

    {"summary": "Fog early, clearing by noon",
     "high": 68, "low": 55, "wind": "W 12 mph",
     "sunrise": "6:53 a.m.", "sunset": "7:15 p.m.",
     "hourly": [{"time": "7 a.m.", "temp": 55, "sky": "Fog"}, ... six of them]}

The six hourly rows are 7, 10, 13, 16, 19 and 22 local time. ``summary`` is
rule-based from the morning (7--10) against the afternoon (13--16) weather
codes and is kept under 30 characters, because it lives in the ear box.

``fetch`` raises on a network or API failure -- ``gather.run_all`` catches it
and substitutes ``unavailable()``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from app.settings import Env, Settings

log = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 20  # seconds

DAILY = [
    "temperature_2m_max",
    "temperature_2m_min",
    "weather_code",
    "sunrise",
    "sunset",
    "wind_direction_10m_dominant",
    "wind_speed_10m_max",
]
HOURLY = ["temperature_2m", "weather_code"]
HOURS = (7, 10, 13, 16, 19, 22)

DASH = "—"  # em dash, for the unavailable shape

COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


# ------------------------------------------------------- WMO weather codes
def sky(code: int, *, daylight: bool = True) -> str:
    """A WMO code as the short word that goes in the `sky` column."""
    if code == 0:
        return "Sunny" if daylight else "Clear"
    if code == 1:
        return "Mostly sunny" if daylight else "Mostly clear"
    if code == 2:
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if 51 <= code <= 57:
        return "Drizzle"
    if 61 <= code <= 67:
        return "Rain"
    if 71 <= code <= 77:
        return "Snow"
    if 80 <= code <= 82:
        return "Showers"
    if 95 <= code <= 99:
        return "Storms"
    return "Unsettled"


# Categories for the summary, in order of how much they want mentioning.
_CATEGORIES = (
    "clear", "cloudy", "overcast", "fog", "drizzle", "showers", "rain",
    "snow", "storms",
)
_WET = {"drizzle", "showers", "rain", "snow", "storms"}
_WET_WORD = {
    "drizzle": "Drizzle", "showers": "Showers", "rain": "Rain",
    "snow": "Snow", "storms": "Storms",
}


def _category(code: int) -> str:
    if code in (0, 1):
        return "clear"
    if code == 2:
        return "cloudy"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67:
        return "rain"
    if 71 <= code <= 77:
        return "snow"
    if 80 <= code <= 82:
        return "showers"
    if 95 <= code <= 99:
        return "storms"
    return "cloudy"


def _dominant(codes: list[int]) -> str:
    """The category worth naming: the most notable one in the group."""
    cats = [_category(c) for c in codes] or ["clear"]
    return max(cats, key=_CATEGORIES.index)


_ALL_DAY = {
    "clear": "Sunny all day",
    "cloudy": "Partly cloudy all day",
    "overcast": "Cloudy all day",
    "fog": "Fog all day",
    "drizzle": "Drizzle on and off",
    "showers": "Showers on and off",
    "rain": "Rain through the day",
    "snow": "Snow through the day",
    "storms": "Storms through the day",
}
_AFTERNOON = {
    "drizzle": "Drizzle in the afternoon",
    "showers": "Showers in the afternoon",
    "rain": "Rain through the afternoon",
    "snow": "Snow in the afternoon",
    "storms": "Storms in the afternoon",
}
_DRY_TO_DRY = {
    ("clear", "cloudy"): "Sunny, clouds moving in",
    ("clear", "overcast"): "Sunny, clouding over later",
    ("clear", "fog"): "Sunny, fog late",
    ("cloudy", "clear"): "Cloudy early, sunny later",
    ("cloudy", "overcast"): "Clouding over",
    ("cloudy", "fog"): "Cloudy, fog rolling in",
    ("overcast", "clear"): "Cloudy early, sunny later",
    ("overcast", "cloudy"): "Cloudy, breaking up later",
    ("overcast", "fog"): "Cloudy, fog rolling in",
    ("fog", "clear"): "Fog early, clearing by noon",
    ("fog", "cloudy"): "Fog early, then clouds",
    ("fog", "overcast"): "Fog early, then clouds",
}


def summarize(morning_codes: list[int], afternoon_codes: list[int]) -> str:
    """The ear-box line, under 30 characters, from morning vs. afternoon."""
    morning = _dominant(morning_codes)
    afternoon = _dominant(afternoon_codes)

    if morning == afternoon:
        text = _ALL_DAY[morning]
    elif morning in _WET and afternoon not in _WET:
        word = _WET_WORD[morning]
        text = (f"{word} early, then sun" if afternoon == "clear"
                else f"{word} early, then dry")
    elif afternoon in _WET and morning not in _WET:
        if afternoon == "showers" and morning in ("cloudy", "overcast"):
            text = "Cloudy, showers later"
        else:
            text = _AFTERNOON[afternoon]
    elif morning in _WET and afternoon in _WET:
        text = f"{_WET_WORD[morning]}, then {_WET_WORD[afternoon].lower()}"
    else:
        text = _DRY_TO_DRY.get((morning, afternoon), _ALL_DAY[afternoon])

    if len(text) >= 30:                                  # belt and braces
        text = text[:29].rstrip(" ,")
    return text


# ----------------------------------------------------------------- pieces
def compass(degrees: float) -> str:
    """Wind direction in degrees as a 16-point abbreviation."""
    return COMPASS[int((float(degrees) / 22.5) + 0.5) % 16]


def _clock(when: dt.datetime) -> str:
    """A time as "6:53 a.m." / "7:15 p.m." (the paper's house style)."""
    hour = when.hour % 12 or 12
    half = "a.m." if when.hour < 12 else "p.m."
    return f"{hour}:{when.minute:02d} {half}"


def _short_clock(hour: int) -> str:
    """An hourly row's label: "7 a.m.", "1 p.m.", "10 p.m."."""
    return f"{hour % 12 or 12} {'a.m.' if hour < 12 else 'p.m.'}"


def _parse(stamp: str) -> dt.datetime:
    """Open-Meteo returns local naive ISO stamps when `timezone` is set."""
    return dt.datetime.fromisoformat(stamp)


# --------------------------------------------------------------- fetching
def _request(lat: float, lon: float, tz: str) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY),
        "hourly": ",".join(HOURLY),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": tz,
        "forecast_days": 2,
    }
    r = requests.get(API_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _build(payload: dict, day: dt.date) -> dict:
    daily = payload["daily"]
    hourly = payload["hourly"]
    iso = day.isoformat()

    try:
        d = daily["time"].index(iso)
    except ValueError as exc:                    # the API skipped our day
        raise ValueError(f"no daily forecast for {iso}") from exc

    sunrise = _parse(daily["sunrise"][d])
    sunset = _parse(daily["sunset"][d])

    by_time = {t: i for i, t in enumerate(hourly["time"])}
    rows: list[dict] = []
    codes: dict[int, int] = {}
    for hour in HOURS:
        i = by_time.get(f"{iso}T{hour:02d}:00")
        if i is None:
            log.warning("no hourly forecast for %s %02d:00", iso, hour)
            continue
        code = int(hourly["weather_code"][i])
        codes[hour] = code
        # The row describes a whole hour, so judge day or night by its middle:
        # the 7 p.m. row is "Clear", not "Sunny", when the sun sets at 7:15.
        middle = dt.datetime.combine(day, dt.time(hour)) + dt.timedelta(minutes=30)
        rows.append({
            "time": _short_clock(hour),
            "temp": round(hourly["temperature_2m"][i]),
            "sky": sky(code, daylight=sunrise <= middle <= sunset),
        })

    # Morning 7-10 and afternoon 13-16 drive the summary; fall back to the
    # day's own code if the hourly block did not reach us.
    day_code = int(daily["weather_code"][d])
    morning = [codes[h] for h in (7, 10) if h in codes] or [day_code]
    afternoon = [codes[h] for h in (13, 16) if h in codes] or [day_code]

    return {
        "summary": summarize(morning, afternoon),
        "high": round(daily["temperature_2m_max"][d]),
        "low": round(daily["temperature_2m_min"][d]),
        "wind": f"{compass(daily['wind_direction_10m_dominant'][d])} "
                f"{round(daily['wind_speed_10m_max'][d])} mph",
        "sunrise": _clock(sunrise),
        "sunset": _clock(sunset),
        "hourly": rows,
    }


# ------------------------------------------------------------------- API
def fetch(settings: Settings, *, today: Optional[dt.date] = None) -> dict:
    """Today's forecast. Raises on failure; `run_all` substitutes
    `unavailable()`."""
    tz_name = Env.tz()
    day = today or dt.datetime.now(ZoneInfo(tz_name)).date()
    where = settings.sources.weather
    payload = _request(where.lat, where.lon, tz_name)
    return _build(payload, day)


def unavailable() -> dict:
    """The empty shape, for a run where the forecast could not be had."""
    return {
        "summary": "Forecast unavailable",
        "high": DASH,
        "low": DASH,
        "wind": DASH,
        "sunrise": DASH,
        "sunset": DASH,
        "hourly": [],
    }


def check(lat: float, lon: float, *, today: Optional[dt.date] = None) -> dict:
    """For the Sources tab's "Check" button: today's forecast for a lat/lon.

    Raises on failure so the page can show the error beside the fields.
    """
    tz_name = Env.tz()
    day = today or dt.datetime.now(ZoneInfo(tz_name)).date()
    return _build(_request(float(lat), float(lon), tz_name), day)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(Settings.load()), indent=2, ensure_ascii=False))
