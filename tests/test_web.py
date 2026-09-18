"""
The web layer: auth, each tab saving its own section, the Check endpoints
never crashing the page, `POST /tasks`, the archive route's name check, a
real sample preview, and the scheduler's trigger.

Nothing here touches the network: every gatherer call is monkeypatched but
the preview, which renders the sample issue in the bundled Chromium.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler
from app.settings import (
    FONT_CHOICES_BODY,
    FONT_CHOICES_HEAD,
    FONT_CHOICES_MASTHEAD,
    CalendarSource,
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
            "ear_initials": "M. L.",
            "ear_lines": "One line\nAnother line\n",
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
    assert saved.look.ear.lines == ["One line", "Another line"]
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

    # And the tab shows it back: the saved side is the selected one.
    body = client.get("/layout", auth=AUTH).text
    assert '<option value="left" selected>Left</option>' in body


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
    """Look, Layout, Sources, Output, Preview -- the order of the work."""
    import re as _re

    for tab in ("look", "layout", "sources", "output", "preview"):
        body = client.get(f"/{tab}", auth=AUTH).text
        nav = body.split('<nav class="tabs">')[1].split("</nav>")[0]
        assert _re.findall(r'>([A-Za-z]+)</a>', nav) == [
            "Look", "Layout", "Sources", "Output", "Preview"
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
                Step("pdf", True, "Accepts application/pdf"),
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
        "resolve", "connect", "ipp", "pdf", "state"
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

    assert client.get("/archive/2026-09-16.pdf", auth=AUTH).status_code == 200
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
