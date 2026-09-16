"""Offline tests for gather/weather.py, against the saved Open-Meteo response."""
from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

from app.settings import Settings
from gather import weather

TODAY = dt.date(2026, 9, 16)
TOMORROW = dt.date(2026, 9, 17)


# ------------------------------------------------------------- plumbing
class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def la_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")


@pytest.fixture
def payload(fixtures):
    return json.loads((fixtures / "open_meteo.json").read_text())


@pytest.fixture
def served(monkeypatch, payload):
    """Serve the fixture from `requests.get` and record the call."""
    calls: list[dict] = []

    def fake_get(url, params=None, timeout=None, **kwargs):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(payload)

    monkeypatch.setattr(weather.requests, "get", fake_get)
    return calls


# --------------------------------------------------------- the contract
def test_matches_the_sample_data_contract(served, sample_data):
    assert weather.fetch(Settings(), today=TODAY) == sample_data["weather"]


def test_hourly_is_six_rows_at_the_named_hours(served):
    hourly = weather.fetch(Settings(), today=TODAY)["hourly"]
    assert [h["time"] for h in hourly] == [
        "7 a.m.", "10 a.m.", "1 p.m.", "4 p.m.", "7 p.m.", "10 p.m.",
    ]
    assert all(isinstance(h["temp"], int) for h in hourly)
    assert all(set(h) == {"time", "temp", "sky"} for h in hourly)


def test_temperatures_are_whole_numbers(served):
    w = weather.fetch(Settings(), today=TODAY)
    assert (w["high"], w["low"]) == (68, 55)      # 67.8 / 54.7 in the response


def test_sun_times_are_house_style(served):
    w = weather.fetch(Settings(), today=TODAY)
    assert w["sunrise"] == "6:53 a.m."
    assert w["sunset"] == "7:15 p.m."


def test_wind_is_compass_speed_unit(served):
    assert weather.fetch(Settings(), today=TODAY)["wind"] == "W 12 mph"


def test_a_second_day_of_the_same_response(served):
    w = weather.fetch(Settings(), today=TOMORROW)
    assert w["summary"] == "Rain through the afternoon"
    assert w["wind"] == "SSW 18 mph"
    assert [h["sky"] for h in w["hourly"]] == [
        "Mostly sunny", "Partly cloudy", "Rain", "Rain", "Rain", "Rain",
    ]


def test_sky_words_follow_the_sun(served):
    """Code 0 is "Sunny" by day and "Clear" once the sun is down."""
    hourly = weather.fetch(Settings(), today=TODAY)["hourly"]
    by_time = {h["time"]: h["sky"] for h in hourly}
    assert by_time["1 p.m."] == "Sunny"           # code 0, broad daylight
    assert by_time["7 p.m."] == "Clear"           # code 0, sunset is 7:15
    assert by_time["7 a.m."] == "Fog"


# ------------------------------------------------------------- the query
def test_open_meteo_query(served):
    settings = Settings()
    settings.sources.weather.lat = 37.7749
    settings.sources.weather.lon = -122.4194
    weather.fetch(settings, today=TODAY)

    call = served[0]
    assert call["url"] == "https://api.open-meteo.com/v1/forecast"
    assert call["timeout"] == 20
    assert call["params"] == {
        "latitude": 37.7749,
        "longitude": -122.4194,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,"
                 "sunset,wind_direction_10m_dominant,wind_speed_10m_max",
        "hourly": "temperature_2m,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "America/Los_Angeles",
        "forecast_days": 2,
    }


def test_timezone_follows_the_env(monkeypatch, payload, served):
    monkeypatch.setenv("TZ", "America/New_York")
    weather.fetch(Settings(), today=TODAY)
    assert served[0]["params"]["timezone"] == "America/New_York"


