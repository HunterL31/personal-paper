"""
House rule 3: the paper is produced even when every source fails.

The gatherers are stubbed with modules injected into `sys.modules`, so these
tests hold whether or not the real modules exist yet, and never touch the
network.
"""
from __future__ import annotations

import sys
import time
import types

import pytest

import gather
from app.settings import Settings

SECTIONS = ["weather", "events", "tasks", "articles"]
MODULES = {
    "weather": "gather.weather",
    "events": "gather.calendar",
    "tasks": "gather.tasks",
    "articles": "gather.substack",
}

GOOD_WEATHER = {
    "summary": "Sunny all day", "high": 68, "low": 55, "wind": "W 12 mph",
    "sunrise": "6:53 a.m.", "sunset": "7:15 p.m.", "hourly": [],
}


def stub(monkeypatch, section: str, fetch, **extra) -> types.ModuleType:
    name = MODULES[section]
    module = types.ModuleType(name)
    module.fetch = fetch
    for key, value in extra.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def boom(_settings):
    raise RuntimeError("source is down")


@pytest.fixture
def settings():
    return Settings()


def test_shape_matches_the_contract(monkeypatch, settings, sample_data):
    stub(monkeypatch, "weather", lambda s: GOOD_WEATHER)
    stub(monkeypatch, "events", lambda s: sample_data["events"])
    stub(monkeypatch, "tasks", lambda s: sample_data["tasks"])
    stub(monkeypatch, "articles", lambda s: sample_data["articles"])

    data = gather.run_all(settings)

    assert set(data) == set(sample_data) | {"errors"}
    assert data["errors"] == {}
    assert set(data["paper"]) == set(sample_data["paper"])
    assert data["paper"] == {
        "name": settings.look.paper_name,
        "volume": "",          # run.py fills these two
        "date": "",
        "imprint": settings.look.imprint,
        "price": settings.look.price,
    }
    assert data["events"] == sample_data["events"]
    assert data["tasks"] == sample_data["tasks"]
    assert data["articles"] == sample_data["articles"]
    assert data["weather"] == GOOD_WEATHER


def test_every_gatherer_failing_still_produces_a_paper(monkeypatch, settings, caplog):
    for section in SECTIONS:
        stub(monkeypatch, section, boom)

    with caplog.at_level("ERROR"):
        data = gather.run_all(settings)

    assert set(data["errors"]) == set(SECTIONS)
    assert all("source is down" in reason for reason in data["errors"].values())
    assert data["events"] == [] and data["tasks"] == [] and data["articles"] == []
    assert data["weather"]["summary"] == "Forecast unavailable"
    assert data["weather"]["hourly"] == []
    assert data["paper"]["name"] == settings.look.paper_name
    assert "Traceback" in caplog.text


def test_weather_empty_value_comes_from_the_module(monkeypatch, settings):
    stub(
        monkeypatch, "weather", boom,
        unavailable=lambda: {"summary": "Forecast unavailable", "hourly": [],
                             "high": None, "low": None, "wind": "",
                             "sunrise": "", "sunset": ""},
    )
    for section in ("events", "tasks", "articles"):
        stub(monkeypatch, section, lambda s: [])

    data = gather.run_all(settings)
    assert data["weather"]["summary"] == "Forecast unavailable"
    assert "weather" in data["errors"]


def test_a_missing_module_is_only_an_error_entry(monkeypatch, settings):
    monkeypatch.setitem(sys.modules, "gather.calendar", None)   # import fails
    stub(monkeypatch, "weather", lambda s: GOOD_WEATHER)
    stub(monkeypatch, "tasks", lambda s: ["Water the fig tree"])
    stub(monkeypatch, "articles", lambda s: [])

    data = gather.run_all(settings)

    assert data["events"] == []
    assert "events" in data["errors"]
    assert data["tasks"] == ["Water the fig tree"]      # the others still ran
    assert data["weather"] == GOOD_WEATHER


def test_a_hung_gatherer_does_not_hold_up_the_run(monkeypatch, settings):
    monkeypatch.setattr(gather, "TIMEOUT_SECONDS", 1.0)

    def hang(_settings):
        time.sleep(60)

    stub(monkeypatch, "tasks", hang)
    stub(monkeypatch, "weather", lambda s: GOOD_WEATHER)
    stub(monkeypatch, "events", lambda s: [])
    stub(monkeypatch, "articles", lambda s: [])

    started = time.monotonic()
    data = gather.run_all(settings)
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert data["tasks"] == []
    assert "timed out" in data["errors"]["tasks"]
    assert data["weather"] == GOOD_WEATHER          # the rest of the paper is intact
    assert set(data["errors"]) == {"tasks"}


def test_gatherers_run_in_parallel(monkeypatch, settings):
    monkeypatch.setattr(gather, "TIMEOUT_SECONDS", 5.0)

    def slow(value):
        def fetch(_settings):
            time.sleep(0.5)
            return value
        return fetch

    stub(monkeypatch, "weather", slow(GOOD_WEATHER))
    stub(monkeypatch, "events", slow([]))
    stub(monkeypatch, "tasks", slow([]))
    stub(monkeypatch, "articles", slow([]))

    started = time.monotonic()
    data = gather.run_all(settings)
    assert time.monotonic() - started < 1.5       # not 4 x 0.5 s
    assert data["errors"] == {}


def test_run_all_never_raises(monkeypatch, settings):
    class Exploding:
        def __getattr__(self, name):
            raise ValueError("nope")

    for section in SECTIONS:
        stub(monkeypatch, section, lambda s: Exploding().anything)

    data = gather.run_all(settings)
    assert set(data["errors"]) == set(SECTIONS)
