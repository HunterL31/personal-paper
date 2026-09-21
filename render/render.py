#!/usr/bin/env python3
"""
Render today's paper.

    python render/render.py data.json                      # -> out/paper.html, out/paper.pdf
    python render/render.py data.json --png                # also out/page-1.png, page-2.png
    python render/render.py data.json --look settings.json # use the Look from a settings file
    python render/render.py data.json --print brother

The data file is whatever the gather step produced (see sample_data.json for
the shape). Article paragraphs must be the author's text, untouched.

The paper is one double-sided sheet: page 1, and page 2 on its back with the
continuations and the crossword -- printed one-sided on a morning with no
articles, when page 2 has nothing to carry and the crossword takes the front.
The sheet is filled: the articles that fit whole are printed whole, and the
next one may be printed as far as its last whole paragraph that fits, ended
by a line saying where the rest is. `RenderResult.printed` says which
articles are on the sheet and `RenderResult.partial` how much of the partial
one was printed.

As a library:

    from render.render import render
    result = render(data, settings.look, out_dir)   # -> RenderResult
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:                                    # imported as a package
    from .fontlist import FONT_DIR, FONT_FILES, STACKS
except ImportError:                     # run as a script: python render/render.py
    from fontlist import FONT_DIR, FONT_FILES, STACKS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.settings import Look

HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)

#: Fallback defaults, used when `app.settings` is unavailable and to fill in
#: keys a partial look dict leaves out.  Kept in sync with `app.settings.Look`.
_FALLBACK_LOOK: dict[str, Any] = {
    "paper_name": "Personal Paper",
    "imprint": "Printed at home before sunrise",
    "price": "Single copy, free",
    "ear_left": {"kind": "weather", "initials": "", "lines": [],
                 "countdown_date": "", "countdown_label": ""},
    "ear_right": {"kind": "monogram", "initials": "", "lines": ["Continued stories inside."],
                  "countdown_date": "", "countdown_label": ""},
    "date_place": "folio",
    "date_font": "Old Standard",
    "date_size_pt": 8.0,
    "masthead_font": "Maguntia",
    "headline_font": "Old Standard",
    "body_font": "PT Serif",
    "body_size_pt": 9.0,
    "lead_body_height_in": 2.7,
    "justify": True,
    "show_todo": True,
    "show_hourly": True,
    "show_notes": True,
    "layout": {
        "sections": [
            {"key": "agenda", "place": "rail"},
            {"key": "list:tasks", "place": "rail"},
            {"key": "hourly", "place": "rail"},
            {"key": "notes", "place": "rail"},
        ],
        "rail_side": "right",
        "rail_width_in": 1.9,
        "front_stories": 4,
        "crossword_place": "bottom",
        "crossword_cell_in": 0.19,
        "crossword_max_pct": 55,
        "pictures": False,
    },
}


# ------------------------------------------------------------- hyphenation
#: Headless Chromium on Linux ships no hyphenation dictionary, so `hyphens:
#: auto` does nothing and justified columns open wide word gaps. We mark the
#: break points ourselves, at render time, with soft hyphens: they are
#: typesetting marks, invisible unless a line actually breaks there, and they
#: never reach the saved article data.
SOFT_HYPHEN = "\u00ad"
#: Shorter words are left alone; breaking them saves nothing and reads badly.
MIN_HYPHENATED = 6
#: Anything with a digit, a URL's punctuation, an existing hyphen or an
#: abbreviation's inner dot is left exactly as it is.
_NEVER = re.compile(r"[\d/:@_\u00ad-]|[^\W\d_]\.[^\W\d_]")
#: The first run of letters in a token, which is the word to break.
_WORD = re.compile(r"[^\W\d_]+")


@functools.lru_cache(maxsize=1)
def _hyphenator():
    import pyphen

    return pyphen.Pyphen(lang="en_US", left=3, right=3)


def _mark_token(dic, token: str) -> str:
    if _NEVER.search(token):
        return token
    word = _WORD.search(token)
    if word is None or len(word.group()) < MIN_HYPHENATED:
        return token
    return token[: word.start()] + dic.inserted(word.group(), hyphen=SOFT_HYPHEN) + token[word.end():]


def hyphenate(text: Any) -> Any:
    """Mark where the typesetter may break a word. Not one letter changes.

    Used as the `hyphenate` Jinja filter on article prose only: never on a
    title, a byline or a crossword clue, and never on the data itself.
    """
    if not text or not isinstance(text, str):
        return text
    try:
        dic = _hyphenator()
    except Exception as exc:  # pragma: no cover - pyphen missing or no en_US
        log.warning("hyphenation unavailable, setting without it: %s", exc)
        return text
    return "".join(
        part if (not part or part.isspace()) else _mark_token(dic, part)
        for part in re.split(r"(\s+)", text)
    )


# ------------------------------------------------------------------- ears
#: A clock time as the calendar gatherer writes one: "7:30", "9:30 a.m.".
#: An event whose time is not one of these is an all-day event.
_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(?:([ap])\.?\s*m\.?)?\s*$", re.I)


def clock_minutes(text: Any) -> Optional[int]:
    """"6:53 a.m." -> 413, minutes past midnight. None if it is not a clock."""
    if not isinstance(text, str):
        return None
    m = _CLOCK.match(text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if minute > 59 or hour > (12 if half else 23):
        return None
    if half:
        hour = hour % 12 + (12 if half == "p" else 0)
    return hour * 60 + minute


def first_clock_event(events: Any) -> Optional[dict]:
    """The first event of the day that happens at a time, not all day."""
    for event in events or []:
        if isinstance(event, dict) and clock_minutes(event.get("time")) is not None:
            return event
    return None


def day_length(sunrise: Any, sunset: Any) -> str:
    """"6:53 a.m.", "7:15 p.m." -> "12h 22m". "" if either is not a clock."""
    up, down = clock_minutes(sunrise), clock_minutes(sunset)
    if up is None or down is None:
        return ""
    minutes = (down - up) % (24 * 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def countdown(iso: Any, today: Any = None) -> str:
    """"12 days" until that date, "Today" on it, "12 days since" after it."""
    from datetime import date as _date

    try:
        year, month, day = (int(part) for part in str(iso).split("-"))
        target = _date(year, month, day)
    except (TypeError, ValueError):
        return ""
    days = (target - (today or _date.today())).days
    if days == 0:
        return "Today"
    n = abs(days)
    return f"{n} day{'' if n == 1 else 's'}" + ("" if days > 0 else " since")


def DEFAULT_LOOK() -> dict[str, Any]:
    """The default Look as a plain dict (from `app.settings` when importable).

    The fallback underneath it is what keeps a Look that predates a key --
    or one that never had it -- from leaving the template without it.
    """
    base = json.loads(json.dumps(_FALLBACK_LOOK))
    try:
        from app.settings import Look

        base.update(Look().model_dump())
    except Exception:  # pragma: no cover - app/ missing or broken
        pass
    return base


@dataclass
class RenderResult:
    pdf: Path
    html: Path
    #: 2, the front of the sheet and its back -- or 1 on a morning with no
    #: articles to print, when there is nothing to continue onto page 2 and
    #: the paper is the front alone.  `pages == 1` exactly when `printed`
    #: is empty.  With the picture sheet on (`look.layout.pictures`) and a
    #: printed story that has a picture, `picture_pages` (1 or 2) more.
    pages: int
    #: Indices into `data["articles"]` of the articles that were printed, in
    #: the order they were given: the whole ones and, last, the partial one
    #: if there is one.  The others are nowhere on the sheet.
    printed: list[int] = field(default_factory=list)
    #: `{index: paragraphs printed}` for the one article that was printed
    #: partially, or `{}` when every printed article was printed whole.  Its
    #: index is the last of `printed`; the paragraphs printed are the first
    #: `n` of the author's, unchanged, followed by a line saying where the
    #: rest of the story is.
    partial: dict[int, int] = field(default_factory=dict)
    #: `{section key: rows}` for the reader's own sections that ran on into
    #: page 2's column, and for the rows no column had room for. The keys are
    #: `look.layout.sections` keys (`agenda`, `list:tasks`, ...). A key in
    #: `rail_dropped` with 0 rows is a section that has no rows to count --
    #: the notes block -- and was left off whole.
    rail_continued: dict[str, int] = field(default_factory=dict)
    rail_dropped: dict[str, int] = field(default_factory=dict)
    #: The picture sheet: how many pages it took (0 when it was not asked
    #: for, or no printed story had a picture), which pictures are on it as
    #: `(article index, picture index)` in the order they are numbered, and
    #: which pictures of the printed stories it could not hold. A picture is
    #: never on the sheet without its line in the story, nor the line
    #: without the picture.
    picture_pages: int = 0
    pictures: list[tuple[int, int]] = field(default_factory=list)
    pictures_dropped: list[tuple[int, int]] = field(default_factory=list)
    pngs: list[Path] = field(default_factory=list)
    #: `document.documentElement.outerHTML` after the fitting script ran, i.e.
    #: the pages as they were printed.  The verbatim test parses this.
    laid_out_html: str = ""


def _look_dict(look: "Look | dict | None") -> dict[str, Any]:
    """Normalise whatever the caller passed into a full look dict.

    The ears and `layout` are merged key by key, so a caller may hand over
    one layout setting, or one ear, and still get a whole arrangement. A
    look from before either ear could be set carries one `ear`: it is the
    right-hand monogram, exactly as `app.settings.Look` reads it.
    """
    base = DEFAULT_LOOK()
    if look is None:
        return base
    raw = look.model_dump() if hasattr(look, "model_dump") else dict(look)
    merged = {**base, **{k: v for k, v in raw.items() if v is not None}}

    def _part(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        return dict(value or {})

    legacy = _part(raw.get("ear"))
    if legacy and not raw.get("ear_right"):
        merged["ear_right"] = {**base["ear_right"], "kind": "monogram",
                               **{k: v for k, v in legacy.items() if v is not None}}
    merged.pop("ear", None)
    for side in ("ear_left", "ear_right"):
        given = _part(raw.get(side))
        if given:
            merged[side] = {**base[side], **{k: v for k, v in given.items() if v is not None}}
    merged["layout"] = {**base["layout"],
                        **{k: v for k, v in _part(raw.get("layout")).items() if v is not None}}
    return merged


def picture_key(key: Any) -> tuple[int, int]:
    """"0:2" -> (0, 2): the article's index and the picture's within it."""
    a, _, i = str(key).partition(":")
    return int(a), int(i)


def pictures_for(articles: list, look: dict[str, Any], image_dir: Path | str | None) -> list[dict[str, Any]]:
    """The pictures the template may set, one per fetched picture of every
    article, in article order -- or none at all when the reader has not
    switched the picture sheet on.

    Each is `{key, article, after, src, caption, title}`; `key` is
    `"<article>:<picture>"`. A picture's `file` is relative to `image_dir`
    (the folder its data.json is in; the `render/` folder for the sample
    issue) unless it is absolute. One whose file is not there is left out
    with a warning, and the story then carries no line for it: a replay of
    a day whose pictures were cleaned up is still a paper.
    """
    if not (look.get("layout") or {}).get("pictures"):
        return []
    base = Path(image_dir) if image_dir is not None else HERE
    out: list[dict[str, Any]] = []
    for a_index, article in enumerate(articles or []):
        if not isinstance(article, dict):
            continue
        for i_index, image in enumerate(article.get("images") or []):
            file = image.get("file") if isinstance(image, dict) else None
            if not file:
                continue
            path = Path(str(file))
            if not path.is_absolute():
                path = base / path
            if not path.is_file():
                log.warning("picture %d of article %d is not on disk (%s); not printed",
                            i_index + 1, a_index, path)
                continue
            try:
                after = max(0, int(image.get("after") or 0))
            except (TypeError, ValueError):
                after = 0
            out.append({
                "key": f"{a_index}:{i_index}",
                "article": a_index,
                "after": after,
                "src": path.resolve().as_uri(),
                "caption": str(image.get("caption") or ""),
                "title": str(article.get("title") or ""),
            })
    return out


def build_html(data: dict, look: "Look | dict | None" = None, *,
               font_dir: str | None = None, image_dir: Path | str | None = None) -> str:
    """Render the Jinja2 template (no browser involved)."""
    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html"]))
    env.filters["hyphenate"] = hyphenate
    # What the ears are set from, where the data alone is not the words:
    # the next thing on today, how long the day is, how far off a date is.
    env.filters["first_clock_event"] = first_clock_event
    env.filters["day_length"] = day_length
    env.filters["countdown"] = countdown
    ctx = dict(data)
    ctx.setdefault("paper", {})
    ctx.setdefault("weather", {})
    ctx.setdefault("events", [])
    ctx.setdefault("tasks", [])
    ctx.setdefault("articles", [])
    ctx.setdefault("crossword", None)
    # A data.json from before the reader could keep several lists has one,
    # under `tasks`. It is the list the default layout calls "To do".
    if not ctx.get("lists") and ctx.get("tasks"):
        ctx["lists"] = [{"name": "To do", "slug": "tasks", "style": "checkbox",
                         "items": list(ctx["tasks"])}]
    ctx.setdefault("lists", [])
    full_look = _look_dict(look)
    ctx["pictures"] = pictures_for(ctx["articles"], full_look, image_dir)
    return env.get_template("template.html").render(
        font_dir=font_dir if font_dir is not None else FONT_DIR.as_uri(),
        # The faces, from render/fontlist.py: `font_faces` is what the
        # @font-face block is built from, `FONTS` the stack each family is
        # set in. The Look tab's picker is served the same table.
        font_faces=FONT_FILES,
        FONTS=STACKS,
        look=full_look,
        **ctx,
    )


def _launch(pw):
    exe = os.environ.get("CHROMIUM_PATH") or None
    kwargs = {"executable_path": exe} if exe else {}
    return pw.chromium.launch(**kwargs)


def _rasterize(pdf_path: Path, out_dir: Path, dpi: int = 70) -> list[Path]:
    import pymupdf

    pngs: list[Path] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            p = out_dir / f"page-{i}.png"
            pix.save(p)
            pngs.append(p)
    return pngs


def render(
    data: dict,
    look: "Look | dict | None" = None,
    out_dir: Path | str = HERE / "out",
    *,
    png: bool = False,
    browser: Any = None,
    image_dir: Path | str | None = None,
) -> RenderResult:
    """Lay the paper out in Chromium and write `paper.html` and `paper.pdf`.

    `look` is an `app.settings.Look` (or a plain dict, or None for defaults).
    `browser` lets a caller reuse one Playwright browser across renders; when
    it is None a browser is launched and closed for this render. `image_dir`
    is the folder a picture's relative `file` is under (see `pictures_for`);
    the `render/` folder, where the sample issue's are, when it is None.

    The paper is two pages, or one when no article reached the sheet, with
    the picture sheet's page or two after them when the reader asked for it;
    anything else is a bug in the template, not a layout the caller could
    recover from, so it raises.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    html = build_html(data, look, image_dir=image_dir)
    html_path = out / "paper.html"
    html_path.write_text(html)
    pdf_path = out / "paper.pdf"

    def _do(br) -> tuple[int, list[int], dict[int, int], dict[str, dict[str, int]], dict[str, Any], str]:
        page = br.new_page()
        try:
            page.emulate_media(media="print")     # measure in the same mode we print in
            page.goto(html_path.as_uri())
            page.wait_for_function("window.__layoutDone === true")
            n = page.evaluate("window.__pages")
            printed = page.evaluate("window.__printed")
            partial = page.evaluate("window.__partial") or {}
            rail = page.evaluate("window.__rail") or {}
            pics = page.evaluate("window.__pictures") or {}
            laid_out = page.evaluate("document.documentElement.outerHTML")
            page.pdf(path=str(pdf_path), prefer_css_page_size=True, print_background=True)
            return (int(n), [int(i) for i in printed],
                    {int(k): int(v) for k, v in dict(partial).items()},
                    {part: {str(k): int(v) for k, v in dict(rail.get(part) or {}).items()}
                     for part in ("continued", "dropped")},
                    {"pages": int(pics.get("pages") or 0),
                     "printed": [picture_key(k) for k in (pics.get("printed") or [])],
                     "dropped": [picture_key(k) for k in (pics.get("dropped") or [])]},
                    laid_out)
        finally:
            page.close()

    if browser is not None:
        pages, printed, partial, rail, pics, laid_out_html = _do(browser)
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            br = _launch(p)
            try:
                pages, printed, partial, rail, pics, laid_out_html = _do(br)
            finally:
                br.close()

    # The sheet, then the picture sheet: nothing else is ever laid out.
    sheet = pages - pics["pages"]
    if sheet not in (1, 2) or pics["pages"] > 2:
        raise AssertionError(f"the paper is one sheet and at most one more of pictures: "
                             f"expected 1 or 2 pages plus 0-2, laid out {pages} "
                             f"({pics['pages']} of pictures)")
    if bool(pics["pages"]) != bool(pics["printed"]):
        raise AssertionError(f"{pics['pages']} picture page(s) with {len(pics['printed'])} picture(s) on them")
    # One page is the morning with nothing queued, and only that: a page 2
    # missing from a paper that has stories on it would lose their
    # continuations, which is exactly what must never happen silently.
    if (sheet == 1) != (not printed):
        raise AssertionError(
            f"a one-page paper is a morning with no articles: laid out {sheet} page(s) "
            f"with {len(printed)} article(s) printed"
        )

    pngs = _rasterize(pdf_path, out) if png else []
    given = len(data.get("articles") or [])
    log.info("rendered %s page(s) -> %s (%s of %s article(s) printed%s)", pages, pdf_path, len(printed), given,
             "".join(f", article {i} partial: {n} paragraph(s)" for i, n in partial.items()))
    # Her own sections are the one part of the sheet she cannot see the rest
    # of online, so what a column could not hold is said out loud.
    if rail["continued"]:
        log.info("rail continued on page 2: %s",
                 ", ".join(f"{k} ({n} row(s))" for k, n in rail["continued"].items()))
    if rail["dropped"]:
        log.warning("rail did not fit the sheet: %s",
                    ", ".join(f"{k} ({n} row(s))" for k, n in rail["dropped"].items()))
    if pics["printed"]:
        log.info("picture sheet: %d picture(s) on %d page(s)", len(pics["printed"]), pics["pages"])
    if pics["dropped"]:
        log.warning("picture sheet had no room for %d picture(s): %s", len(pics["dropped"]),
                    ", ".join(f"article {a} picture {i + 1}" for a, i in pics["dropped"]))
    return RenderResult(pdf=pdf_path, html=html_path, pages=pages, printed=printed,
                        partial=partial, rail_continued=rail["continued"],
                        rail_dropped=rail["dropped"], picture_pages=pics["pages"],
                        pictures=pics["printed"], pictures_dropped=pics["dropped"],
                        pngs=pngs, laid_out_html=laid_out_html)


