"""
How the date on the folio line is written.

One table, so the Look tab can show today's date in every style and the
run can print the one the reader chose. No `%-d`: it is glibc-only, and
the container's C library is not the reader's business.
"""
from __future__ import annotations

from datetime import date, datetime

# AP-style month abbreviations: March, April, May, June and July are never cut.
AP_MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]
AP_DAYS = ["Mon.", "Tues.", "Wed.", "Thurs.", "Fri.", "Sat.", "Sun."]

DEFAULT_DATE_FORMAT = "long"


def _parts(d: date | datetime) -> dict[str, str]:
    return {
        "weekday": f"{d:%A}",
        "wd_ap": AP_DAYS[d.weekday()],
        "wd3": f"{d:%a}",
        "month": f"{d:%B}",
        "mon_ap": AP_MONTHS[d.month - 1],
        "mon3": f"{d:%b}",
        "d": str(d.day),
        "dd": f"{d.day:02d}",
        "m": str(d.month),
        "mm": f"{d.month:02d}",
        "y": str(d.year),
        "yy": f"{d.year % 100:02d}",
    }


#: key -> (label for the Look tab, pattern over the parts above)
DATE_FORMATS: dict[str, tuple[str, str]] = {
    "long":        ("Weekday, Month day, year",        "{weekday}, {month} {d}, {y}"),
    "long_ap":     ("Weekday, abbreviated month",      "{weekday}, {mon_ap} {d}, {y}"),
    "ap":          ("Abbreviated weekday and month",   "{wd_ap} {mon_ap} {d}, {y}"),
    "month_day":   ("Month day, year",                 "{month} {d}, {y}"),
    "day_month":   ("Weekday day Month year",          "{weekday} {d} {month} {y}"),
    "day_month_short": ("day Month year",              "{d} {month} {y}"),
    "iso":         ("Year-month-day",                  "{y}-{mm}-{dd}"),
    "numeric_us":  ("Month/day/year",                  "{m}/{d}/{y}"),
    "numeric_eu":  ("day.month.year",                  "{d}.{m}.{y}"),
    "weekday_only": ("Weekday only",                   "{weekday}"),
}


def format_date(d: date | datetime, key: str | None = None) -> str:
    """The folio date in the chosen style; an unknown key gives the default."""
    _, pattern = DATE_FORMATS.get(key or DEFAULT_DATE_FORMAT, DATE_FORMATS[DEFAULT_DATE_FORMAT])
    return pattern.format(**_parts(d))


def samples(d: date | datetime | None = None) -> list[dict[str, str]]:
    """Every style rendered for one date, for the Look tab's picker."""
    d = d or date.today()
    return [{"key": k, "label": label, "sample": format_date(d, k)} for k, (label, _) in DATE_FORMATS.items()]
