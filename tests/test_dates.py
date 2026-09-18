from datetime import date

from app.dates import DATE_FORMATS, format_date, samples


def test_every_style_renders_one_date():
    d = date(2026, 9, 16)
    got = {k: format_date(d, k) for k in DATE_FORMATS}
    assert got["long"] == "Wednesday, September 16, 2026"
    assert got["long_ap"] == "Wednesday, Sept. 16, 2026"
    assert got["ap"] == "Wed. Sept. 16, 2026"
    assert got["day_month"] == "Wednesday 16 September 2026"
    assert got["iso"] == "2026-09-16"
    assert got["numeric_us"] == "9/16/2026"
    assert got["weekday_only"] == "Wednesday"


def test_ap_never_abbreviates_the_short_months():
    assert format_date(date(2026, 3, 2), "ap") == "Mon. March 2, 2026"
    assert format_date(date(2026, 7, 4), "long_ap") == "Saturday, July 4, 2026"


def test_unknown_style_falls_back_to_the_long_form():
    assert format_date(date(2026, 9, 16), "nonsense") == "Wednesday, September 16, 2026"
    assert format_date(date(2026, 9, 16), None) == "Wednesday, September 16, 2026"


def test_samples_cover_every_style():
    rows = samples(date(2026, 9, 16))
    assert [r["key"] for r in rows] == list(DATE_FORMATS)
    assert all(r["sample"] and r["label"] for r in rows)
