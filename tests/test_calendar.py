"""Offline tests for gather/calendar.py, against the .ics fixtures."""
from __future__ import annotations

import datetime as dt

import pytest

from app.settings import CalendarSource, Settings
from gather import calendar as cal

TODAY = dt.date(2026, 9, 16)          # a Wednesday, PDT (UTC-7)
BASIC = "https://calendar.example/basic/basic.ics"
SECOND = "https://calendar.example/second/second.ics"


# ------------------------------------------------------------- plumbing
class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def la_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")


@pytest.fixture
def ics(fixtures):
    return {
        BASIC: (fixtures / "calendar_basic.ics").read_text(),
        SECOND: (fixtures / "calendar_second.ics").read_text(),
    }


@pytest.fixture
def served(monkeypatch, ics):
    """Serve the fixtures from `requests.get`; nothing touches the network."""
    calls: list[str] = []

    def fake_get(url, timeout=None, **kwargs):
        calls.append(url)
        assert timeout == 20, "every calendar fetch needs the 20s timeout"
        if url not in ics:
            raise AssertionError(f"unexpected URL {url}")
        return FakeResponse(ics[url])

    monkeypatch.setattr(cal.requests, "get", fake_get)
    return calls


def settings_for(*urls: str) -> Settings:
    s = Settings()
    s.sources.calendars = [CalendarSource(url=u) for u in urls]
    return s


# ---------------------------------------------------------------- shape
def test_merges_both_calendars_in_order(served):
    events = cal.fetch(settings_for(BASIC, SECOND), today=TODAY)

    assert events == [
        {"time": "All day", "title": "Sarah visiting", "where": "Guest room"},
        {"time": "All day", "title": "Library books due"},
        {"time": "All day", "title": "Recycling goes out tonight"},
        {"time": "12:15", "title": "Flight check-in opens"},
        {"time": "7:30", "title": "Pilates"},
        {"time": "8:00", "title": "School drop-off",
         "where": "Alvarado Elementary"},
        {"time": "9:30", "title": "Coffee with Dana",
         "where": "Corner cafe on 24th"},
        {"time": "3:00", "title": "Dentist", "where": "Dr. Okafor, suite 210"},
        {"time": "6:30", "title": "Dinner reservation for two"},
    ]
    assert served == [BASIC, SECOND]


def test_all_day_first_then_by_start_time(served):
    events = cal.fetch(settings_for(BASIC, SECOND), today=TODAY)
    all_day = [e for e in events if e["time"] == cal.ALL_DAY]
    assert events[:len(all_day)] == all_day       # they lead the list
    # and the timed ones run 00:15, 07:30, 08:00, 09:30, 15:00, 18:30
    assert [e["time"] for e in events[len(all_day):]] == [
        "12:15", "7:30", "8:00", "9:30", "3:00", "6:30",
    ]


def test_times_have_no_am_pm_and_no_leading_zero(served):
    for e in cal.fetch(settings_for(BASIC, SECOND), today=TODAY):
        if e["time"] == cal.ALL_DAY:
            continue
        assert "m." not in e["time"] and "M" not in e["time"]
        assert not e["time"].startswith("0")
        hour, minute = e["time"].split(":")
        assert 1 <= int(hour) <= 12 and len(minute) == 2


def test_multi_day_event_spanning_today_is_all_day(served):
    events = cal.fetch(settings_for(BASIC), today=TODAY)
    visiting = next(e for e in events if e["title"] == "Sarah visiting")
    assert visiting["time"] == cal.ALL_DAY     # it started Tuesday, ends Friday


def test_where_is_only_the_first_line_of_location(served):
    events = cal.fetch(settings_for(BASIC), today=TODAY)
    dentist = next(e for e in events if e["title"] == "Dentist")
    assert dentist["where"] == "Dr. Okafor, suite 210"
    assert "Valencia" not in dentist["where"]
    # an event with no LOCATION has no `where` key at all
    assert "where" not in next(e for e in events if e["title"] == "Pilates")


def test_no_calendars_configured_is_empty(served):
    assert cal.fetch(Settings(), today=TODAY) == []


# ------------------------------------------------------------ exclusions
def test_cancelled_event_is_skipped(served):
    titles = [e["title"] for e in cal.fetch(settings_for(BASIC), today=TODAY)]
    assert "Contractor walkthrough" not in titles


def test_event_declined_by_the_calendar_owner_is_skipped(served):
    titles = [e["title"] for e in cal.fetch(settings_for(BASIC), today=TODAY)]
    # X-WR-CALNAME is molly@example.com, whose ATTENDEE line is DECLINED
    assert "Quarterly planning sync" not in titles
    # ... while the one she accepted stays
    assert "Dentist" in titles


def test_owner_answer_wins_when_others_declined(fixtures):
    """Rule 1: the owner's own PARTSTAT decides, whatever the others said."""
    text = (fixtures / "calendar_basic.ics").read_text().replace(
        "ATTENDEE;CN=Ravi Patel;PARTSTAT=ACCEPTED:mailto:ravi@work.example",
        "ATTENDEE;CN=Ravi Patel;PARTSTAT=DECLINED:mailto:ravi@work.example",
    ).replace(
        "ATTENDEE;CN=molly@example.com;PARTSTAT=DECLINED;RSVP=FALSE:mailto:mol"
        "ly@exam\n ple.com",
        "ATTENDEE;CN=molly@example.com;PARTSTAT=ACCEPTED:mailto:molly@example.com",
    )
    rows = cal._events_from_ics(text, today=TODAY, tz=cal.ZoneInfo("America/Los_Angeles"))
    titles = [r[3]["title"] for r in rows]
    assert "Quarterly planning sync" in titles


