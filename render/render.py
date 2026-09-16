#!/usr/bin/env python3
"""
Render today's paper.

    python render.py data.json                 # -> out/paper.html, out/paper.pdf
    python render.py data.json --png           # also out/page-1.png, page-2.png ... for a quick look
    python render.py data.json --crossword nyt.pdf --print brother

The data file is whatever the gather step produced (see sample_data.json for
the shape). Article paragraphs must be the author's text, untouched.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?", default=HERE / "sample_data.json")
    ap.add_argument("--out", default=HERE / "out")
    ap.add_argument("--crossword", metavar="PDF", help="append this PDF as the back page(s)")
    ap.add_argument("--print", dest="printer", metavar="QUEUE", help="CUPS queue to print to, e.g. brother")
    ap.add_argument("--png", action="store_true", help="rasterize pages for review")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(args.data).read_text())

    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html"]))
    html = env.get_template("template.html").render(
        font_dir=os.path.relpath(HERE / "fonts", out), **data
    )
    html_path = out / "paper.html"
    html_path.write_text(html)

    from playwright.sync_api import sync_playwright

    pdf_path = out / "paper.pdf"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.emulate_media(media="print")          # measure in the same mode we print in
        page.goto(html_path.as_uri())
        page.wait_for_function("window.__layoutDone === true")
        n = page.evaluate("window.__pages")
        page.pdf(path=str(pdf_path), prefer_css_page_size=True, print_background=True)
        browser.close()

    if args.crossword:
        merged = out / "paper_with_crossword.pdf"
        subprocess.run(["pdfunite", str(pdf_path), args.crossword, str(merged)], check=True)
        pdf_path = merged

    print(f"{n} page(s) -> {pdf_path}")

    if args.png:
        subprocess.run(["pdftoppm", "-png", "-r", "70", str(pdf_path), str(out / "page")], check=True)

    if args.printer:
        subprocess.run(
            ["lp", "-d", args.printer, "-o", "media=Letter", "-o", "sides=two-sided-long-edge", str(pdf_path)],
            check=True,
        )


if __name__ == "__main__":
    sys.exit(main())
