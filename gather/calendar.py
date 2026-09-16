"""
Calendar gatherer: today's events from one or more Google Calendar secret
iCal addresses.

Contract (see ``render/sample_data.json``)::

    [{"time": "All day" | "9:30", "title": str, "where": str (optional)}, ...]

All-day events first, then by start time. The window is today 00:00 through
24:00 in ``Env.tz()`` and *all* date math is done in that zone; the classic
bug here is an event stored in UTC landing on the wrong local day.

Times are bare clock times without am/pm ("7:30", "12:00", "3:00"): the rail
column is narrow and the order of the list makes morning and afternoon
obvious.

Declined events
---------------
An event is skipped when the reader has declined it. Because an iCal feed
carries no explicit "this is you" marker, the owner is identified from the
calendar's ``X-WR-CALNAME`` (for a Google secret iCal address of a personal
calendar this is the owner's email address), and the rule is:

1. If any ``ATTENDEE`` matches the owner -- its ``mailto:`` address or its
   ``CN`` equals ``X-WR-CALNAME``, case-insensitively -- then the event is
   skipped when that attendee's ``PARTSTAT`` is ``DECLINED``, and kept
   otherwise. The owner's own answer wins outright.
2. Otherwise (no owner match, e.g. a shared calendar whose name is not an
   address), the event is skipped only when it has attendees and *every*
   one of them has declined.
3. An event with no ``ATTENDEE`` at all is always kept.

``STATUS:CANCELLED`` events are skipped before any of this.

A calendar that fails to fetch or parse is logged and skipped; the other
calendars still produce their events.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

from app.settings import Env, Settings

log = logging.getLogger(__name__)

TIMEOUT = 20  # seconds, per calendar
ALL_DAY = "All day"


# --------------------------------------------------------------- fetching
def _http_url(url: str) -> str:
    """Google hands out ``webcal://`` links from some buttons; requests can't."""
    u = url.strip()
    if u.lower().startswith("webcal://"):
        return "https://" + u[len("webcal://"):]
    return u


