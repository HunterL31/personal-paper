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
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS_PATH_ENV = "SETTINGS_PATH"

FONT_CHOICES_MASTHEAD = ["Maguntia", "UnifrakturCook"]
FONT_CHOICES_HEAD = ["Old Standard", "Playfair Display", "PT Serif", "EB Garamond", "Libre Baskerville"]
FONT_CHOICES_BODY = ["PT Serif", "EB Garamond", "Libre Baskerville", "Old Standard"]


# ---------------------------------------------------------------- Look
class Ear(BaseModel):
    """Text in the boxes either side of the masthead."""
    initials: str = ""            # the reader's monogram; empty means no line
    lines: list[str] = Field(default_factory=lambda: ["Continued stories inside."])


class Look(BaseModel):
    paper_name: str = "Personal Paper"
    imprint: str = "Printed at home before sunrise"
    price: str = "Single copy, free"
    ear: Ear = Field(default_factory=Ear)
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


class Sources(BaseModel):
    calendars: list[CalendarSource] = Field(default_factory=list)
    substacks: list[SubstackSource] = Field(default_factory=list)
    weather: WeatherSource = Field(default_factory=WeatherSource)
    crossword: CrosswordSource = Field(default_factory=CrosswordSource)
    #: How far back the article queue looks. A post older than this is never
    #: printed, so switching on a publication does not print its archive.
    article_max_age_days: int = Field(7, ge=1, le=60)
    tasks_max_age_hours: int = 24
    #: What the Sources tab tells the phone to post to, when the address the
    #: browser used is not the address the phone can reach (a reverse proxy,
    #: or the page opened on the box itself). Empty means "work it out".
    tasks_post_url: str = ""


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


class Settings(BaseModel):
    look: Look = Field(default_factory=Look)
    sources: Sources = Field(default_factory=Sources)
    output: Output = Field(default_factory=Output)

    # -- persistence -------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return Path(os.environ.get(SETTINGS_PATH_ENV, DATA_DIR / "settings.json"))

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            return cls.model_validate_json(p.read_text())
        except Exception:
            # A corrupt file must never stop the paper; the UI shows defaults.
            return cls()

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
