#!/usr/bin/env python3
"""
Render today's paper.

    python render/render.py data.json                      # -> out/paper.html, out/paper.pdf
    python render/render.py data.json --png                # also out/page-1.png, page-2.png ...
    python render/render.py data.json --look settings.json # use the Look from a settings file
    python render/render.py data.json --crossword nyt.pdf --print brother

The data file is whatever the gather step produced (see sample_data.json for
the shape). Article paragraphs must be the author's text, untouched.

As a library:

    from render.render import render
    result = render(data, settings.look, out_dir)   # -> RenderResult
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.settings import Look

HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"
log = logging.getLogger(__name__)

#: Fallback defaults, used when `app.settings` is unavailable and to fill in
#: keys a partial look dict leaves out.  Kept in sync with `app.settings.Look`.
_FALLBACK_LOOK: dict[str, Any] = {
    "paper_name": "Personal Paper",
    "imprint": "Printed at home before sunrise",
    "price": "Single copy, free",
    "ear": {"initials": "", "lines": ["Continued stories inside."]},
    "masthead_font": "Maguntia",
    "headline_font": "Old Standard",
    "body_font": "PT Serif",
    "body_size_pt": 9.0,
    "lead_body_height_in": 2.7,
    "show_todo": True,
    "show_hourly": True,
    "show_notes": True,
}


def DEFAULT_LOOK() -> dict[str, Any]:
    """The default Look as a plain dict (from `app.settings` when importable)."""
    try:
        from app.settings import Look

        return Look().model_dump()
    except Exception:  # pragma: no cover - app/ missing or broken
        return json.loads(json.dumps(_FALLBACK_LOOK))


@dataclass
class RenderResult:
    pdf: Path
    html: Path
    pages: int
    pngs: list[Path] = field(default_factory=list)
    #: `document.documentElement.outerHTML` after the fitting script ran, i.e.
    #: the pages as they were printed.  The verbatim test parses this.
    laid_out_html: str = ""


def _look_dict(look: "Look | dict | None") -> dict[str, Any]:
    """Normalise whatever the caller passed into a full look dict."""
    base = DEFAULT_LOOK()
    if look is None:
        return base
    raw = look.model_dump() if hasattr(look, "model_dump") else dict(look)
    merged = {**base, **{k: v for k, v in raw.items() if v is not None}}
    ear = raw.get("ear") or {}
    if hasattr(ear, "model_dump"):
        ear = ear.model_dump()
    merged["ear"] = {**base["ear"], **{k: v for k, v in dict(ear).items() if v is not None}}
    return merged


def build_html(data: dict, look: "Look | dict | None" = None, *, font_dir: str | None = None) -> str:
    """Render the Jinja2 template (no browser involved)."""
    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html"]))
    ctx = dict(data)
    ctx.setdefault("paper", {})
    ctx.setdefault("weather", {})
    ctx.setdefault("events", [])
    ctx.setdefault("tasks", [])
    ctx.setdefault("articles", [])
    return env.get_template("template.html").render(
        font_dir=font_dir if font_dir is not None else FONT_DIR.as_uri(),
        look=_look_dict(look),
        **ctx,
    )


def _launch(pw):
    exe = os.environ.get("CHROMIUM_PATH") or None
    kwargs = {"executable_path": exe} if exe else {}
    return pw.chromium.launch(**kwargs)


def _merge_crossword(pdf_path: Path, crossword: Path, out_path: Path) -> Path:
    import pymupdf

    with pymupdf.open(pdf_path) as doc, pymupdf.open(crossword) as extra:
        doc.insert_pdf(extra)
        doc.save(out_path)
    return out_path


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
    crossword: Path | str | None = None,
    browser: Any = None,
) -> RenderResult:
    """Lay the paper out in Chromium and write `paper.html` and `paper.pdf`.

    `look` is an `app.settings.Look` (or a plain dict, or None for defaults).
    `browser` lets a caller reuse one Playwright browser across renders; when
    it is None a browser is launched and closed for this render.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    html = build_html(data, look)
    html_path = out / "paper.html"
    html_path.write_text(html)
    pdf_path = out / "paper.pdf"

    def _do(br) -> tuple[int, str]:
        page = br.new_page()
        try:
            page.emulate_media(media="print")     # measure in the same mode we print in
            page.goto(html_path.as_uri())
            page.wait_for_function("window.__layoutDone === true")
            n = page.evaluate("window.__pages")
            laid_out = page.evaluate("document.documentElement.outerHTML")
            page.pdf(path=str(pdf_path), prefer_css_page_size=True, print_background=True)
            return int(n), laid_out
        finally:
            page.close()

    if browser is not None:
        pages, laid_out_html = _do(browser)
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            br = _launch(p)
            try:
                pages, laid_out_html = _do(br)
            finally:
                br.close()

    if crossword:
        pdf_path = _merge_crossword(pdf_path, Path(crossword), out / "paper_with_crossword.pdf")

    pngs = _rasterize(pdf_path, out) if png else []
    log.info("rendered %s page(s) -> %s", pages, pdf_path)
    return RenderResult(pdf=pdf_path, html=html_path, pages=pages, pngs=pngs, laid_out_html=laid_out_html)


def _look_from_settings_file(path: str) -> Any:
    from app.settings import Settings

    return Settings.model_validate_json(Path(path).read_text()).look


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", default=str(HERE / "sample_data.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--look", metavar="SETTINGS_JSON", help="settings.json whose .look is used")
    ap.add_argument("--crossword", metavar="PDF", help="append this PDF as the back page(s)")
    ap.add_argument("--print", dest="printer", metavar="QUEUE", help="CUPS queue to print to, e.g. brother")
    ap.add_argument("--png", action="store_true", help="rasterize pages for review")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    data = json.loads(Path(args.data).read_text())
    look = _look_from_settings_file(args.look) if args.look else None

    result = render(data, look, Path(args.out), png=args.png, crossword=args.crossword)
    print(f"{result.pages} page(s) -> {result.pdf}")
    for p in result.pngs:
        print(f"  {p}")

    if args.printer:
        subprocess.run(
            ["lp", "-d", args.printer, "-o", "media=Letter", "-o", "sides=two-sided-long-edge", str(result.pdf)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    # Run as a script the repo root is not on sys.path; app.settings is
    # wanted for --look and for the Look defaults.
    sys.path.insert(0, str(HERE.parent))
    sys.exit(main())
