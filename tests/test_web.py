"""
The web layer: auth, each tab saving its own section, the Check endpoints
never crashing the page, `POST /tasks`, the archive route's name check, a
real sample preview, and the scheduler's trigger.

Nothing here touches the network: every gatherer call is monkeypatched but
the preview, which renders the sample issue in the bundled Chromium.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler
from app.settings import (
    DATE_PLACES,
    EAR_KINDS,
    FONT_CHOICES_BODY,
    FONT_CHOICES_HEAD,
    FONT_CHOICES_MASTHEAD,
    CalendarSource,
    EarBox,
    ListSource,
    Schedule,
    Settings,
    SubstackSource,
)

AUTH = ("reader", "pw")


@pytest.fixture(autouse=True)
def web_env(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "pw")
    monkeypatch.setenv("TASKS_TOKEN", "tok")
    monkeypatch.setenv("PAPER_NO_SCHEDULER", "1")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    jobs.reset()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def wait_for(job_id: str, timeout: float = 120.0) -> jobs.Job:
    """Poll the job table (not the HTTP endpoint) until the job finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get(job_id)
        assert job is not None
        if job.status != "running":
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


# ------------------------------------------------------------------- auth
def test_look_needs_a_password(client):
    response = client.get("/look")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("www-authenticate", "")


def test_look_with_auth(client):
    response = client.get("/look", auth=AUTH)
    assert response.status_code == 200
    assert "Personal Paper" in response.text


def test_wrong_password_is_refused(client):
    assert client.get("/look", auth=("reader", "nope")).status_code == 401


