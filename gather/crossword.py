"""
Crossword gatherer: the day's New York Times puzzle as data — the grid's
black squares, its clue numbers, and the clues themselves — for the
template to typeset on the paper.

The owner's decision (PLAN.md, "crossword.py"): read the puzzle with the
subscriber's own ``NYT-S`` session cookie, copied out of a logged-in
browser and set as the container variable ``NYT_S``. No password is stored
and no login is scripted. This is automated access to a subscription the
Times' terms discourage; the owner of the paper accepts that for one
household copy.

What comes back from ``fetch``::

    {"provider": "nyt", "date": "2026-09-17",
     "title": str | None, "author": str, "editor": str | None,
     "width": 15, "height": 15,
     "grid": [[None | {"n": int | None}, ...], ...],   # None = black square
     "across": [{"n": 1, "clue": "..."}, ...],
     "down":   [{"n": 1, "clue": "..."}, ...]}

**No solution letters, ever.** The answers are in the response and they are
dropped here, so they are never written to ``data.json`` and can never
reach the page. Clues are the Times' words, unchanged: entities are
unescaped and italic tags flattened to text, and nothing else is touched
(house rule 1).

``fetch`` never raises: a bad cookie, a missing puzzle or a slow morning
means no puzzle today and a log line, never a paper that did not print.
``check`` is the same work with the errors raised instead, for the Sources
tab's Check button — the reader's first live test of the cookie, so every
message has to be specific enough to act on.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from app.settings import Env, Settings

log = logging.getLogger(__name__)

# -- endpoints -------------------------------------------------------------
# Verified against xword-dl's New York Times downloader
# (src/xword_dl/downloader/newyorktimesdownloader.py), which reads this same
# v6 endpoint with this same NYT-S cookie. A URL change is a one-line fix.

#: The puzzle for a print date: metadata, cells and clues.
META_URL = "https://www.nytimes.com/svc/crosswords/v6/puzzle/daily/{date}.json"
#: The older endpoint, used when v6 does not answer.
META_URL_FALLBACK = "https://www.nytimes.com/svc/crosswords/v3/puzzle/daily-{date}.json"

#: The cookie the browser holds after logging in at nytimes.com.
COOKIE_NAME = "NYT-S"
#: A browser User-Agent: the svc endpoints answer a bare client with HTML.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20  # seconds, every request

ACROSS, DOWN = "Across", "Down"
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TAGS = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------ helpers
def _today() -> dt.date:
    try:
        return dt.datetime.now(ZoneInfo(Env.tz())).date()
    except Exception:  # an unknown TZ must not stop the puzzle
        return dt.date.today()


def data_dir() -> Path:
    """DATA_DIR, read fresh (tests and the web app move it)."""
    import state as state_file

    return state_file.data_dir()


def _cookie() -> str:
    """The NYT-S cookie, or "" when the container variable is not set."""
    return (Env.get(Env.NYT_S) or "").strip()


def _headers(cookie: str) -> dict[str, str]:
    return {
        "Cookie": f"{COOKIE_NAME}={cookie}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }


def _looks_like_html(body: bytes, content_type: str) -> bool:
    head = body[:512].lstrip().lower()
    return "html" in content_type.lower() or head.startswith((b"<!doctype", b"<html"))


def _http_error(response: requests.Response, url: str, day: dt.date) -> RuntimeError:
    """The one sentence that says what to do about this response."""
    code = response.status_code
    if code in (401, 403):
        return RuntimeError(
            f"{COOKIE_NAME} cookie expired or invalid (HTTP {code} from {url}). "
            f"Copy a fresh {COOKIE_NAME} cookie out of a logged-in browser."
        )
    if code == 404:
        return RuntimeError(f"no puzzle for {day} at {url}")
    return RuntimeError(f"HTTP {code} from {url}")


def _get(url: str, cookie: str) -> requests.Response:
    try:
        return requests.get(url, headers=_headers(cookie), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"could not reach {url}: {type(exc).__name__}: {exc}") from exc


def _json(response: requests.Response, url: str, day: dt.date) -> Any:
    """The body as JSON, or the specific reason it is not."""
    content_type = response.headers.get("content-type", "")
    if _looks_like_html(response.content, content_type):
        raise RuntimeError(f"{COOKIE_NAME} cookie rejected (got a login page) from {url}")
    if response.status_code != 200:
        raise _http_error(response, url, day)
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{url} did not answer with JSON ({exc})") from exc


def download(day: dt.date, cookie: str) -> Any:
    """The raw puzzle JSON for `day`, from v6 or, failing that, v3."""
    first = META_URL.format(date=day.isoformat())
    response = _get(first, cookie)
    if response.status_code == 200:
        return _json(response, first, day)
    if _looks_like_html(response.content, response.headers.get("content-type", "")):
        raise RuntimeError(f"{COOKIE_NAME} cookie rejected (got a login page) from {first}")
    if response.status_code in (401, 403):
        raise _http_error(response, first, day)

    second = META_URL_FALLBACK.format(date=day.isoformat())
    log.info("crossword: %s answered HTTP %s; trying %s", first, response.status_code, second)
    return _json(_get(second, cookie), second, day)


# ------------------------------------------------------------------ parsing
def flatten(text: str) -> str:
    """A clue as plain text: tags dropped, entities unescaped, words kept.

    Clues arrive with the Times' own markup — ``<i>`` around a cited title,
    ``&quot;`` around a quotation. The paper prints in one face, so the tags
    go and the words stay exactly as they were written.
    """
    return html.unescape(_TAGS.sub("", text or "")).strip()


def _clue_text(item: dict) -> str:
    parts: list[str] = []
    for segment in item.get("text") or []:
        if isinstance(segment, str):
            parts.append(segment)
        elif isinstance(segment, dict):
            parts.append(str(segment.get("plain") or segment.get("formatted") or ""))
    if not parts:  # v3 spells it as a bare string
        parts.append(str(item.get("clue") or ""))
    return flatten("".join(parts))


def _label(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def numbering(black: list[list[bool]]) -> list[list[Optional[int]]]:
    """Clue numbers from the black squares alone, by the standard rule.

    A square is numbered when it starts an across entry (nothing to its
    left, something to its right) or a down entry (nothing above, something
    below), counting left to right, top to bottom.
    """
    height = len(black)
    width = len(black[0]) if height else 0
    numbers: list[list[Optional[int]]] = []
    n = 0
    for r in range(height):
        row: list[Optional[int]] = []
        for c in range(width):
            if black[r][c]:
                row.append(None)
                continue
            across = (c == 0 or black[r][c - 1]) and (c + 1 < width and not black[r][c + 1])
            down = (r == 0 or black[r - 1][c]) and (r + 1 < height and not black[r + 1][c])
            if across or down:
                n += 1
                row.append(n)
            else:
                row.append(None)
        numbers.append(row)
    return numbers


def _puzzle_body(payload: Any) -> dict:
    data = payload if isinstance(payload, dict) else {}
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        data = results[0]
    elif isinstance(results, dict):
        data = results
    return data


def parse(payload: Any, day: dt.date) -> dict[str, Any]:
    """The render contract's `crossword` object, from a v6 (or v3) response.

    Raises when the response is not a puzzle this can lay out.
    """
    data = _puzzle_body(payload)
    bodies = data.get("body")
    body = bodies[0] if isinstance(bodies, list) and bodies else data.get("puzzle_meta") or {}
    if not isinstance(body, dict):
        body = {}

    dimensions = body.get("dimensions") or {}
    cells = body.get("cells")
    if not isinstance(cells, list) or not cells:
        raise RuntimeError(f"the puzzle for {day} has no grid in it")
    width = int(dimensions.get("width") or 0)
    height = int(dimensions.get("height") or 0)
    if width <= 0 or height <= 0 or width * height != len(cells):
        raise RuntimeError(
            f"the puzzle for {day} is {width}x{height} but has {len(cells)} cells"
        )

    # A black square is an empty object; everything else is a letter the
    # paper never prints, so only the clue number is kept.
    black = [
        [not (isinstance(cells[r * width + c], dict) and cells[r * width + c]) for c in range(width)]
        for r in range(height)
    ]
    computed = numbering(black)
    grid: list[list[Optional[dict]]] = []
    for r in range(height):
        row: list[Optional[dict]] = []
        for c in range(width):
            if black[r][c]:
                row.append(None)
                continue
            cell = cells[r * width + c]
            row.append({"n": _label(cell.get("label")) or computed[r][c]})
        grid.append(row)

    across: list[dict[str, Any]] = []
    down: list[dict[str, Any]] = []
    for item in body.get("clues") or []:
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").strip().lower()
        entry = {"n": _label(item.get("label")), "clue": _clue_text(item)}
        if direction.startswith("a"):
            across.append(entry)
        elif direction.startswith("d"):
            down.append(entry)
    if not (across or down):
        raise RuntimeError(f"the puzzle for {day} has no clues in it")
    across.sort(key=lambda e: (e["n"] is None, e["n"] or 0))
    down.sort(key=lambda e: (e["n"] is None, e["n"] or 0))

    constructors = data.get("constructors")
    if isinstance(constructors, list) and constructors:
        author = ", ".join(str(c) for c in constructors if c)
    else:
        author = str(data.get("author") or "")
    printed = (
        data.get("printDate") or data.get("print_date") or data.get("publicationDate") or ""
    )
    return {
        "provider": "nyt",
        "date": str(printed) or day.isoformat(),
        "title": str(data["title"]) if data.get("title") else None,
        "author": author,
        "editor": str(data["editor"]) if data.get("editor") else None,
        "width": width,
        "height": height,
        "grid": grid,
        "across": across,
        "down": down,
    }


def summary(puzzle: dict[str, Any]) -> str:
    """The one line the status strip shows."""
    title = puzzle.get("title") or "Daily crossword"
    author = f" by {puzzle['author']}" if puzzle.get("author") else ""
    clues = len(puzzle.get("across") or []) + len(puzzle.get("down") or [])
    return (
        f"{title}{author}, {puzzle.get('date')} "
        f"({puzzle.get('width')}x{puzzle.get('height')}, {clues} clues)"
    )


def record_check(ok: bool, text: str) -> None:
    """Remember the last check for the status strip, the way the printer does."""
    try:
        import state as state_file

        state_file.update_state(
            crossword_check={
                "ok": bool(ok),
                "when": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "summary": text,
            }
        )
    except Exception:  # noqa: BLE001 - the strip is never worth an exception
        log.warning("could not record the crossword check", exc_info=True)


def _keep_raw(day: dt.date, payload: Any) -> None:
    """Save the response beside the day's data, for debugging a bad grid."""
    try:
        out = data_dir() / "out" / day.isoformat()
        out.mkdir(parents=True, exist_ok=True)
        (out / "crossword.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )
    except Exception as exc:  # noqa: BLE001 - debugging aid, not the paper
        log.warning("crossword: could not write crossword.json (%s)", exc)


# ------------------------------------------------------------------- fetch
def fetch(settings, *, today: Optional[dt.date] = None) -> Optional[dict[str, Any]]:
    """Today's puzzle for the render contract, or None.

    None means "no puzzle this morning" for every reason there is: the
    source is off, today is not one of its days, the cookie is not set, or
    the fetch failed. Never raises (house rule 3).
    """
    source = getattr(getattr(settings, "sources", None), "crossword", None)
    if source is None or not source.enabled:
        log.info("crossword: not enabled")
        return None

    day = today or _today()
    if day.weekday() not in list(source.days or []):
        log.info("crossword: %s is off (no puzzle on %s)", day, DAY_NAMES[day.weekday()])
        return None

    cookie = _cookie()
    if not cookie:
        log.warning(
            "crossword: %s is not set in the container, so there is no puzzle today",
            Env.NYT_S,
        )
        return None

    try:
        payload = download(day, cookie)
        _keep_raw(day, payload)
        puzzle = parse(payload, day)
    except Exception as exc:  # noqa: BLE001 - house rule 3: no puzzle, but a paper
        log.error("crossword: %s", exc)
        record_check(False, str(exc))
        return None

    log.info("crossword: %s", summary(puzzle))
    record_check(True, summary(puzzle))
    return puzzle


# ------------------------------------------------------------------- check
def check(today: Optional[dt.date] = None) -> dict[str, Any]:
    """The Sources tab's Check button: fetch and parse today's puzzle.

    Raises on failure, with the message the reader needs to fix it — a
    missing cookie, an expired one, or a day with no puzzle.
    """
    day = today or _today()
    cookie = _cookie()
    if not cookie:
        error = f"{Env.NYT_S} is not set in the container"
        record_check(False, error)
        raise RuntimeError(error)

    try:
        puzzle = parse(download(day, cookie), day)
    except Exception as exc:
        record_check(False, str(exc))
        raise

    record_check(True, summary(puzzle))
    return {
        "ok": True,
        "date": puzzle["date"],
        "title": puzzle["title"] or "Daily crossword",
        "author": puzzle["author"],
        "editor": puzzle["editor"] or "",
        "size": f"{puzzle['width']}x{puzzle['height']}",
        "clues": len(puzzle["across"]) + len(puzzle["down"]),
        "error": "",
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    Settings.load()  # so a broken settings file shows up here too
    try:
        print(json.dumps(check(), indent=2, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        raise SystemExit(1) from None
