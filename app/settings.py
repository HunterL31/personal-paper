"""
Settings model: the contract between the web page, the run, the gatherers,
and the delivery routes.

Two stores, one rule: credentials live in the container's environment
(see `Env`); everything the reader would change lives in
`/data/settings.json` (see `Settings`), edited from the web page.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS_PATH_ENV = "SETTINGS_PATH"

#: The faces the Look tab offers, in the order the picker shows them. Every
#: name here is a family in `render.fontlist.FONT_FILES`, which is where the
#: files behind it live; an unknown name in a settings file is ignored and
#: the paper is set in the default (see `render/template.html`).
FONT_CHOICES_MASTHEAD = [
    "Maguntia", "UnifrakturCook", "Pirata One", "Grenze Gotisch",
    "Cinzel Decorative", "Playfair Display",
]
FONT_CHOICES_HEAD = [
    "Old Standard", "Playfair Display", "PT Serif", "EB Garamond", "Libre Baskerville",
    "Bodoni Moda", "Cormorant Garamond", "Libre Caslon Text", "Oswald",
]
FONT_CHOICES_BODY = [
    "PT Serif", "EB Garamond", "Libre Baskerville", "Old Standard",
    "Merriweather", "Source Serif 4", "Crimson Pro", "Lora", "Literata",
]

#: The list every paper starts with, and the slug the phone has always
#: posted to (`POST /tasks` is still an alias for it).
DEFAULT_LIST_NAME = "To do"
DEFAULT_LIST_SLUG = "tasks"
#: A slug is what the phone puts in the URL and, on the owner's own
#: request, in the Authorization header. Lowercase, digits and hyphens.
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
SLUG_MAX = 40


def slugify(name: str) -> str:
    """"Weekend shopping" -> "weekend-shopping". Empty when nothing is left."""
    parts = re.findall(r"[a-z0-9]+", (name or "").lower())
    return "-".join(parts)[:SLUG_MAX].strip("-")


# ---------------------------------------------------------------- Look
#: What an ear box can be set to, in the order the Look tab offers them.
#: The template draws each one; anything else in a settings file is an
#: empty box, never an exception.
EAR_KINDS = [
    "weather", "date", "monogram", "issue", "text",
    "next_event", "sun", "countdown", "puzzle", "none",
]
#: What the Look tab calls each one. The paper prints no label of its own:
#: an ear is the thing itself, not a heading over it.
EAR_KIND_LABELS = {
    "weather": "Today's weather",
    "date": "The date",
    "monogram": "Monogram and lines",
    "issue": "Volume and number",
    "text": "Lines of your own",
    "next_event": "The next thing on today",
    "sun": "Sunrise and sunset",
    "countdown": "A countdown",
    "puzzle": "The crossword",
    "none": "Nothing (no box)",
}
#: Which of an ear's fields the Look tab shows for a kind, by the field's
#: own name. Every field is submitted whatever is picked; only the ones a
#: kind uses are read back.
EAR_KIND_FIELDS = {
    "monogram": ["initials", "lines"],
    "text": ["lines"],
    "countdown": ["countdown_date", "countdown_label"],
}
#: Where the date is printed. "folio" is the rule under the masthead, as
#: the paper has always set it; "none" leaves it to an ear, or to nowhere.
DATE_PLACES = ["folio", "above", "below", "none"]
DATE_PLACE_LABELS = {
    "folio": "On the rule under the masthead",
    "above": "Above the masthead",
    "below": "Below the masthead",
    "none": "Only where an ear shows it",
}


class EarBox(BaseModel):
    """One of the two boxes either side of the masthead.

    `kind` is what the box shows; the rest are the words the kinds that
    need them are set from, and are kept whatever the kind is, so that
    trying another one and coming back loses nothing.
    """
    kind: Literal[
        "weather", "date", "monogram", "issue", "text",
        "next_event", "sun", "countdown", "puzzle", "none",
    ] = "none"
    initials: str = ""            # monogram: the reader's own; empty means no line
    lines: list[str] = Field(default_factory=list)   # monogram and text
    countdown_date: str = ""      # YYYY-MM-DD
    countdown_label: str = ""     # "Sarah's visit"


# -------------------------------------------------------------- Layout
#: The sections the paper can hold, besides the reader's own lists. A list
#: is the key `list:<slug>` of one of `sources.lists`.
FIXED_SECTIONS = ["agenda", "hourly", "notes"]
#: What the Layout tab calls each section: the reader's words, not the
#: paper's. The heading actually printed is `PRINTED_HEADINGS` below, and
#: the tab shows it under the name so the two are never confused.
SECTION_LABELS = {
    "agenda": "Today's agenda",
    "hourly": "Hourly forecast",
    "notes": "Notes",
}
#: The heading `render/template.html` sets over each section. Newspaper
#: voice: it is the paper's, and nothing on the web page may change it.
PRINTED_HEADINGS = {"agenda": "Today", "hourly": "Hour by hour", "notes": "Notes"}
#: Where a section goes. The stored words are the paper's own ("rail" is
#: page 1's reader column); the labels are what the Layout tab shows, so a
#: settings file written before the tab was reworded still loads.
PLACES = ["rail", "page2", "off"]
PLACE_LABELS = {"rail": "Page 1", "page2": "Page 2", "off": "Not shown"}
#: Which edge of the sheet the reader's column runs down.
RAIL_SIDES = ["right", "left"]
RAIL_SIDE_LABELS = {"right": "Right", "left": "Left"}


def list_key(slug: str) -> str:
    """The layout key of a list: `list:groceries`."""
    return f"list:{slug}"


def key_slug(key: str) -> str:
    """The slug inside a `list:<slug>` key, or "" for a fixed section."""
    return key[5:] if key.startswith("list:") else ""


class RailSection(BaseModel):
    """One section of the paper's furniture, and where it goes.

    `key` is `agenda`, `hourly`, `notes`, or `list:<slug>`. A key naming a
    list that no longer exists is ignored by the template; the Sources tab
    drops it when the list is removed.
    """
    key: str
    place: Literal["rail", "page2", "off"] = "rail"


def default_sections() -> list["RailSection"]:
    """Today, the to-do list, the hours, the notes: the rail as it ships."""
    return [
        RailSection(key="agenda"),
        RailSection(key=list_key(DEFAULT_LIST_SLUG)),
        RailSection(key="hourly"),
        RailSection(key="notes"),
    ]


class Layout(BaseModel):
    """Where the furniture goes, and how much room the paper gives it."""
    #: In order: the rail fills page 1's right column top to bottom, and
    #: `page2` sections go in a column beside the continuations.
    sections: list[RailSection] = Field(default_factory=default_sections)
    #: Which edge of the sheet the reader's column runs down. "right" is
    #: the paper as it has always been set.
    rail_side: Literal["left", "right"] = "right"
    rail_width_in: float = Field(1.9, ge=1.5, le=2.8)
    front_stories: int = Field(4, ge=1, le=4)
    crossword_place: Literal["bottom", "top"] = "bottom"
    crossword_cell_in: float = Field(0.19, ge=0.14, le=0.26)
    crossword_max_pct: int = Field(55, ge=25, le=75)
    #: The stories' pictures, on a sheet of their own after page 2. Each is
    #: numbered, and a line of the paper's own between the author's
    #: paragraphs says which one belongs there. Off, the paper is exactly
    #: the one sheet it has always been and no picture is fetched.
    pictures: bool = False


class Look(BaseModel):
    paper_name: str = "Personal Paper"
    #: a key of app.dates.DATE_FORMATS; how the folio line writes the date
    date_format: str = "long"
    imprint: str = "Printed at home before sunrise"
    price: str = "Single copy, free"
    #: The two boxes either side of the masthead. The weather on the left
    #: and the reader's line on the right are the paper as it has always
    #: been set; either box may be set to any of `EAR_KINDS`.
    ear_left: EarBox = Field(default_factory=lambda: EarBox(kind="weather"))
    ear_right: EarBox = Field(default_factory=lambda: EarBox(
        kind="monogram", initials="", lines=["Continued stories inside."]))
    #: Where the date is printed, and how it is set there. The folio at 8pt
    #: is the rule under the masthead; a line above or below it wants 10-14.
    date_place: Literal["folio", "above", "below", "none"] = "folio"
    date_font: str = "Old Standard"        # any FONT_CHOICES_HEAD entry
    date_size_pt: float = Field(8.0, ge=6.0, le=24.0)
    masthead_font: str = "Maguntia"
    headline_font: str = "Old Standard"
    body_font: str = "PT Serif"
    body_size_pt: float = Field(9.0, ge=8.0, le=11.0)      # half-point steps in the UI
    lead_body_height_in: float = Field(2.7, ge=1.5, le=5.0)
    #: Justified columns, as a newspaper sets them. Off gives a ragged
    #: right edge, which some readers find easier in narrow measures.
    justify: bool = True
    show_todo: bool = True
    show_hourly: bool = True
    show_notes: bool = True
    layout: Layout = Field(default_factory=Layout)

    @model_validator(mode="before")
    @classmethod
    def _ear_right_from_legacy(cls, data):
        """A settings file from before either ear could be set.

        It has one `ear`, the monogram and lines of the right-hand box; it
        becomes `ear_right`, so the paper is set exactly as it was.
        """
        if not isinstance(data, dict) or "ear_right" in data:
            return data
        legacy = data.get("ear")
        if hasattr(legacy, "model_dump"):
            legacy = legacy.model_dump()
        if not isinstance(legacy, dict):
            return data
        data = dict(data)
        data["ear_right"] = {
            "kind": "monogram",
            "initials": legacy.get("initials", "") or "",
            "lines": list(legacy.get("lines", ["Continued stories inside."]) or []),
        }
        return data


# ------------------------------------------------------------- Sources
class CalendarSource(BaseModel):
    url: str                       # secret iCal address; masked in the UI after save
    name: str = ""                 # display name, filled by "Check"


class SubstackSource(BaseModel):
    name: str                      # the <name> in <name>.substack.com, or a full feed URL
    paid: bool = False


class WeatherSource(BaseModel):
    lat: float = 37.7749
    lon: float = -122.4194


class CrosswordSource(BaseModel):
    """The day's puzzle, appended as the back page(s).

    The subscriber's `NYT_S` cookie is a credential, so it lives in the
    container's variables, never here. `days` is which weekdays get a
    puzzle (0 = Monday), for a reader who skips, say, Saturday.
    """
    enabled: bool = False
    provider: Literal["nyt"] = "nyt"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])


class ListSource(BaseModel):
    """One of the reader's lists: a name, the slug the phone posts to, and
    how it is set on paper.

    The slug is derived from the name when it is left empty, and never
    changes afterwards: it is half of the address the phone is configured
    with.
    """
    name: str                      # "To do", "Groceries", "Packing"
    slug: str = ""                 # derived from the name when empty
    style: Literal["checkbox", "plain", "numbered"] = "checkbox"
    max_age_hours: int = Field(24, ge=1, le=168)

    @model_validator(mode="after")
    def _derive_slug(self) -> "ListSource":
        if not self.slug:
            self.slug = slugify(self.name) or "list"
        return self


def default_lists() -> list[ListSource]:
    return [ListSource(name=DEFAULT_LIST_NAME, slug=DEFAULT_LIST_SLUG)]


class Sources(BaseModel):
    calendars: list[CalendarSource] = Field(default_factory=list)
    substacks: list[SubstackSource] = Field(default_factory=list)
    weather: WeatherSource = Field(default_factory=WeatherSource)
    crossword: CrosswordSource = Field(default_factory=CrosswordSource)
    #: How far back the article queue looks. A post older than this is never
    #: printed, so switching on a publication does not print its archive.
    article_max_age_days: int = Field(7, ge=1, le=60)
    #: when nothing unread is inside the window, look further back in steps
    #: (14, 30, 60, 90, 180, 365 days) rather than print a paper with no story
    extend_window_when_empty: bool = False
    #: The reader's lists, in the order they were added; one file each
    #: under `<DATA_DIR>/lists/<slug>.json`.
    lists: list[ListSource] = Field(default_factory=default_lists)
    #: Kept for settings files written before there were named lists: it is
    #: the age limit the single to-do list was given, and `_lists_from_tasks`
    #: below builds that list out of it on load.
    tasks_max_age_hours: int = 24
    #: What the Sources tab tells the phone to post to, when the address the
    #: browser used is not the address the phone can reach (a reverse proxy,
    #: or the page opened on the box itself). Empty means "work it out".
    tasks_post_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _lists_from_tasks(cls, data):
        """A settings file from before named lists has no `lists` key: its
        one to-do list is rebuilt from `tasks_max_age_hours`."""
        if isinstance(data, dict) and "lists" not in data:
            data = dict(data)
            data["lists"] = [{
                "name": DEFAULT_LIST_NAME,
                "slug": DEFAULT_LIST_SLUG,
                "max_age_hours": data.get("tasks_max_age_hours", 24),
            }]
        return data

    def list_by_slug(self, slug: str) -> Optional[ListSource]:
        return next((li for li in self.lists if li.slug == slug), None)

    def slugs(self) -> list[str]:
        return [li.slug for li in self.lists]


# -------------------------------------------------------------- Output
class PrintRoute(BaseModel):
    enabled: bool = False
    printer_host: str = ""         # IP or hostname; "" means not configured
    printer_name: str = ""         # display name from discovery/test
    duplex: bool = True


class EmailRoute(BaseModel):
    enabled: bool = False
    to: list[str] = Field(default_factory=list)
    subject: str = "Personal Paper, {date}"


class Schedule(BaseModel):
    time: str = "06:00"            # HH:MM in TZ
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # 0 = Monday


class Output(BaseModel):
    print: PrintRoute = Field(default_factory=PrintRoute)
    email: EmailRoute = Field(default_factory=EmailRoute)
    schedule: Schedule = Field(default_factory=Schedule)
    notify: Literal["none", "email", "unraid"] = "none"
    notify_email: str = ""


# ----------------------------------------------------------------- Web
class Web(BaseModel):
    """This page, not the paper.

    The paper is black on white, always: `Look` says nothing about screens
    and nothing here reaches the sheet. `theme` is how the settings page is
    set for the reader's own eyes -- "auto" follows whatever the phone or
    the laptop is already doing, which is what it ships as.
    """
    theme: Literal["auto", "light", "dark"] = "auto"


#: What the theme switch calls each choice.
THEMES = ["auto", "light", "dark"]
THEME_LABELS = {"auto": "Auto", "light": "Light", "dark": "Dark"}


# ------------------------------------------------------------- Logging
#: How many past runs enhanced logging keeps a file for.
KEEP_RUNS_DEFAULT = 7


class Logs(BaseModel):
    """Enhanced logging: a file of its own for each of the last few runs.

    Off (the default), a run logs what it always has: a line an event,
    appended to `<DATA_DIR>/logs/run.log`, which the Log page tails. On,
    the run also writes `<DATA_DIR>/logs/runs/<date>-<time>.log` at debug
    level, with what it was set to and what each step did, and the page
    offers that file and the `keep_runs - 1` before it for download. Older
    ones are deleted as each run ends, so the directory never grows.
    """
    enhanced: bool = False
    keep_runs: int = Field(KEEP_RUNS_DEFAULT, ge=1, le=30)


class Settings(BaseModel):
    look: Look = Field(default_factory=Look)
    sources: Sources = Field(default_factory=Sources)
    output: Output = Field(default_factory=Output)
    #: How this page is set. A settings file written before there was a
    #: choice has no `web` key and gets "auto", which is the page as it was.
    web: Web = Field(default_factory=Web)
    logs: Logs = Field(default_factory=Logs)

    def sync_list_sections(self) -> None:
        """Keep `look.layout.sections` in step with `sources.lists`.

        A list the reader has just added gets a section on page 1 without a
        trip to the Layout tab; a list they removed loses its section. The
        fixed sections and the order of everything else are left alone.
        """
        slugs = self.sources.slugs()
        kept: list[RailSection] = []
        for section in self.look.layout.sections:
            slug = key_slug(section.key)
            if slug and slug not in slugs:
                continue                       # the list is gone
            kept.append(section)
        have = {s.key for s in kept}
        for slug in slugs:
            if list_key(slug) not in have:
                kept.append(RailSection(key=list_key(slug), place="rail"))
        self.look.layout.sections = kept

    def known_sections(self) -> list[dict]:
        """Every section the Layout tab offers, in the layout's own order.

        `{key, label, prints_as, place, is_list}`: `label` is what the tab
        calls the section and `prints_as` the heading the paper sets over
        it, or "" when the two are the same word.
        """
        names = {li.slug: li.name for li in self.sources.lists}
        places = {s.key: s.place for s in self.look.layout.sections}
        order = [s.key for s in self.look.layout.sections]
        keys = [k for k in order if key_slug(k) in ("", *names)]
        for key in [*FIXED_SECTIONS, *(list_key(s) for s in names)]:
            if key not in keys:
                keys.append(key)
        rows = []
        for key in keys:
            slug = key_slug(key)
            label = names.get(slug, key) if slug else SECTION_LABELS.get(key, key)
            printed = label if slug else PRINTED_HEADINGS.get(key, label)
            rows.append({
                "key": key,
                "label": label,
                # Only worth saying when the paper's heading is another word.
                "prints_as": printed if printed != label else "",
                "place": places.get(key, "rail"),
                "is_list": bool(slug),
            })
        return rows

    # -- persistence -------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        """The current paper's settings file (see `papers.py`).

        `SETTINGS_PATH` moves only the main paper's; every other paper's
        is in its own folder.
        """
        import papers

        if papers.current() != papers.MAIN:
            return papers.data_dir() / "settings.json"
        return Path(os.environ.get(SETTINGS_PATH_ENV, DATA_DIR / "settings.json"))

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            settings = cls.model_validate_json(p.read_text())
        except Exception:
            # A corrupt file must never stop the paper; the UI shows defaults.
            return cls()
        # A list always has a section, even in a file edited by hand or
        # written before that list existed; a section for a list that is
        # gone is dropped. Not written back: the next save does that.
        settings.sync_list_sections()
        return settings

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with _lock:
            tmp.write_text(self.model_dump_json(indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(p)


_lock = threading.Lock()


# ----------------------------------------------------------------- Env
class Env:
    """Credentials and infrastructure, read from the container environment."""

    WEB_PASSWORD = "WEB_PASSWORD"
    TASKS_TOKEN = "TASKS_TOKEN"
    #: The subscriber's NYT-S session cookie, for the crossword PDF.
    NYT_S = "NYT_S"
    SMTP = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")
    IMAP = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")
    TZ = "TZ"

    @staticmethod
    def get(name: str, default: Optional[str] = None) -> Optional[str]:
        v = os.environ.get(name)
        return v if v not in (None, "") else default

    @classmethod
    def is_set(cls, name: str) -> bool:
        return cls.get(name) is not None

    @classmethod
    def tz(cls) -> str:
        return cls.get(cls.TZ, "America/Los_Angeles")

    @classmethod
    def smtp(cls) -> Optional[dict]:
        host, port, user, pw = (cls.get(n) for n in cls.SMTP)
        if not (host and user and pw):
            return None
        return {"host": host, "port": int(port or 587), "user": user, "password": pw}

    @classmethod
    def status(cls) -> dict[str, bool]:
        """What the web page shows: each variable, set or not. Never the value."""
        names = [cls.WEB_PASSWORD, cls.TASKS_TOKEN, cls.NYT_S, *cls.SMTP, *cls.IMAP, cls.TZ]
        return {n: cls.is_set(n) for n in names}
