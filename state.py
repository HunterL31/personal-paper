"""
`<DATA_DIR>/state.json`: the few facts that survive between runs.

Keys
----
issue         int   number of issues printed so far; the next paper is issue + 1
first_issue_date  str  ISO date of issue 1; the volume is the year of publication, counted from here
volume        int   the volume `issue` belongs to; numbering restarts at 1 in a new volume
issues_total  int   issues printed over all volumes
seen_posts    list  Substack post GUIDs already printed (gather/substack.py)
last_run      str   ISO timestamp of the last attempt
last_success  str   ISO timestamp of the last successful run
last_error    str   message of the last failure ("" once a run succeeds)
last_pages    int   page count of the last successful paper
last_pdf      str   path of the last archived PDF
last_issue    int   the issue number that PDF carries (0 when it is not known)

Everything is written atomically; a corrupt or missing file reads as defaults,
because a bad state file must never stop the paper.
"""
from __future__ import annotations

from datetime import date as _date

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
_lock = threading.Lock()

DEFAULT_STATE: dict[str, Any] = {
    "issue": 0,
    "seen_posts": [],
    "last_run": "",
    "last_success": "",
    "last_error": "",
    "last_pages": 0,
    "last_pdf": "",
    "last_issue": 0,
}


def data_dir() -> Path:
    """DATA_DIR is read on every call so tests (and the web app) can move it."""
    return Path(os.environ.get("DATA_DIR", "/data"))


def state_path() -> Path:
    return data_dir() / "state.json"


def load_state() -> dict[str, Any]:
    p = state_path()
    state = json.loads(json.dumps(DEFAULT_STATE))
    try:
        if p.exists():
            stored = json.loads(p.read_text())
            if isinstance(stored, dict):
                state.update(stored)
    except Exception as exc:  # corrupt file: start from defaults, keep going
        log.warning("state.json unreadable (%s); using defaults", exc)
    return state


def save_state(state: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


def update_state(**values: Any) -> dict[str, Any]:
    """Merge `values` into the stored state and write it back."""
    state = load_state()
    state.update(values)
    save_state(state)
    return state


def _as_date(today) -> _date:
    if isinstance(today, _date):
        return today
    if today:
        try:
            return _date.fromisoformat(str(today)[:10])
        except ValueError:
            pass
    return _date.today()


def next_issue(today=None) -> int:
    """The number the paper being made now will carry.

    Numbering restarts at 1 with each new volume, so the first paper of a
    new year of publication is No. 1 again.
    """
    state = load_state()
    if int(state.get("volume", 0) or 0) and volume_number(_as_date(today)) != int(state["volume"]):
        return 1
    return int(state.get("issue", 0)) + 1


def bump_issue(today: str | None = None) -> int:
    """Count one printed issue and return the new number.

    The first print also records `first_issue_date`, which the volume
    number counts from (one volume per year of publication). When the
    volume has moved on since the last print, the count restarts at 1.
    `issues_total` keeps counting across volumes.
    """
    state = load_state()
    day = _as_date(today)
    if not state.get("first_issue_date"):
        state["first_issue_date"] = day.isoformat()
    volume = _volume_for(state.get("first_issue_date"), day)
    if int(state.get("volume", 0) or 0) and volume != int(state["volume"]):
        state["issue"] = 0
    state["volume"] = volume
    state["issue"] = int(state.get("issue", 0)) + 1
    state["issues_total"] = int(state.get("issues_total", 0) or state["issue"] - 1) + 1
    save_state(state)
    return state["issue"]


def volume_number(today: "_date | None" = None) -> int:
    """1 in the first year of publication, 2 in the second, and so on.

    Before the first issue is printed the paper is in volume 1. Counts
    whole years from `first_issue_date`, anniversary to anniversary.
    """
    return _volume_for(load_state().get("first_issue_date"), today or _date.today())


def _volume_for(first, today: _date) -> int:
    if not first:
        return 1
    try:
        start = _date.fromisoformat(str(first)[:10])
    except ValueError:
        return 1
    years = today.year - start.year - (1 if (today.month, today.day) < (start.month, start.day) else 0)
    return max(1, years + 1)


def roman(n: int) -> str:
    """1 -> I, 4 -> IV, 12 -> XII. Good for as many volumes as a life holds."""
    n = max(1, int(n))
    out = ""
    for value, glyph in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                         (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out += glyph
            n -= value
    return out