def _download(url: str) -> str:
    r = requests.get(_http_url(url), timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- parsing
def _calendar_name(cal: icalendar.Calendar) -> str:
    """X-WR-CALNAME, falling back to PRODID (the plan's fallback)."""
    for key in ("X-WR-CALNAME", "PRODID"):
        value = cal.get(key)
        if value:
            return str(value).strip()
    return ""


def _owner(cal: icalendar.Calendar) -> str:
    """The identity used to recognise the reader among an event's attendees."""
    name = cal.get("X-WR-CALNAME")
    return str(name).strip().lower() if name else ""


def _attendees(event: icalendar.Event) -> list[tuple[str, str, str]]:
    """[(email, cn, partstat), ...] for every ATTENDEE on the event."""
    raw = event.get("ATTENDEE")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out: list[tuple[str, str, str]] = []
    for a in raw:
        value = str(a).strip()
        email = value[7:] if value.lower().startswith("mailto:") else value
        params = getattr(a, "params", {}) or {}
        cn = str(params.get("CN", "")).strip().lower()
        partstat = str(params.get("PARTSTAT", "NEEDS-ACTION")).strip().upper()
        out.append((email.lower(), cn, partstat))
    return out


def _is_declined(event: icalendar.Event, owner: str) -> bool:
    """See the module docstring for the rule."""
    attendees = _attendees(event)
    if not attendees:
        return False
    if owner:
        mine = [a for a in attendees if a[0] == owner or a[1] == owner]
        if mine:
            return any(partstat == "DECLINED" for _, _, partstat in mine)
    return all(partstat == "DECLINED" for _, _, partstat in attendees)


def _is_cancelled(event: icalendar.Event) -> bool:
    status = event.get("STATUS")
    return bool(status) and str(status).strip().upper() == "CANCELLED"


def _where(event: icalendar.Event) -> str:
    """First line of LOCATION; Google puts a full postal address in there."""
    loc = event.get("LOCATION")
    if not loc:
        return ""
    for line in str(loc).splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _as_local(value: Any, tz: ZoneInfo) -> Optional[dt.datetime]:
    """A DTSTART/DTEND value as an aware datetime in `tz`. Dates -> midnight."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:                 # floating time: it is local time
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=tz)
    return None


def _clock(when: dt.datetime) -> str:
    """18:30 -> "6:30"; noon and midnight -> "12:00" / "12:15"."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d}"


def _day_bounds(today: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(today.year, today.month, today.day, tzinfo=tz)
    return start, start + dt.timedelta(days=1)


def _events_from_ics(
    text: str,
    *,
    today: dt.date,
    tz: ZoneInfo,
) -> list[tuple[int, dt.datetime, str, dict]]:
    """Sortable (all_day_rank, start, title, event) tuples for one calendar."""
    cal = icalendar.Calendar.from_ical(text)
    owner = _owner(cal)
    day_start, day_end = _day_bounds(today, tz)

    occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(
        day_start, day_end
    )

    rows: list[tuple[int, dt.datetime, str, dict]] = []
    for occurrence in occurrences:
        if _is_cancelled(occurrence) or _is_declined(occurrence, owner):
            continue

        raw_start = occurrence.get("DTSTART")
        if raw_start is None:
            continue
        start_value = raw_start.dt
        start = _as_local(start_value, tz)
        if start is None:
            continue

        raw_end = occurrence.get("DTEND")
        end = _as_local(raw_end.dt, tz) if raw_end is not None else start

        # `between` is inclusive of its stop; an event that begins exactly at
        # tomorrow's midnight belongs to tomorrow.
        if start >= day_end:
            continue
        # ... and one that has already ended before today began is not today's
        # either (it can only be here because it started earlier).
        if end <= day_start:
            continue

        dated = not isinstance(start_value, dt.datetime)     # VALUE=DATE
        spans_today = start <= day_start and end >= day_end
        all_day = dated or spans_today

        title = str(occurrence.get("SUMMARY") or "").strip()
        if not title:
            continue

        event: dict[str, str] = {
            "time": ALL_DAY if all_day else _clock(start),
            "title": title,
        }
        where = _where(occurrence)
        if where:
            event["where"] = where

        rows.append((0 if all_day else 1, start, title, event))
    return rows


def _sorted(rows: Iterable[tuple[int, dt.datetime, str, dict]]) -> list[dict]:
    return [row[3] for row in sorted(rows, key=lambda r: (r[0], r[1], r[2]))]


# ------------------------------------------------------------------- API
def fetch(settings: Settings, *, today: Optional[dt.date] = None) -> list[dict]:
    """Today's events across every configured calendar, merged and ordered."""
    tz = ZoneInfo(Env.tz())
    day = today or dt.datetime.now(tz).date()

    rows: list[tuple[int, dt.datetime, str, dict]] = []
    for source in settings.sources.calendars:
        if not source.url:
            continue
        label = source.name or "calendar"
        try:
            text = _download(source.url)
            rows.extend(_events_from_ics(text, today=day, tz=tz))
        except Exception:
            # House rule 3: one bad calendar never costs us the others.
            log.exception("calendar %s failed; skipping it", label)
            continue
    return _sorted(rows)


def check(url: str, *, today: Optional[dt.date] = None) -> dict:
    """For the Sources tab's "Check" button: the calendar's name and count.

    Raises on a failed fetch or unparseable feed so the web page can show the
    error next to the field.
    """
    tz = ZoneInfo(Env.tz())
    day = today or dt.datetime.now(tz).date()
    text = _download(url)
    cal = icalendar.Calendar.from_ical(text)
    rows = _events_from_ics(text, today=day, tz=tz)
    return {"name": _calendar_name(cal), "events_today": len(rows)}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(Settings.load()), indent=2))