def _look_from_settings_file(path: str) -> Any:
    from app.settings import Settings

    return Settings.model_validate_json(Path(path).read_text()).look


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", default=str(HERE / "sample_data.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--look", metavar="SETTINGS_JSON", help="settings.json whose .look is used")
    ap.add_argument("--print", dest="printer", metavar="QUEUE", help="CUPS queue to print to, e.g. brother")
    ap.add_argument("--png", action="store_true", help="rasterize pages for review")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    data = json.loads(Path(args.data).read_text())
    look = _look_from_settings_file(args.look) if args.look else None

    result = render(data, look, Path(args.out), png=args.png)
    articles = data.get("articles") or []
    print(f"{result.pages} page(s) -> {result.pdf}")
    print(f"  {len(result.printed)} of {len(articles)} article(s) printed")
    for i in result.printed:
        part = result.partial.get(i)
        of = f"  ({part} of {len(articles[i]['paragraphs'])} paragraphs, rest online)" if part else ""
        print(f"    [{i}] {articles[i]['title']}{of}")
    if result.picture_pages:
        print(f"  {len(result.pictures)} picture(s) on {result.picture_pages} picture page(s)"
              + (f", {len(result.pictures_dropped)} left off" if result.pictures_dropped else ""))
    for p in result.pngs:
        print(f"  {p}")

    if args.printer:
        # One page is the morning with no articles: not a duplex job.
        sides = "two-sided-long-edge" if result.pages > 1 else "one-sided"
        subprocess.run(
            ["lp", "-d", args.printer, "-o", "media=Letter", "-o", f"sides={sides}", str(result.pdf)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    # Run as a script the repo root is not on sys.path; app.settings is
    # wanted for --look and for the Look defaults.
    sys.path.insert(0, str(HERE.parent))
    sys.exit(main())