def test_event_all_of_whose_attendees_declined_is_skipped(served):
    """Rule 2: on a calendar whose name is not an address, all-declined goes."""
    titles = [e["title"] for e in cal.fetch(settings_for(SECOND), today=TODAY)]
    assert "Block party planning" not in titles
    assert "School drop-off" in titles          # no attendees at all: kept


# ----------------------------------------------------- the date boundary
def test_off_by_one_around_midnight(served):
    """The classic bug: 23:30 yesterday out, 00:15 today in."""
    events = cal.fetch(settings_for(BASIC), today=TODAY)
    titles = [e["title"] for e in events]

    assert "Put the bins out" not in titles          # 2026-09-15 23:30 local
    checkin = next(e for e in events if e["title"] == "Flight check-in opens")
    assert checkin["time"] == "12:15"                # 2026-09-16 00:15 local

    # and yesterday's own view is the mirror image of that
    yesterday = [e["title"] for e in
                 cal.fetch(settings_for(BASIC), today=dt.date(2026, 9, 15))]
    assert "Put the bins out" in yesterday
    assert "Flight check-in opens" not in yesterday


def test_utc_stored_event_lands_on_the_local_day(served):
    """20260917T013000Z is 18:30 *today* in Los Angeles, not tomorrow."""
    events = cal.fetch(settings_for(BASIC), today=TODAY)
    dinner = next(e for e in events if e["title"] == "Dinner reservation for two")
    assert dinner["time"] == "6:30"

    tomorrow = [e["title"] for e in
                cal.fetch(settings_for(BASIC), today=dt.date(2026, 9, 17))]
    assert "Dinner reservation for two" not in tomorrow

    # the other direction: 20260916T043000Z is 21:30 *yesterday* local
    assert "Last night's book club" not in [e["title"] for e in events]
    assert "Last night's book club" in [
        e["title"] for e in cal.fetch(settings_for(BASIC), today=dt.date(2026, 9, 15))
    ]


# ------------------------------------------------------------ recurrence
def test_weekly_recurrence_is_expanded(served):
    """Without recurring-ical-events the weekly Pilates would simply vanish."""
    for wednesday in (dt.date(2026, 9, 16), dt.date(2026, 9, 23), dt.date(2026, 10, 7)):
        titles = [e["title"] for e in cal.fetch(settings_for(BASIC), today=wednesday)]
        assert "Pilates" in titles, wednesday

    thursday = [e["title"] for e in
                cal.fetch(settings_for(BASIC), today=dt.date(2026, 9, 17))]
    assert "Pilates" not in thursday


def test_recurrence_survives_the_dst_change(served):
    """2026-11-04 is a Wednesday in PST; the class is still at 7:30 local."""
    events = cal.fetch(settings_for(BASIC), today=dt.date(2026, 11, 4))
    pilates = next(e for e in events if e["title"] == "Pilates")
    assert pilates["time"] == "7:30"


# --------------------------------------------------------------- failure
def test_a_failing_calendar_does_not_lose_the_others(monkeypatch, ics, caplog):
    def fake_get(url, timeout=None, **kwargs):
        if url == BASIC:
            raise RuntimeError("502 from the calendar host")
        return FakeResponse(ics[url])

    monkeypatch.setattr(cal.requests, "get", fake_get)
    events = cal.fetch(settings_for(BASIC, SECOND), today=TODAY)

    assert [e["title"] for e in events] == ["Library books due", "School drop-off"]
    assert "failed" in caplog.text


def test_unparseable_calendar_is_skipped(monkeypatch, ics):
    def fake_get(url, timeout=None, **kwargs):
        return FakeResponse("this is not a calendar" if url == BASIC else ics[url])

    monkeypatch.setattr(cal.requests, "get", fake_get)
    assert [e["title"] for e in cal.fetch(settings_for(BASIC, SECOND), today=TODAY)] == [
        "Library books due", "School drop-off",
    ]


# ----------------------------------------------------------------- check
def test_check_reports_name_and_count(served):
    assert cal.check(BASIC, today=TODAY) == {
        "name": "molly@example.com", "events_today": 7,
    }
    assert cal.check(SECOND, today=TODAY) == {
        "name": "Family", "events_today": 2,
    }


def test_check_falls_back_to_prodid(monkeypatch, ics):
    text = "\n".join(line for line in ics[BASIC].splitlines()
                     if not line.startswith("X-WR-CALNAME"))
    monkeypatch.setattr(cal.requests, "get",
                        lambda url, timeout=None, **kw: FakeResponse(text))
    assert cal.check(BASIC, today=TODAY)["name"] == (
        "-//Google Inc//Google Calendar 70.9054//EN"
    )


def test_webcal_urls_are_fetched_over_https(monkeypatch, ics):
    seen: list[str] = []

    def fake_get(url, timeout=None, **kwargs):
        seen.append(url)
        return FakeResponse(ics[BASIC])

    monkeypatch.setattr(cal.requests, "get", fake_get)
    cal.fetch(settings_for("webcal://calendar.example/basic/basic.ics"), today=TODAY)
    assert seen == ["https://calendar.example/basic/basic.ics"]