# -------------------------------------------------------- code mapping
@pytest.mark.parametrize("code,word", [
    (0, "Sunny"), (1, "Mostly sunny"), (2, "Partly cloudy"), (3, "Overcast"),
    (45, "Fog"), (48, "Fog"), (51, "Drizzle"), (57, "Drizzle"),
    (61, "Rain"), (65, "Rain"), (67, "Rain"), (71, "Snow"), (77, "Snow"),
    (80, "Showers"), (82, "Showers"), (95, "Storms"), (99, "Storms"),
])
def test_wmo_codes_become_short_words(code, word):
    assert weather.sky(code) == word


def test_night_words():
    assert weather.sky(0, daylight=False) == "Clear"
    assert weather.sky(1, daylight=False) == "Mostly clear"
    assert weather.sky(45, daylight=False) == "Fog"


@pytest.mark.parametrize("degrees,point", [
    (0, "N"), (11, "N"), (23, "NNE"), (45, "NE"), (90, "E"), (135, "SE"),
    (180, "S"), (200, "SSW"), (225, "SW"), (270, "W"), (272, "W"),
    (315, "NW"), (340, "NNW"), (359, "N"),
])
def test_wind_direction_to_compass(degrees, point):
    assert weather.compass(degrees) == point


# ------------------------------------------------------------- summaries
@pytest.mark.parametrize("morning,afternoon,expected", [
    ([45, 45], [0, 0], "Fog early, clearing by noon"),
    ([0, 0], [0, 1], "Sunny all day"),
    ([3, 3], [61, 63], "Rain through the afternoon"),
    ([3, 3], [80, 81], "Cloudy, showers later"),
    ([61, 63], [0, 0], "Rain early, then sun"),
    ([80, 80], [3, 3], "Showers early, then dry"),
    ([0, 0], [3, 3], "Sunny, clouding over later"),
    ([3, 3], [3, 3], "Cloudy all day"),
    ([45, 45], [45, 48], "Fog all day"),
    ([51, 51], [95, 95], "Drizzle, then storms"),
    ([71, 71], [71, 73], "Snow through the day"),
])
def test_summary_rules(morning, afternoon, expected):
    assert weather.summarize(morning, afternoon) == expected


@pytest.mark.parametrize("morning", [0, 1, 2, 3, 45, 51, 61, 71, 80, 95])
@pytest.mark.parametrize("afternoon", [0, 1, 2, 3, 45, 51, 61, 71, 80, 95])
def test_every_summary_fits_the_ear_box(morning, afternoon):
    text = weather.summarize([morning], [afternoon])
    assert 0 < len(text) < 30, text
    assert text[0].isupper()


def test_summary_falls_back_to_the_daily_code(monkeypatch, payload):
    """No hourly block: the day's own weather code still has to say something."""
    payload["hourly"] = {"time": [], "temperature_2m": [], "weather_code": []}
    monkeypatch.setattr(weather.requests, "get",
                        lambda *a, **kw: FakeResponse(payload))
    w = weather.fetch(Settings(), today=TODAY)
    assert w["summary"] == "Fog all day"          # daily weather_code is 45
    assert w["hourly"] == []


# ---------------------------------------------------------------- errors
def test_fetch_raises_so_run_all_can_substitute(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(weather.requests, "get", boom)
    with pytest.raises(requests.ConnectionError):
        weather.fetch(Settings(), today=TODAY)


def test_missing_day_raises(served):
    with pytest.raises(ValueError):
        weather.fetch(Settings(), today=dt.date(2026, 9, 20))


def test_unavailable_is_the_same_shape(served, sample_data):
    out = weather.unavailable()
    assert set(out) == set(sample_data["weather"])
    assert set(out) == set(weather.fetch(Settings(), today=TODAY))
    assert out["summary"] == "Forecast unavailable"
    assert out["hourly"] == []
    assert all(out[k] == "—"
               for k in ("high", "low", "wind", "sunrise", "sunset"))


# ----------------------------------------------------------------- check
def test_check_returns_a_forecast_for_a_lat_lon(served):
    out = weather.check(45.52, -122.68, today=TODAY)
    assert out["summary"] == "Fog early, clearing by noon"
    assert len(out["hourly"]) == 6
    assert served[0]["params"]["latitude"] == 45.52
    assert served[0]["params"]["longitude"] == -122.68