def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_root_redirects_to_preview(client):
    response = client.get("/", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/preview"


# ------------------------------------------------------------- saving tabs
def test_saving_look_keeps_the_other_sections(client):
    settings = Settings()
    settings.output.email.to = ["her@example.com"]
    settings.output.schedule.time = "05:45"
    settings.sources.substacks = [SubstackSource(name="oneuseful")]
    settings.save()

    response = client.post(
        "/look",
        auth=AUTH,
        data={
            "paper_name": "The Evening Ledger",
            "imprint": "Printed at home",
            "price": "Free",
            "ear_right_kind": "monogram",
            "ear_right_initials": "M. L.",
            "ear_right_lines": "One line\nAnother line\n",
            "masthead_font": "UnifrakturCook",
            "headline_font": "Playfair Display",
            "body_font": "EB Garamond",
            "body_size_pt": "10.5",
            "lead_body_height_in": "3.1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/look?saved=1"

    saved = Settings.load()
    assert saved.look.paper_name == "The Evening Ledger"
    assert saved.look.body_size_pt == 10.5
    assert saved.look.lead_body_height_in == 3.1
    assert saved.look.masthead_font == "UnifrakturCook"
    assert saved.look.ear_right.lines == ["One line", "Another line"]
    # A save that does not touch the Layout table leaves it alone.
    assert [s.key for s in saved.look.layout.sections] == [
        "agenda", "list:tasks", "hourly", "notes"
    ]
    # The other tabs' sections are untouched.
    assert saved.output.email.to == ["her@example.com"]
    assert saved.output.schedule.time == "05:45"
    assert [s.name for s in saved.sources.substacks] == ["oneuseful"]


def test_look_ignores_an_unknown_font_and_clamps_the_size(client):
    client.post("/look", auth=AUTH, data={"paper_name": "X", "body_font": "Comic Sans", "body_size_pt": "99"})
    look = Settings.load().look
    assert look.body_font == "PT Serif"
    assert look.body_size_pt == 11.0


# ------------------------------------------- the ears and the date line
def test_the_look_tab_offers_both_ears_and_the_date_controls(client):
    """One column per box, every kind in each, and where the date goes."""
    from render import fontlist

    settings = Settings()
    settings.look.ear_left = EarBox(kind="sun")
    settings.look.ear_right = EarBox(kind="countdown", countdown_date="2026-12-24",
                                     countdown_label="Christmas")
    settings.look.date_place = "above"
    settings.look.date_font = "Bodoni Moda"
    settings.look.date_size_pt = 11.5
    settings.save()

    body = client.get("/look", auth=AUTH).text
    assert "Left ear" in body and "Right ear" in body
    for side, chosen in (("left", "sun"), ("right", "countdown")):
        assert f'name="ear_{side}_kind"' in body, side
        for kind in EAR_KINDS:
            assert f'<option value="{kind}"' in body, kind
        assert f'<option value="{chosen}" selected>' in body, side
        for field in ("initials", "lines", "countdown_date", "countdown_label"):
            assert f'name="ear_{side}_{field}"' in body, (side, field)
    assert "2026-12-24" in body and "Christmas" in body

    # The date: where it goes, what face it is set in, and how big.
    assert 'name="date_place"' in body
    for place in DATE_PLACES:
        assert f'<option value="{place}"' in body, place
    assert '<option value="above" selected>' in body
    assert "On the rule under the masthead" in body
    for name in FONT_CHOICES_HEAD:
        assert f'name="date_font" value="{name}"' in body, name
        assert f"--face: {fontlist.stack(name)}" in body, name
    assert 'name="date_font" value="Bodoni Moda" checked' in body
    assert 'name="date_size_pt"' in body and 'value="11.5"' in body


def test_saving_the_ears_and_the_date_line(client):
    response = client.post("/look", auth=AUTH, data={
        "paper_name": "The Evening Gull",
        "ear_left_kind": "countdown",
        "ear_left_countdown_date": "2026-12-24",
        "ear_left_countdown_label": "Christmas",
        "ear_left_initials": "M. L.",
        "ear_left_lines": "A line\nAnother\n",
        "ear_right_kind": "none",
        "ear_right_initials": "",
        "ear_right_lines": "",
        "date_place": "below",
        "date_font": "Playfair Display",
        "date_size_pt": "12.5",
    }, follow_redirects=False)
    assert response.status_code == 303

    look = Settings.load().look
    assert look.ear_left.kind == "countdown"
    assert look.ear_left.countdown_date == "2026-12-24"
    assert look.ear_left.countdown_label == "Christmas"
    # The words of the other kinds are kept: she may come back to them.
    assert look.ear_left.initials == "M. L."
    assert look.ear_left.lines == ["A line", "Another"]
    assert look.ear_right.kind == "none"
    assert (look.date_place, look.date_font, look.date_size_pt) == \
        ("below", "Playfair Display", 12.5)


def test_a_countdown_date_that_is_not_a_date_is_blanked(client):
    client.post("/look", auth=AUTH, data={
        "paper_name": "X",
        "ear_left_kind": "countdown",
        "ear_left_countdown_date": "next Tuesday",
    })
    assert Settings.load().look.ear_left.countdown_date == ""

    client.post("/look", auth=AUTH, data={
        "paper_name": "X", "ear_left_kind": "countdown",
        "ear_left_countdown_date": "2026-02-30",
    })
    assert Settings.load().look.ear_left.countdown_date == ""


def test_an_unknown_ear_kind_or_date_place_is_ignored(client):
    settings = Settings()
    settings.look.ear_left = EarBox(kind="sun")
    settings.look.date_place = "above"
    settings.save()

    client.post("/look", auth=AUTH, data={
        "paper_name": "X",
        "ear_left_kind": "horoscope",
        "date_place": "sideways",
        "date_font": "Comic Sans",
        "date_size_pt": "99",
    })
    look = Settings.load().look
    assert look.ear_left.kind == "sun"          # what it was, not what was sent
    assert look.date_place == "above"
    assert look.date_font == "Old Standard"
    assert look.date_size_pt == 24.0            # clamped to the range the tab offers


# ------------------------------------------------------------ the fonts
def test_the_fonts_stylesheet_carries_every_face(client):
    """One table behind both: what /fonts.css declares is what the sheet
    is set from (render/fontlist.py)."""
    from render import fontlist

    response = client.get("/fonts.css", auth=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    faces = response.text
    assert faces.count("@font-face") == sum(len(r) for r in fontlist.FONT_FILES.values())
    for name in (*FONT_CHOICES_BODY, *FONT_CHOICES_HEAD, *FONT_CHOICES_MASTHEAD):
        assert f'font-family: "{name}"' in faces, name
    for family, rules in fontlist.FONT_FILES.items():
        for file, _, _ in rules:
            assert f'url("/fonts/{file}")' in faces


def test_the_fonts_stylesheet_needs_the_password(client):
    assert client.get("/fonts.css").status_code == 401


def test_a_bundled_font_is_served_behind_the_password(client):
    assert client.get("/fonts/Lora-var.ttf").status_code == 401
    response = client.get("/fonts/Lora-var.ttf", auth=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/ttf"
    assert response.content[:4] == b"\x00\x01\x00\x00"          # a TrueType file, not a 404 page


def test_a_font_that_is_not_bundled_is_a_404(client):
    assert client.get("/fonts/Nonesuch-var.ttf", auth=AUTH).status_code == 404


def test_the_look_tab_shows_a_card_for_every_face(client):
    """The picker: a radio per choice, set in that face, the saved one ticked."""
    from render import fontlist

    settings = Settings()
    settings.look.masthead_font = "Pirata One"
    settings.look.headline_font = "Bodoni Moda"
    settings.look.body_font = "Literata"
    settings.save()

    body = client.get("/look", auth=AUTH).text
    assert '<link rel="stylesheet" href="/fonts.css">' in body
    groups = [("masthead_font", FONT_CHOICES_MASTHEAD, "Pirata One"),
              ("headline_font", FONT_CHOICES_HEAD, "Bodoni Moda"),
              ("body_font", FONT_CHOICES_BODY, "Literata")]
    for field, choices, chosen in groups:
        for name in choices:
            assert f'name="{field}" value="{name}"' in body, (field, name)
            # the card is set in the face it names
            assert f"--face: {fontlist.stack(name)}" in body, name
        assert f'name="{field}" value="{chosen}" checked' in body, field
        # exactly one of the group is ticked
        ticked = [n for n in choices if f'name="{field}" value="{n}" checked' in body]
        assert ticked == [chosen]


def test_the_look_tab_saves_a_newly_offered_face(client):
    client.post("/look", auth=AUTH, data={
        "paper_name": "The Evening Gull",
        "masthead_font": "Grenze Gotisch",
        "headline_font": "Oswald",
        "body_font": "Merriweather",
    })
    look = Settings.load().look
    assert (look.masthead_font, look.headline_font, look.body_font) == \
        ("Grenze Gotisch", "Oswald", "Merriweather")


def test_the_layout_tab_saves_the_layout(client):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.sync_list_sections()
    settings.save()

    # The table submits one `sec_key` per row, with the order and page
    # boxes numbered by row.
    response = client.post(
        "/layout",
        auth=AUTH,
        follow_redirects=False,
        data={
            "sec_key": ["agenda", "list:tasks", "hourly", "notes", "list:groceries"],
            "sec_order_0": "2", "sec_place_0": "rail",
            "sec_order_1": "1", "sec_place_1": "rail",
            "sec_order_2": "4", "sec_place_2": "page2",
            "sec_order_3": "5", "sec_place_3": "off",
            "sec_order_4": "3", "sec_place_4": "page2",
            "rail_side": "left",
            "rail_width_in": "2.4",
            "front_stories": "2",
            "crossword_place": "top",
            "crossword_cell_in": "0.22",
            "crossword_max_pct": "40",
            "pictures": "on",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/layout?saved=1"

    layout = Settings.load().look.layout
    assert [(s.key, s.place) for s in layout.sections] == [
        ("list:tasks", "rail"),
        ("agenda", "rail"),
        ("list:groceries", "page2"),
        ("hourly", "page2"),
        ("notes", "off"),
    ]
    assert layout.rail_side == "left"
    assert layout.rail_width_in == 2.4
    assert layout.front_stories == 2
    assert layout.crossword_place == "top"
    assert layout.crossword_cell_in == 0.22
    assert layout.crossword_max_pct == 40
    assert layout.pictures is True

    # And the tab shows it back: the saved side is the selected one.
    body = client.get("/layout", auth=AUTH).text
    assert '<option value="left" selected>Left</option>' in body
    assert 'name="pictures" checked' in body


def test_the_picture_sheet_is_off_until_it_is_ticked(client):
    Settings().save()
    assert Settings.load().look.layout.pictures is False
    body = client.get("/layout", auth=AUTH).text
    assert "<h2>Pictures</h2>" in body
    assert 'name="pictures"' in body and 'name="pictures" checked' not in body

    # Saving the tab with the box unticked leaves it off; ticking it turns it on.
    client.post("/layout", auth=AUTH, data={"rail_side": "right"})
    assert Settings.load().look.layout.pictures is False
    client.post("/layout", auth=AUTH, data={"rail_side": "right", "pictures": "on"})
    assert Settings.load().look.layout.pictures is True
    client.post("/layout", auth=AUTH, data={"rail_side": "right"})
    assert Settings.load().look.layout.pictures is False


def test_the_rail_side_defaults_to_the_right_and_a_nonsense_value_is_ignored(client):
    Settings().save()
    assert Settings.load().look.layout.rail_side == "right"
    body = client.get("/layout", auth=AUTH).text
    assert 'name="rail_side"' in body
    assert '<option value="right" selected>Right</option>' in body

    client.post("/layout", auth=AUTH, data={"rail_side": "sideways"})
    assert Settings.load().look.layout.rail_side == "right"


def test_the_layout_numbers_are_clamped_and_unknown_sections_ignored(client):
    client.post(
        "/layout",
        auth=AUTH,
        data={
            "sec_key": ["agenda", "list:nosuchlist", "hourly", "notes"],
            "sec_order_0": "1", "sec_place_0": "sideways",     # not a place
            "sec_order_1": "2", "sec_place_1": "rail",
            "sec_order_2": "3", "sec_place_2": "off",
            "sec_order_3": "4", "sec_place_3": "rail",
            "rail_width_in": "9",
            "front_stories": "12",
            "crossword_cell_in": "0.01",
            "crossword_max_pct": "1",
        },
    )
    layout = Settings.load().look.layout
    # The section naming a list that does not exist is dropped, and the
    # list that does exist gets its section back at the end of the rail.
    assert [(s.key, s.place) for s in layout.sections] == [
        ("agenda", "rail"), ("hourly", "off"), ("notes", "rail"), ("list:tasks", "rail")
    ]
    assert layout.rail_width_in == 2.8
    assert layout.front_stories == 4
    assert layout.crossword_cell_in == 0.14
    assert layout.crossword_max_pct == 25


def test_the_layout_tab_lists_every_section_with_its_list_name(client):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Weekend shopping"))
    settings.sync_list_sections()
    settings.save()

    body = client.get("/layout", auth=AUTH).text
    assert "<h2>Sections</h2>" in body
    assert 'value="list:weekend-shopping"' in body
    assert "Weekend shopping" in body
    # The reader's words for the fixed sections, not the paper's.
    assert "Today&#39;s agenda" in body or "Today's agenda" in body
    assert "Hourly forecast" in body and "Notes" in body
    assert 'name="rail_width_in"' in body and 'name="front_stories"' in body
    assert 'name="crossword_cell_in"' in body and 'name="crossword_max_pct"' in body
    assert 'name="pictures"' in body


def test_the_layout_table_says_what_each_section_prints_as(client):
    """The paper keeps its newspaper voice; the tab says so in a muted line."""
    Settings().save()
    body = client.get("/layout", auth=AUTH).text
    assert "prints as: Hour by hour" in body
    assert "prints as: Today" in body
    # Notes is the same word on paper, so there is nothing to explain.
    assert "prints as: Notes" not in body


def test_the_look_tab_no_longer_carries_the_layout(client):
    """Layout is its own tab: nothing on Look may save a section's place."""
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.sync_list_sections()
    settings.save()

    body = client.get("/look", auth=AUTH).text
    for field in ("sec_key", "sec_place_0", "sec_order_0", "rail_side",
                  "rail_width_in", "front_stories", "crossword_place",
                  "crossword_cell_in", "crossword_max_pct"):
        assert f'name="{field}"' not in body, field
    assert "<h2>Sections</h2>" not in body
    assert 'action="/layout"' not in body

    # A Look save that carries stale layout fields still cannot move a section.
    client.post("/look", auth=AUTH, data={
        "paper_name": "The Evening Ledger",
        "sec_key": ["agenda"], "sec_order_0": "1", "sec_place_0": "off",
        "rail_side": "left", "rail_width_in": "2.8",
    })
    layout = Settings.load().look.layout
    assert [(s.key, s.place) for s in layout.sections] == [
        ("agenda", "rail"), ("list:tasks", "rail"), ("hourly", "rail"),
        ("notes", "rail"), ("list:groceries", "rail"),
    ]
    assert layout.rail_side == "right" and layout.rail_width_in == 1.9


def test_the_tabs_are_in_order_on_every_page(client):
    """Look, Layout, Sources, Output, Preview -- the order of the work --
    and then Papers, which is about the container rather than one paper."""
    import re as _re

    for tab in ("look", "layout", "sources", "output", "preview", "papers"):
        body = client.get(f"/{tab}", auth=AUTH).text
        nav = body.split('<nav class="tabs">')[1].split("</nav>")[0]
        assert _re.findall(r'>([A-Za-z]+)</a>', nav) == [
            "Look", "Layout", "Sources", "Output", "Preview", "Papers"
        ], tab
        assert f'href="/{tab}" class="active"' in nav


def test_the_status_strip_labels_every_item_plainly(client):
    """No internal words in the strip: each line says what it is."""
    import state

    state.update_state(last_run="2026-09-17T06:00:00-07:00",
                       last_success="2026-09-17T06:00:00-07:00", last_pages=2)
    body = client.get("/output", auth=AUTH).text
    strip = body.split('<section class="status"', 1)[1].split("</section>", 1)[0]
    for label in ("Last run", "Result", "Last successful print", "Latest paper",
                  "Next scheduled run", "Log"):
        assert f"<dt>{label}</dt>" in strip, label
    for gone in ("Last success<", "Last paper<", "Scheduler<"):
        assert gone not in strip, gone
    assert "ok</span>, 2 pages" in strip
    assert "not scheduled" in strip               # PAPER_NO_SCHEDULER is set here


def test_the_status_strip_says_why_a_run_failed(client):
    import state

    state.update_state(last_run="2026-09-17T06:00:00-07:00",
                       last_error="no route to the printer")
    strip = client.get("/output", auth=AUTH).text.split(
        '<section class="status"', 1)[1].split("</section>", 1)[0]
    assert "failed</span>: no route to the printer" in strip


def test_saving_output_persists_and_reschedules(client, monkeypatch):
    calls: list[Settings] = []
    monkeypatch.setattr("app.main.scheduler.reschedule", lambda s: calls.append(s))
    Settings().save()

    response = client.post(
        "/output",
        auth=AUTH,
        data={
            "print_enabled": "on",
            "printer_host": "192.168.1.40",
            "printer_name": "Brother HL-L2460DW",
            "duplex": "on",
            "email_enabled": "on",
            "email_to": "her@example.com, him@example.com",
            "email_subject": "The Evening Ledger, {date}",
            "schedule_time": "07:15",
            "day_0": "on",
            "day_2": "on",
            "notify": "email",
            "notify_email": "her@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    out = Settings.load().output
    assert out.schedule.time == "07:15"
    assert out.schedule.days == [0, 2]
    assert out.print.enabled and out.print.printer_host == "192.168.1.40"
    assert out.email.to == ["her@example.com", "him@example.com"]
    assert out.notify == "email"
    assert len(calls) == 1


def test_saving_sources_keeps_masked_urls_and_adds_rows(client):
    settings = Settings()
    settings.sources.calendars = [CalendarSource(url="https://example.com/secret/basic.ics", name="Home")]
    settings.save()

    client.post(
        "/sources",
        auth=AUTH,
        data={
            "cal_index": "0",
            "cal_url_0": "",                      # masked on the page: keep what is stored
            "cal_name_0": "Home",
            "cal_new": "n0",
            "cal_new_url_n0": "https://example.com/other/basic.ics",
            "cal_new_name_n0": "",
            "sub_new": "n0",
            "sub_new_name_n0": "oneuseful",
            "sub_new_paid_n0": "on",
            "sub_new_order_n0": "1",
            "lat": "37.87",
            "lon": "-122.27",
            "article_max_age_days": "14",
            "list_index": "0",
            "list_name_0": "To do",
            "list_style_0": "numbered",
            "list_max_age_0": "12",
            "tasks_post_url": "http://paper.lan:9000/",
        },
    )
    sources = Settings.load().sources
    assert [c.url for c in sources.calendars] == [
        "https://example.com/secret/basic.ics",
        "https://example.com/other/basic.ics",
    ]
    assert [(s.name, s.paid) for s in sources.substacks] == [("oneuseful", True)]
    assert sources.weather.lat == 37.87
    assert sources.article_max_age_days == 14
    assert [(li.slug, li.style, li.max_age_hours) for li in sources.lists] == [
        ("tasks", "numbered", 12)
    ]
    assert sources.tasks_post_url == "http://paper.lan:9000"   # the slash is dropped


def test_adding_a_list_gives_it_a_slug_a_section_and_a_box(client):
    response = client.post(
        "/sources",
        auth=AUTH,
        data={
            "list_index": "0", "list_name_0": "To do",
            "list_style_0": "checkbox", "list_max_age_0": "24",
            "list_new": ["n0", "n1"],
            "list_new_name_n0": "Weekend shopping!",
            "list_new_style_n0": "numbered",
            "list_new_max_age_n0": "72",
            "list_new_name_n1": "",                       # left blank: ignored
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    settings = Settings.load()
    assert [(li.name, li.slug, li.style, li.max_age_hours) for li in settings.sources.lists] == [
        ("To do", "tasks", "checkbox", 24),
        ("Weekend shopping!", "weekend-shopping", "numbered", 72),
    ]
    # The new list takes its place in the rail without a trip to the Look tab.
    assert [(s.key, s.place) for s in settings.look.layout.sections] == [
        ("agenda", "rail"), ("list:tasks", "rail"), ("hourly", "rail"),
        ("notes", "rail"), ("list:weekend-shopping", "rail"),
    ]

    body = client.get("/sources", auth=AUTH, headers={"Host": "unraid.local:8080"}).text
    assert 'href="http://unraid.local:8080/lists/weekend-shopping"' in body
    assert 'data-copy="http://unraid.local:8080/lists/weekend-shopping"' in body
    assert 'data-copy="Bearer weekend-shopping"' in body
    assert "Copy URL" in body and "Copy example body" in body
    # The address name is shown, read-only, beside the list's name.
    assert 'name="list_slug_1" value="weekend-shopping" readonly' in body
    assert "Address name" in body and ">Slug<" not in body
    assert "How the phone sends this list: Weekend shopping!" in body

    # And it is a working address straight away.
    assert client.post(
        "/lists/weekend-shopping",
        json={"items": ["Cheese"]},
        headers={"Authorization": "Bearer weekend-shopping"},
    ).json() == {"ok": True, "list": "weekend-shopping", "count": 1}


def test_a_list_keeps_its_slug_when_it_is_renamed(client):
    client.post(
        "/sources",
        auth=AUTH,
        data={"list_index": "0", "list_name_0": "To do",
              "list_new": "n0", "list_new_name_n0": "Groceries"},
    )
    client.post(
        "/sources",
        auth=AUTH,
        data={
            "list_index": ["0", "1"],
            "list_name_0": "To do", "list_name_1": "Shopping",
        },
    )
    settings = Settings.load()
    assert [(li.name, li.slug) for li in settings.sources.lists] == [
        ("To do", "tasks"), ("Shopping", "groceries")
    ]


def test_two_lists_with_the_same_name_get_different_slugs(client):
    client.post(
        "/sources",
        auth=AUTH,
        data={
            "list_index": "0", "list_name_0": "To do",
            "list_new": ["n0", "n1"],
            "list_new_name_n0": "Packing", "list_new_name_n1": "Packing",
        },
    )
    assert Settings.load().sources.slugs() == ["tasks", "packing", "packing-2"]


def test_removing_a_list_drops_its_section(client):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.sync_list_sections()
    settings.save()
    assert "list:groceries" in [s.key for s in Settings.load().look.layout.sections]

    client.post(
        "/sources",
        auth=AUTH,
        data={
            "list_index": ["0", "1"],
            "list_name_0": "To do", "list_name_1": "Groceries",
            "list_remove_1": "on",
        },
    )
    settings = Settings.load()
    assert settings.sources.slugs() == ["tasks"]
    assert [s.key for s in settings.look.layout.sections] == [
        "agenda", "list:tasks", "hourly", "notes"
    ]
    # Its address is gone with it.
    assert client.post(
        "/lists/groceries", json={"items": ["Milk"]}, headers={"Authorization": "Bearer tok"}
    ).status_code == 404


def test_several_new_rows_save_at_once(client):
    """"Add another" clones rows client-side; the server reads every key."""
    client.post(
        "/sources",
        auth=AUTH,
        data={
            "cal_new": ["n0", "n1", "n2", "bogus"],
            "cal_new_url_n0": "https://example.com/a/basic.ics", "cal_new_name_n0": "A",
            "cal_new_url_n1": "",                                    # left blank: ignored
            "cal_new_url_n2": "https://example.com/c/basic.ics", "cal_new_name_n2": "C",
            "sub_new": ["n0", "n1", "bogus"],
            "sub_new_name_n0": "second", "sub_new_order_n0": "2",
            "sub_new_name_n1": "first", "sub_new_order_n1": "1", "sub_new_paid_n1": "on",
            "sub_new_name_bogus": "ignored",
        },
    )
    sources = Settings.load().sources
    assert [(c.url, c.name) for c in sources.calendars] == [
        ("https://example.com/a/basic.ics", "A"),
        ("https://example.com/c/basic.ics", "C"),
    ]
    assert [(s.name, s.paid) for s in sources.substacks] == [("first", True), ("second", False)]


def test_sources_page_shows_masked_url_and_shortcut_instructions(client):
    settings = Settings()
    settings.sources.calendars = [CalendarSource(url="https://example.com/abc123456", name="Home")]
    settings.save()

    body = client.get("/sources", auth=AUTH).text
    assert "abc123456" not in body
    assert "••••123456" in body
    assert "/lists/tasks" in body and "Bearer" in body
    assert "TASKS_TOKEN" in body


# ------------------------------------------------------------ the list boxes
def test_the_window_field_is_on_the_page(client):
    settings = Settings()
    settings.sources.article_max_age_days = 21
    settings.save()

    body = client.get("/sources", auth=AUTH).text
    assert 'name="article_max_age_days"' in body
    assert 'value="21"' in body
    assert "Skip posts older than" in body


def test_an_out_of_range_window_is_clamped(client):
    client.post("/sources", auth=AUTH, data={"article_max_age_days": "900"})
    assert Settings.load().sources.article_max_age_days == 60


def test_the_tasks_box_uses_the_host_the_browser_used(client):
    body = client.get("/sources", auth=AUTH, headers={"Host": "unraid.local:8080"}).text
    assert 'href="http://unraid.local:8080/lists/tasks"' in body


def test_the_tasks_box_falls_back_to_the_lan_address(client, monkeypatch):
    """A Host of 127.0.0.1 tells the phone nothing, so it is never shown."""
    from app import main as app_main

    monkeypatch.setattr(app_main, "lan_ip", lambda: "192.168.1.50")
    body = client.get("/sources", auth=AUTH, headers={"Host": "127.0.0.1:8080"}).text
    assert 'href="http://192.168.1.50:8080/lists/tasks"' in body
    assert "127.0.0.1:8080" not in body


def test_the_tasks_box_falls_back_again_when_there_is_no_lan_address(client, monkeypatch):
    from app import main as app_main

    monkeypatch.setattr(app_main, "lan_ip", lambda: "")
    body = client.get("/sources", auth=AUTH, headers={"Host": "localhost:8080"}).text
    assert 'href="http://unraid.local:8080/lists/tasks"' in body


def test_the_tasks_box_shows_the_override_when_it_is_set(client, monkeypatch):
    from app import main as app_main

    monkeypatch.setattr(app_main, "lan_ip", lambda: "192.168.1.50")
    settings = Settings()
    settings.sources.tasks_post_url = "http://paper.example.com"
    settings.save()

    body = client.get("/sources", auth=AUTH, headers={"Host": "127.0.0.1:8080"}).text
    assert 'href="http://paper.example.com/lists/tasks"' in body
    assert 'href="http://192.168.1.50:8080/lists/tasks"' not in body   # the override wins; the derived one stays as the placeholder
    # the worked-out address is still offered, as the field's placeholder
    assert 'placeholder="http://192.168.1.50:8080"' in body


def test_the_tasks_box_names_the_token_without_revealing_it(client, monkeypatch):
    monkeypatch.setenv("TASKS_TOKEN", "tok3n-of-my-esteem")
    body = client.get("/sources", auth=AUTH).text
    assert "Bearer tok3…" in body
    # The token is never printed as text: only the copy button carries it,
    # and only because this client is behind WEB_PASSWORD.
    assert body.count("tok3n-of-my-esteem") == 1
    assert 'data-copy="Bearer tok3n-of-my-esteem"' in body


def test_the_tasks_box_says_when_no_token_is_set(client, monkeypatch):
    monkeypatch.delenv("TASKS_TOKEN", raising=False)
    body = client.get("/sources", auth=AUTH).text
    assert "not set in the container" in body
    assert "Bearer tok" not in body


# --------------------------------------------------------- check endpoints
def test_calendar_check_reports_errors(client, monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr("gather.calendar.check", boom)
    body = client.post("/sources/calendar/check", auth=AUTH, json={"url": "https://x/basic.ics"}).json()
    assert body["ok"] is False
    assert "404 Not Found" in body["error"]


def test_calendar_check_ok(client, monkeypatch):
    monkeypatch.setattr("gather.calendar.check", lambda url, **k: {"name": "Home", "events_today": 3})
    body = client.post("/sources/calendar/check", auth=AUTH, json={"url": "https://x/basic.ics"}).json()
    assert body == {"ok": True, "result": {"name": "Home", "events_today": 3}}


def test_weather_check_reports_errors(client, monkeypatch):
    def boom(lat, lon, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr("gather.weather.check", boom)
    body = client.post("/sources/weather/check", auth=AUTH, json={"lat": 37.8, "lon": -122.3}).json()
    assert body["ok"] is False and "no route to host" in body["error"]


def test_weather_check_ok(client, monkeypatch):
    monkeypatch.setattr(
        "gather.weather.check",
        lambda lat, lon, **k: {"summary": "Sunny all day", "high": "72", "low": "54"},
    )
    body = client.post("/sources/weather/check", auth=AUTH, json={"lat": "37.8", "lon": "-122.3"}).json()
    assert body["ok"] is True and body["result"]["summary"] == "Sunny all day"


def test_substack_check_reports_errors(client, monkeypatch):
    def boom(name):
        raise ValueError("no such feed")

    monkeypatch.setattr("gather.substack.check", boom)
    body = client.post("/sources/substack/check", auth=AUTH, json={"name": "nope"}).json()
    assert body["ok"] is False and "no such feed" in body["error"]


QUEUE_PREVIEW = {
    "window_days": 7,
    "queued": [{"publication": "The Slow Kitchen", "title": "The bread you meant to make",
                "url": "https://slowkitchen.substack.com/p/bread", "published": "2025-09-15",
                "age_days": 1.0, "position": 1, "status": "queued"}],
    "printed": [],
    "skipped": [],
    "errors": [],
}


def test_substack_queue_returns_the_preview(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "gather.substack.queue_preview",
        lambda settings: seen.append(settings) or QUEUE_PREVIEW,
    )
    body = client.post("/sources/substack/queue", auth=AUTH).json()
    assert body == {"ok": True, "result": QUEUE_PREVIEW}
    assert len(seen) == 1 and hasattr(seen[0].sources, "substacks")


def test_substack_queue_reports_errors(client, monkeypatch):
    def boom(settings):
        raise ValueError("no such feed")

    monkeypatch.setattr("gather.substack.queue_preview", boom)
    body = client.post("/sources/substack/queue", auth=AUTH).json()
    assert body["ok"] is False and "no such feed" in body["error"]


def test_sources_page_has_the_show_queue_button(client):
    body = client.get("/sources", auth=AUTH).text
    assert 'id="show-queue"' in body
    assert "Show what&rsquo;s waiting" in body
    assert "is marked as printed and nothing is sent anywhere" in body
    assert "/sources/substack/queue" in body


def test_crossword_check_reports_errors(client, monkeypatch):
    def boom(today=None):
        raise RuntimeError("NYT-S cookie rejected (got a login page)")

    monkeypatch.setattr("gather.crossword.check", boom)
    body = client.post("/sources/crossword/check", auth=AUTH).json()
    assert body["ok"] is False
    assert "cookie rejected (got a login page)" in body["error"]


def test_crossword_check_ok(client, monkeypatch):
    monkeypatch.setattr(
        "gather.crossword.check",
        lambda today=None: {"ok": True, "date": "2026-09-17", "title": "Cross Purposes",
                            "author": "Robyn Weintraub", "editor": "Will Shortz",
                            "size": "15x15", "clues": 72, "error": ""},
    )
    body = client.post("/sources/crossword/check", auth=AUTH).json()
    assert body["ok"] is True
    assert body["result"]["size"] == "15x15" and body["result"]["clues"] == 72


def test_sources_page_shows_the_crossword_section(client):
    body = client.get("/sources", auth=AUTH).text
    assert "<h2>Crossword</h2>" in body
    assert "NYT_S" in body                     # named in the note and in the table
    assert "crossword_day_0" in body
    # The container's settings are named as such, never as "variables".
    assert "<h2>Set on the container (Unraid)</h2>" in body
    assert "Credentials live in the container's settings, not on this page." in body
    assert "Container variables" not in body


def test_saving_the_crossword_persists_the_days(client):
    client.post(
        "/sources",
        auth=AUTH,
        data={
            "lat": "37.87", "lon": "-122.27", "tasks_max_age_hours": "24",
            "crossword_enabled": "on",
            "crossword_day_0": "on", "crossword_day_2": "on", "crossword_day_6": "on",
        },
    )
    crossword = Settings.load().sources.crossword
    assert crossword.enabled is True
    assert crossword.days == [0, 2, 6]
    assert crossword.provider == "nyt"

    # Unticking the box turns it off again and leaves the days alone.
    client.post("/sources", auth=AUTH,
                data={"lat": "37.87", "lon": "-122.27", "tasks_max_age_hours": "24",
                      "crossword_day_0": "on"})
    assert Settings.load().sources.crossword.enabled is False
    assert Settings.load().sources.crossword.days == [0]


def test_status_strip_shows_the_last_crossword_check(client, monkeypatch):
    import state

    state.update_state(crossword_check={
        "ok": True, "when": "2026-09-17T05:58:00-07:00",
        "summary": "Cross Purposes by Robyn Weintraub, 2026-09-17 (15x15, 72 clues)",
    })

    # Switched off on the Sources tab: the strip says nothing about it.
    assert "<dt>Crossword</dt>" not in client.get("/sources", auth=AUTH).text

    settings = Settings()
    settings.sources.crossword.enabled = True
    settings.save()

    text = client.get("/sources", auth=AUTH).text
    assert "<dt>Crossword</dt>" in text
    assert "Cross Purposes by Robyn Weintraub" in text
    assert "(checked 05:58)" in text


def _diagnosis(ok: bool):
    """A canned Diagnosis, so no test here touches a printer."""
    from deliver.printer import Diagnosis, PrinterInfo, Step

    if ok:
        return Diagnosis(
            ok=True,
            steps=[
                Step("resolve", True, "192.168.1.40:631"),
                Step("connect", True, "192.168.1.40:631 answered"),
                Step("ipp", True, "Brother HL-L2460DW answered Get-Printer-Attributes"),
                Step("format", True, "Will send PDF"),
                Step("state", True, "Idle."),
            ],
            info=PrinterInfo("Brother HL-L2460DW", "HL-L2460DW", "idle", True, []),
            summary="Brother HL-L2460DW: idle, accepts PDF",
        )
    step = Step(
        "connect",
        False,
        "No answer from 192.168.1.40:631 (timed out)",
        "Is the printer on and on the same network as the Unraid box?",
    )
    return Diagnosis(ok=False, steps=[Step("resolve", True, "192.168.1.40:631"), step],
                     info=None, summary=f"connect: {step.detail}")


def test_printer_endpoints_never_crash(client, monkeypatch):
    monkeypatch.setattr("deliver.discover", lambda *a, **k: [])
    assert client.post("/output/printer/discover", auth=AUTH).json() == {"ok": True, "result": []}

    monkeypatch.setattr("deliver.diagnose", lambda host, **k: _diagnosis(False))
    body = client.post("/output/printer/test", auth=AUTH, json={"host": "192.168.1.40"}).json()
    assert body["ok"] is True  # a failed printer is a result, not a crash
    assert body["result"]["ok"] is False
    assert "timed out" in body["result"]["summary"]

    assert client.post("/output/printer/test", auth=AUTH, json={}).json() == {
        "ok": False, "error": "No printer host set"
    }


def test_printer_test_returns_the_whole_checklist(client, monkeypatch):
    monkeypatch.setattr("deliver.diagnose", lambda host, **k: _diagnosis(True))

    body = client.post("/output/printer/test", auth=AUTH, json={"host": "192.168.1.40"}).json()

    assert body["ok"] is True
    result = body["result"]
    assert result["ok"] is True
    assert result["summary"] == "Brother HL-L2460DW: idle, accepts PDF"
    assert [s["name"] for s in result["steps"]] == [
        "resolve", "connect", "ipp", "format", "state"
    ]
    assert set(result["steps"][0]) == {"name", "ok", "detail", "hint"}
    assert result["info"]["accepts_pdf"] is True

    # The check is remembered for the status strip.
    import state

    assert state.load_state()["printer_check"]["summary"] == result["summary"]


def test_status_strip_shows_the_last_printer_check(client, monkeypatch):
    import state

    settings = Settings()
    settings.output.print.printer_host = "192.168.1.40"
    settings.save()

    # Nothing recorded yet: the strip says nothing about the printer.
    assert "<dt>Printer</dt>" not in client.get("/output", auth=AUTH).text

    monkeypatch.setattr("deliver.diagnose", lambda host, **k: _diagnosis(True))
    client.post("/output/printer/test", auth=AUTH, json={"host": "192.168.1.40"})

    text = client.get("/output", auth=AUTH).text
    assert "<dt>Printer</dt>" in text
    assert "Brother HL-L2460DW: idle, accepts PDF" in text
    assert "checked" in text

    # No printer configured at all: the line goes away again.
    blank = Settings()
    blank.save()
    assert "<dt>Printer</dt>" not in client.get("/output", auth=AUTH).text
    assert state.load_state()["printer_check"]["ok"] is True


def test_test_page_refuses_when_the_printer_cannot_be_reached(client, monkeypatch):
    printed = []

    monkeypatch.setattr("deliver.diagnose", lambda host, **k: _diagnosis(False))
    monkeypatch.setattr("deliver.print_pdf", lambda *a, **k: printed.append(a))

    body = client.post(
        "/output/printer/test-page", auth=AUTH, json={"host": "192.168.1.40"}
    ).json()

    assert body["ok"] is False
    assert "No answer from 192.168.1.40:631" in body["error"]
    assert "same network" in body["error"]  # the hint travels with it
    assert printed == [], "nothing was rendered or sent to a printer that is not there"


def test_email_test_reports_errors(client, monkeypatch):
    def boom(to, smtp, **kwargs):
        raise RuntimeError("SMTP is not configured")

    monkeypatch.setattr("deliver.send_test", boom)
    body = client.post("/output/email/test", auth=AUTH, json={"to": "her@example.com"}).json()
    assert body["ok"] is False and "SMTP is not configured" in body["error"]


# --------------------------------------------------- POST /lists/<slug>
def test_a_list_takes_its_own_slug_as_the_token(client, data_dir):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.save()

    response = client.post(
        "/lists/groceries",
        json={"items": ["Milk", "Bread"]},
        headers={"Authorization": "Bearer groceries"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "list": "groceries", "count": 2}
    stored = json.loads((data_dir / "lists" / "groceries.json").read_text())
    assert stored["items"] == ["Milk", "Bread"]


def test_a_list_also_takes_the_container_token(client, data_dir):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.save()

    response = client.post(
        "/lists/groceries", json={"items": ["Milk"]}, headers={"Authorization": "Bearer tok"}
    )
    assert response.json() == {"ok": True, "list": "groceries", "count": 1}


def test_a_list_refuses_a_wrong_token(client):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.save()

    # Neither the container's token nor this list's own slug.
    assert client.post(
        "/lists/groceries", json={"items": ["Milk"]}, headers={"Authorization": "Bearer tasks"}
    ).status_code == 401
    response = client.post(
        "/lists/groceries", json={"items": ["Milk"]}, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401 and response.json()["detail"].startswith("bad token")

    # With TASKS_TOKEN set, no header at all is refused too.
    assert client.post("/lists/groceries", json={"items": ["Milk"]}).status_code == 401


def test_without_a_container_token_a_bare_post_is_accepted(client, monkeypatch):
    monkeypatch.delenv("TASKS_TOKEN")
    assert client.post("/lists/tasks", json={"items": ["a"]}).json() == {
        "ok": True, "list": "tasks", "count": 1
    }
    # The slug still works as the token, and anything else is still refused.
    assert client.post(
        "/lists/tasks", json={"items": ["a"]}, headers={"Authorization": "Bearer tasks"}
    ).status_code == 200
    assert client.post(
        "/lists/tasks", json={"items": ["a"]}, headers={"Authorization": "Bearer nope"}
    ).status_code == 401


def test_an_unknown_slug_names_the_lists_there_are(client):
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.save()

    response = client.post(
        "/lists/packing", json={"items": ["Socks"]}, headers={"Authorization": "Bearer packing"}
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "packing" in detail and "tasks, groceries" in detail

    # A slug that is not even a slug is the same answer, not a crash.
    assert client.post("/lists/Not%20A%20Slug", json={"items": []}).status_code == 404


def test_the_tasks_alias_still_works(client, data_dir):
    response = client.post(
        "/tasks",
        json={"tasks": ["Call the school", "Buy stamps"]},
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "list": "tasks", "count": 2}
    stored = json.loads((data_dir / "lists" / "tasks.json").read_text())
    assert stored["items"] == ["Call the school", "Buy stamps"]

    from gather import lists as lists_gather

    assert lists_gather.status("tasks")["count"] == 2


def test_the_alias_takes_the_slug_as_its_token_too(client):
    assert client.post(
        "/tasks", json={"items": ["a"]}, headers={"Authorization": "Bearer tasks"}
    ).json() == {"ok": True, "list": "tasks", "count": 1}


def test_a_list_accepts_a_plain_text_body(client):
    from gather import lists as lists_gather

    response = client.post(
        "/lists/tasks",
        content="Call the school\nBuy stamps\nWater the fig\n",
        headers={"Authorization": "Bearer tasks", "Content-Type": "text/plain"},
    )
    assert response.json() == {"ok": True, "list": "tasks", "count": 3}
    assert lists_gather.status("tasks")["count"] == 3


def test_the_endpoint_explains_angle_brackets_and_reads_any_content_type(client, monkeypatch):
    monkeypatch.setenv("TASKS_TOKEN", "test")
    r = client.post("/lists/tasks", headers={"Authorization": "Bearer <test>"}, content="x")
    assert r.status_code == 401 and "angle brackets" in r.json()["detail"]

    r = client.post("/tasks", headers={"Authorization": "Basic test"}, content="x")
    assert r.status_code == 401 and "must start with Bearer" in r.json()["detail"]

    r = client.post(
        "/lists/tasks",
        headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        content="Water the fig tree\nCall the vet",
    )
    assert r.status_code == 200 and r.json()["count"] == 2

    r = client.post(
        "/lists/tasks",
        headers={"Authorization": "Bearer test", "Content-Type": "text/plain"},
        content='{"items": ["one", "two", "three"]}',
    )
    assert r.status_code == 200 and r.json()["count"] == 3

    r = client.post(
        "/lists/tasks",
        headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        content='{"items": [broken',
    )
    assert r.status_code == 400


# ------------------------------------------------------------ archive, log
def test_archive_rejects_odd_names(client, data_dir):
    archive = data_dir / "archive"
    archive.mkdir()
    (archive / "2026-09-16.pdf").write_bytes(b"%PDF-1.4 fake")
    (archive / "secret.txt").write_text("nope")
    (archive / "2026-09-18-2.pdf").write_bytes(b"%PDF-1.4 the second of the day")

    assert client.get("/archive/2026-09-16.pdf", auth=AUTH).status_code == 200
    assert client.get("/archive/2026-09-18-2.pdf", auth=AUTH).status_code == 200
    assert client.get("/archive/2026-09-18-.pdf", auth=AUTH).status_code == 404
    assert client.get("/archive/secret.txt", auth=AUTH).status_code == 404
    assert client.get("/archive/2026-09-16.pdf.bak", auth=AUTH).status_code == 404
    assert client.get("/archive/../x", auth=AUTH).status_code == 404
    assert client.get("/archive/%2e%2e%2fstate.json", auth=AUTH).status_code == 404


def test_log_shows_the_tail(client, data_dir):
    logs = data_dir / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("\n".join(f"line {i}" for i in range(300)))
    body = client.get("/log", auth=AUTH).text
    assert "line 299" in body
    assert "line 99" not in body


def _run_logs(data_dir: Path, *names: str) -> Path:
    runs = data_dir / "logs" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for name in names:
        (runs / name).write_text(f"everything {name} wrote down\n")
    return runs


def test_the_log_page_offers_enhanced_logging(client, data_dir):
    body = client.get("/log", auth=AUTH).text
    assert "Enhanced logging" in body
    assert 'name="enhanced"' in body
    assert "The last 7 runs" in body
    assert "no run has a file of its own" in body       # it is off


def test_turning_enhanced_logging_on_and_off(client, data_dir):
    response = client.post("/log", auth=AUTH, data={"enhanced": "on"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert Settings.load().logs.enhanced is True
    assert "checked" in client.get("/log", auth=AUTH).text

    client.post("/log", auth=AUTH, data={"other": "x"}, follow_redirects=False)
    assert Settings.load().logs.enhanced is False


def test_the_log_page_lists_the_runs_newest_first(client, data_dir):
    _run_logs(data_dir, "2026-09-18-060000.log", "2026-09-19-061500.log",
              "2026-09-20-060012.log")
    body = client.get("/log", auth=AUTH).text
    assert "2026-09-19 06:15:00" in body                # the run, in words
    assert (body.index("2026-09-20-060012.log")
            < body.index("2026-09-19-061500.log")
            < body.index("2026-09-18-060000.log"))
    assert body.count("/log/runs/") == 3


def test_a_run_log_is_a_download(client, data_dir):
    _run_logs(data_dir, "2026-09-20-060000.log")
    response = client.get("/log/runs/2026-09-20-060000.log", auth=AUTH)
    assert response.status_code == 200
    assert "everything 2026-09-20-060000.log wrote down" in response.text
    assert "attachment" in response.headers["content-disposition"]


def test_the_run_log_route_rejects_odd_names(client, data_dir):
    _run_logs(data_dir, "2026-09-20-060000.log")
    (data_dir / "logs" / "run.log").write_text("the everyday log")
    for name in ("run.log", "2026-09-20-060000.log.bak", "2026-09-20.log",
                 "../run.log", "%2e%2e%2frun.log", "2026-09-21-060000.log"):
        assert client.get(f"/log/runs/{name}", auth=AUTH).status_code == 404, name


def test_the_log_page_needs_the_password(client, data_dir):
    _run_logs(data_dir, "2026-09-20-060000.log")
    assert client.get("/log").status_code == 401
    assert client.get("/log/runs/2026-09-20-060000.log").status_code == 401


# ------------------------------------------------------------------ preview
def test_preview_renders_the_sample_issue(client, data_dir):
    response = client.post("/preview?source=sample", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].split("job=")[1]

    job = wait_for(job_id)
    assert job.status == "done", job.error
    assert job.result["pages"] == 2
    assert len(job.result["pngs"]) == 2

    status = client.get(f"/jobs/{job_id}", auth=AUTH).json()
    assert status["status"] == "done"

    for url in job.result["pngs"]:
        png = client.get(url, auth=AUTH)
        assert png.status_code == 200
        assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(job.result["html"], auth=AUTH).status_code == 200

    page = client.get(f"/preview?job={job_id}", auth=AUTH).text
    assert job_id in page


def test_preview_files_are_name_checked(client):
    assert client.get("/preview/zzz/page-1.png", auth=AUTH).status_code == 404
    assert client.get("/preview/abc123/paper.pdf", auth=AUTH).status_code == 404


def test_jobs_endpoint_404s_for_unknown_jobs(client):
    assert client.get("/jobs/abc123", auth=AUTH).status_code == 404


def test_preview_keeps_only_three_job_directories(client):
    root = jobs.preview_root()
    for i in range(5):
        d = root / f"{i:012x}"
        d.mkdir(parents=True)
        (d / "paper.html").write_text("x")
    jobs.prune_previews()
    assert len(list(root.iterdir())) == 3


def test_run_now_starts_a_job(client, monkeypatch):
    import run as run_module

    calls: list[bool] = []

    def fake_run(settings, *, dry_run=False, **kwargs):
        calls.append(dry_run)
        return run_module.RunResult(date="2026-09-16", pages=2, ok=True, delivery={"print": None})

    monkeypatch.setattr(run_module, "run", fake_run)
    body = client.post("/run?dry=1", auth=AUTH).json()
    assert body["ok"] is True

    job = wait_for(body["job"], timeout=20)
    assert job.status == "done", job.error
    assert job.result["pages"] == 2
    assert job.result["delivery"] == {"print": "ok"}
    assert calls == [True]


# ---------------------------------------------------------------- scheduler
def test_build_trigger_fires_at_the_configured_time():
    trigger = scheduler.build_trigger(Schedule(time="06:00", days=[0, 1, 2, 3, 4, 5, 6]))
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 16, 12, 0, tzinfo=tz)
    when = trigger.get_next_fire_time(None, now)
    assert (when.hour, when.minute) == (6, 0)
    assert when.date() == datetime(2026, 9, 17, tzinfo=tz).date()


def test_build_trigger_weekends_only():
    trigger = scheduler.build_trigger(Schedule(time="06:00", days=[5, 6]))
    tz = ZoneInfo("America/Los_Angeles")
    when = trigger.get_next_fire_time(None, datetime(2026, 9, 16, 12, 0, tzinfo=tz))  # a Wednesday
    assert when.weekday() == 5                                   # Saturday
    assert when.date() == datetime(2026, 9, 19, tzinfo=tz).date()
    later = trigger.get_next_fire_time(when, when)
    assert later.weekday() == 6                                  # then Sunday


def test_build_trigger_without_days_is_off():
    assert scheduler.build_trigger(Schedule(time="06:00", days=[])) is None


def test_unreadable_schedule_time_falls_back():
    assert scheduler.parse_time("nonsense") == (6, 0)
    assert scheduler.parse_time("07:15") == (7, 15)


def test_no_password_means_no_login(monkeypatch):
    """Unset WEB_PASSWORD: the page opens without credentials and says so."""
    monkeypatch.delenv("WEB_PASSWORD")
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/look")
        assert r.status_code == 200
        assert "WEB_PASSWORD is not set" in r.text
        assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_setting_a_password_turns_login_on(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "pw")
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/look").status_code == 401
        assert "WEB_PASSWORD is not set" not in c.get("/look", auth=("x", "pw")).text


def test_tasks_box_copy_buttons_reveal_the_token_only_behind_a_password(client, monkeypatch):
    monkeypatch.setenv("TASKS_TOKEN", "tok3n-secret-value")
    html = client.get("/sources", auth=AUTH).text
    assert 'data-copy="Bearer tok3n-secret-value"' in html      # WEB_PASSWORD is set in this client
    assert "Copy URL" in html and "Copy example body" in html
    assert html.count("tok3n-secret-value") == 1                  # only inside the copy button


def test_tasks_box_copy_button_is_a_placeholder_on_an_open_page(monkeypatch):
    monkeypatch.delenv("WEB_PASSWORD")
    monkeypatch.setenv("TASKS_TOKEN", "tok3n-secret-value")
    from app.main import app

    with TestClient(app) as c:
        html = c.get("/sources").text
    assert "tok3n-secret-value" not in html
    assert 'data-copy="Bearer <TASKS_TOKEN>"' in html or "data-copy=\"Bearer &lt;TASKS_TOKEN&gt;\"" in html
    assert "Set <code>WEB_PASSWORD</code>" in html


def test_the_box_has_no_token_copy_button_without_a_token(client, monkeypatch):
    monkeypatch.delenv("TASKS_TOKEN", raising=False)
    html = client.get("/sources", auth=AUTH).text
    assert "Copy TASKS_TOKEN header" not in html
    # The slug is not a secret, so its header is always copyable.
    assert 'data-copy="Bearer tasks"' in html
    assert "Copy Authorization header" in html and "Copy URL" in html


def test_the_alias_reads_a_tasks_body_under_any_content_type(client, monkeypatch):
    """The Shortcut that was built for `POST /tasks` keeps working."""
    monkeypatch.setenv("TASKS_TOKEN", "test")
    r = client.post(
        "/tasks",
        headers={"Authorization": "Bearer test", "Content-Type": "text/plain"},
        content='{"tasks": ["one", "two", "three"]}',
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "list": "tasks", "count": 3}


def test_footer_names_the_build(client, monkeypatch):
    monkeypatch.setenv("APP_BUILD", "4f729dc8110eeb24c2da26c7eeadd70cb3b9deb3")
    assert "Build 4f729dc8110e" in client.get("/look", auth=AUTH).text
    monkeypatch.delenv("APP_BUILD")
    assert "Build dev" in client.get("/look", auth=AUTH).text


def test_the_post_url_honours_a_tls_proxy(client):
    """Tailscale Serve or a reverse proxy: the phone uses the public name."""
    body = client.get(
        "/sources", auth=AUTH,
        headers={"host": "127.0.0.1:8080", "x-forwarded-host": "paper.tail1234.ts.net",
                 "x-forwarded-proto": "https"},
    ).text
    assert 'href="https://paper.tail1234.ts.net/lists/tasks"' in body
    assert "127.0.0.1" not in body.split("Address the phone posts to")[0]


def test_look_tab_offers_date_styles_and_saves_one(client):
    html = client.get("/look", auth=AUTH).text
    assert 'name="date_format" value="long" checked' in html
    assert 'value="iso"' in html
    client.post("/look", auth=AUTH, data={"paper_name": "Personal Paper", "date_format": "iso"})
    assert Settings.load().look.date_format == "iso"
    client.post("/look", auth=AUTH, data={"paper_name": "Personal Paper", "date_format": "bogus"})
    assert Settings.load().look.date_format == "iso"          # unknown keys are ignored


# ------------------------------------------------------------------- reprint
def escaped(text: str) -> str:
    """As the template writes it: Jinja escapes the apostrophes."""
    return text.replace("'", "&#39;")


def right_now(body: str) -> str:
    """The "Right now" section of the Output tab, on its own."""
    return body.split("<h2>Right now</h2>", 1)[1].split("<h2>", 1)[0]


@pytest.fixture
def archived(data_dir):
    """One issue on file, and the state the run that made it left behind."""
    import state

    archive = data_dir / "archive"
    archive.mkdir()
    pdf = archive / "2026-09-18.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    state.update_state(
        last_pdf=str(pdf), last_pages=2, last_issue=12, issue=12,
        seen_posts=["post-1"], last_run="2026-09-18T06:00:00-07:00",
        last_success="2026-09-18T06:00:00-07:00",
    )
    return pdf


@pytest.fixture
def fake_routes(monkeypatch):
    """`deliver.deliver` and the page counter, with no printer and no SMTP."""
    import deliver
    import deliver.printer

    calls: dict[str, object] = {"pdfs": [], "pages": [], "result": {"print": None, "email": None}}

    def fake_deliver(pdf, settings, *, test=False, pages=None):
        calls["pdfs"].append(Path(pdf))
        calls["pages"].append(pages)
        return dict(calls["result"])

    monkeypatch.setattr(deliver, "deliver", fake_deliver)
    monkeypatch.setattr(deliver.printer, "page_count", lambda pdf: 2)
    return calls


def test_reprint_sends_the_latest_issue_again(client, data_dir, archived, fake_routes):
    """Every enabled route, the archived PDF, and nothing else touched."""
    import state

    before = state.load_state()
    body = client.post("/reprint", auth=AUTH).json()
    assert body["ok"] is True

    job = wait_for(body["job"], timeout=20)
    assert job.status == "done", job.error
    assert fake_routes["pdfs"] == [archived]
    assert fake_routes["pages"] == [2]
    assert job.result == {
        "ok": True,
        "file": "2026-09-18.pdf",
        "pages": 2,
        "error": "",
        "delivery": {"print": "ok", "email": "ok"},
    }
    # No issue counted, no post marked seen, no run recorded.
    assert state.load_state() == before
    assert "reprinted 2026-09-18.pdf: print: ok; email: ok" in (
        data_dir / "logs" / "run.log").read_text()


def test_a_reprint_reports_a_route_that_failed(client, archived, fake_routes):
    fake_routes["result"] = {"print": "RuntimeError: printer offline", "email": None}
    body = client.post("/reprint", auth=AUTH).json()
    job = wait_for(body["job"], timeout=20)

    assert job.status == "done", job.error
    assert job.result["delivery"] == {"print": "RuntimeError: printer offline", "email": "ok"}
    assert "printer offline" in job.result["error"]
    assert job.result["ok"] is True          # one good route is enough, as in a run


def test_reprint_with_nothing_on_file_is_refused(client):
    response = client.post("/reprint", auth=AUTH)
    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "No paper has been made yet."}

    section = right_now(client.get("/output", auth=AUTH).text)
    assert "disabled" in section
    assert "No paper has been made yet." in section


def test_the_right_now_buttons_say_what_each_one_does(client, archived):
    settings = Settings()
    settings.output.print.enabled = True
    settings.save()

    section = right_now(client.get("/output", auth=AUTH).text)
    assert "disabled" not in section
    wanted = [
        escaped("Print this morning's paper again"),
        "Sends the latest issue exactly as it was made: 2026-09-18, No. 12.",
        "Make a new paper now",
        escaped("Gathers the next stories from the queue, makes a new issue and "
                "delivers it. The stories it prints are marked as printed."),
        "Test run (nothing printed or emailed)",
        "nothing is sent and no story is marked as printed",
    ]
    places = [section.index(text) for text in wanted]        # each one is there
    assert places == sorted(places)                          # and in this order


def test_the_reprint_button_names_the_routes_that_are_on(client, archived):
    settings = Settings()
    settings.save()
    assert escaped("Deliver this morning's paper again") in client.get("/output", auth=AUTH).text

    settings.output.email.enabled = True
    settings.output.email.to = ["her@example.com"]
    settings.save()
    assert escaped("Send this morning's paper again") in client.get("/output", auth=AUTH).text

    settings.output.print.enabled = True
    settings.save()
    assert escaped("Print this morning's paper again") in client.get("/output", auth=AUTH).text


def test_the_status_strip_names_the_latest_issue(client, archived):
    strip = client.get("/output", auth=AUTH).text.split(
        '<section class="status"', 1)[1].split("</section>", 1)[0]
    assert '<a href="/archive/2026-09-18.pdf">2026-09-18, No. 12</a>' in strip


def test_the_reader_can_mark_a_post_printed_or_unread(client):
    import state

    r = client.post("/sources/substack/mark", auth=AUTH, json={"guid": "post-9", "printed": True})
    assert r.status_code == 200 and r.json()["ok"]
    assert "post-9" in state.load_state()["seen_posts"]
    r = client.post("/sources/substack/mark", auth=AUTH, json={"guid": "post-9", "printed": False})
    assert r.json()["ok"]
    assert "post-9" not in state.load_state()["seen_posts"]
    r = client.post("/sources/substack/mark", auth=AUTH, json={"guid": "", "printed": True})
    assert not r.json()["ok"]


def test_the_queue_table_has_mark_buttons(client):
    html = client.get("/sources", auth=AUTH).text
    assert "Mark as printed" in html and "Mark as unread" in html
    assert "/sources/substack/mark" in html


def test_the_look_further_back_option_saves(client):
    client.post("/sources", auth=AUTH, data={"article_max_age_days": "7", "extend_window_when_empty": "on"})
    assert Settings.load().sources.extend_window_when_empty is True
    client.post("/sources", auth=AUTH, data={"article_max_age_days": "7"})
    assert Settings.load().sources.extend_window_when_empty is False
    assert "Look further back" in client.get("/sources", auth=AUTH).text


# ---------------------------------------------------------- the theme switch
#: How the settings page is set for the reader's own eyes. It is this page's
#: furniture and nothing else: the paper is black on white whatever it says.
def test_the_page_ships_following_the_machine(client):
    assert Settings().web.theme == "auto"
    assert 'data-theme="auto"' in client.get("/look", auth=AUTH).text


def test_the_switch_sets_the_theme_and_comes_back_to_the_tab(client):
    r = client.post("/theme", auth=AUTH, data={"theme": "dark", "next": "/sources"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sources"
    assert Settings.load().web.theme == "dark"


@pytest.mark.parametrize("tab", ["look", "layout", "sources", "output", "preview", "log"])
def test_every_tab_is_set_the_way_she_chose(client, tab):
    client.post("/theme", auth=AUTH, data={"theme": "dark", "next": "/look"})
    html = client.get(f"/{tab}", auth=AUTH).text
    assert 'data-theme="dark"' in html
    # ... and the switch says which one is on, for a reader who cannot see it
    assert 'aria-current="true"' in html


def test_a_theme_the_page_does_not_know_changes_nothing(client):
    client.post("/theme", auth=AUTH, data={"theme": "dark", "next": "/look"})
    client.post("/theme", auth=AUTH, data={"theme": "chartreuse", "next": "/look"})
    assert Settings.load().web.theme == "dark"


@pytest.mark.parametrize("nowhere", ["//elsewhere.example", "https://elsewhere.example/x",
                                     "", "javascript:alert(1)"])
def test_the_switch_only_ever_sends_her_back_to_this_page(client, nowhere):
    """`next` comes off a form, so it is treated as somebody else's idea."""
    r = client.post("/theme", auth=AUTH, data={"theme": "light", "next": nowhere},
                    follow_redirects=False)
    assert r.headers["location"] == "/look"


def test_the_theme_is_not_a_setting_of_the_paper(client):
    """Saving a tab leaves it alone, and it never reaches the Look."""
    client.post("/theme", auth=AUTH, data={"theme": "dark", "next": "/look"})
    client.post("/look", auth=AUTH, data={"paper_name": "The Morning"})
    settings = Settings.load()
    assert settings.web.theme == "dark"
    assert settings.look.paper_name == "The Morning"
    assert "theme" not in settings.look.model_dump()


def test_a_settings_file_written_before_there_was_a_choice(client, data_dir):
    """No `web` key means the page as it was: whatever the machine is doing."""
    path = Settings.path()
    written = json.loads(path.read_text()) if path.exists() else {}
    written.pop("web", None)
    path.write_text(json.dumps(written))
    assert Settings.load().web.theme == "auto"
    assert 'data-theme="auto"' in client.get("/look", auth=AUTH).text


#: The palette itself. The page is styled from tokens so that the whole of
#: it turns over at once; a colour written straight into a rule would stay
#: light in the dark, which is the one way this quietly breaks.
def test_the_stylesheet_has_no_colour_outside_the_palette():
    from pathlib import Path
    import re

    css = Path("app/static/style.css").read_text()
    palette, rest = css.split("* { box-sizing: border-box; }", 1)
    stray = re.findall(r"(?<![\w-])#[0-9a-fA-F]{3,8}\b", rest)
    assert stray == [], f"colours set outside the palette: {stray}"
    # Both ways in: the machine's preference, and the reader overruling it.
    assert '@media (prefers-color-scheme: dark)' in palette
    assert ':root:not([data-theme="light"])' in palette, "light must beat the machine"
    assert ':root[data-theme="dark"]' in palette, "dark must beat the machine too"
    assert "color-scheme: dark" in palette, "so the browser's own controls follow"


def test_the_two_dark_blocks_are_the_same_palette():
    """They are written twice because CSS cannot share them; they must agree."""
    from pathlib import Path
    import re

    css = Path("app/static/style.css").read_text()
    blocks = re.findall(r"color-scheme: dark;(.*?)\n\s*\}", css, re.S)
    assert len(blocks) == 2, "one for the media query, one for the reader's choice"
    tokens = [dict(re.findall(r"(--[\w-]+):\s*([^;]+);", b)) for b in blocks]
    assert tokens[0] == tokens[1]
    assert tokens[0], "the dark blocks actually set something"


# --------------------------------------------- the page actually going dark
#: Two ways the theme quietly failed on a real container, both fixed here.
def test_the_browser_must_ask_before_reusing_a_static_file(client):
    """The white-page bug.

    Starlette sends an ETag and no `Cache-Control`, so a browser may decide
    for itself how long a file stays fresh. One did: it served the previous
    image's stylesheet with the new page, and the dark theme the reader had
    just chosen did nothing at all, because that stylesheet had no dark
    palette in it. `no-cache` makes it ask every time.
    """
    response = client.get("/static/style.css", auth=AUTH)
    assert response.headers["cache-control"] == "no-cache"
    # ... and asking is cheap: the answer is a 304 with no body.
    again = client.get("/static/style.css", auth=AUTH,
                       headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304


def test_a_container_update_is_a_new_address_for_the_stylesheet(client, monkeypatch):
    """Belt and braces: even a browser that ignores the header cannot reuse
    the last image's file, because the url it was stored under is gone."""
    monkeypatch.setenv("APP_BUILD", "abc123def4567890")
    html = client.get("/look", auth=AUTH).text
    assert 'href="/static/style.css?v=abc123def456"' in html
    assert 'src="/static/app.js?v=abc123def456"' in html


def test_the_theme_switch_is_legible_in_both_themes():
    """It was grey small caps once: at 0.85rem that is not a control, it is
    a caption nobody can read. Every choice is in full ink now, and the one
    that is on is printed in reverse, which reads either way round."""
    from pathlib import Path

    css = Path("app/static/style.css").read_text()
    rule = css.split(".theme button {", 1)[1].split("}", 1)[0]
    assert "color: var(--ink)" in rule, "the words are set in the page's own ink"
    assert "var(--grey)" not in rule and "small-caps" not in rule
    assert "border: var(--rule)" in rule, "it has an edge, so it reads as a control"
    on = css.split(".theme button.on {", 1)[1].split("}", 1)[0]
    assert "background: var(--ink)" in on and "color: var(--paper)" in on


#: The contract the palette has to keep, checked where it is actually
#: decided: in a browser, with the real stylesheet and the real cascade.
#: "dark" and "light" are the reader overruling her machine, so each is
#: tried against the machine set the other way.
THEME_CASES = [
    ("auto", "light", False),      # no choice made: follow the machine
    ("auto", "dark", True),
    ("light", "dark", False),      # her choice beats the machine, both ways
    ("dark", "light", True),       # ... this one is the bug that was reported
]


@pytest.mark.parametrize("theme,machine,expect_dark", THEME_CASES)
def test_the_page_is_painted_the_way_she_set_it(client, tmp_path, theme, machine, expect_dark):
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    client.post("/theme", auth=AUTH, data={"theme": theme, "next": "/look"})
    html = client.get("/look", auth=AUTH).text
    assert f'data-theme="{theme}"' in html

    # The page and its stylesheet, side by side, so the cascade is the real
    # one and no server is needed.
    page_file = tmp_path / "look.html"
    page_file.write_text(re.sub(r'href="/static/style\.css[^"]*"', 'href="style.css"', html))
    (tmp_path / "style.css").write_text(Path("app/static/style.css").read_text())

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(color_scheme=machine)
        page = context.new_page()
        page.goto(page_file.as_uri())
        painted = page.evaluate("""() => {
          const body = getComputedStyle(document.body);
          const off = [...document.querySelectorAll('.theme button')]
              .find(b => !b.classList.contains('on'));
          return {bg: body.backgroundColor, ink: body.color,
                  offColor: getComputedStyle(off).color};
        }""")
        browser.close()

    def channels(css_colour):
        return [int(n) for n in re.findall(r"\d+", css_colour)[:3]]

    background = sum(channels(painted["bg"])) / 3
    ink = sum(channels(painted["ink"])) / 3
    if expect_dark:
        assert background < 60, f"{theme} on a {machine} machine painted {painted['bg']}"
        assert ink > 180, "and the type has to be light on it"
    else:
        assert background > 200, f"{theme} on a {machine} machine painted {painted['bg']}"
        assert ink < 60

    # The choices she has not made stay as readable as the one she has:
    # dimming them to grey small caps is what made the switch unreadable.
    assert channels(painted["offColor"]) == channels(painted["ink"])


#: Selectors that reach a form control, which the browser draws with its own
#: colours unless told otherwise.
CONTROL_SELECTORS = ("button", "input", "select", "textarea")


def test_no_control_sets_a_background_without_its_ink():
    """The blank-buttons bug, pinned at the cause.

    `button { background: #fff }` with no `color` leaves the text to the
    user agent, which paints `buttontext` white on a device whose system is
    dark -- white on an explicitly white button, so the switch rendered as
    three empty boxes. A control that claims its background claims its
    foreground in the same breath.
    """
    from pathlib import Path
    import re

    css = Path("app/static/style.css").read_text()
    offenders = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selector = selector.strip()
        if selector.startswith("@") or ":root" in selector:
            continue
        if not any(re.search(rf"(^|[\s,>+~]){c}\b", selector) for c in CONTROL_SELECTORS):
            continue
        if re.search(r"(^|[;\s])background(-color)?\s*:", body) and not re.search(r"(^|[;\s])color\s*:", body):
            # A :hover that only re-tints an already-inked rule is fine.
            if ":hover" in selector or ":focus" in selector:
                continue
            offenders.append(selector)
    assert offenders == [], f"controls with a background but no colour: {offenders}"


def test_the_page_declares_which_scheme_its_controls_are_drawn_in():
    """The other half of that defence: with `color-scheme` declared, the
    browser draws its own widgets -- checkboxes, radios, the time picker --
    to match the page instead of guessing from the system."""
    from pathlib import Path
    import re

    css = Path("app/static/style.css").read_text()
    root = css.split("* { box-sizing: border-box; }", 1)[0]
    assert "color-scheme: light" in root
    # The declaration, not the `prefers-color-scheme: dark` query around it.
    declared = re.findall(r"(?<!-)color-scheme:\s*dark", root)
    assert len(declared) == 2, "both ways into the dark palette declare it"
