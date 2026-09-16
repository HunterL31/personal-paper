"""
The verbatim check (house rule 1), shared by test_render and test_look.

The laid-out HTML is the DOM after the fitting script ran, so it contains
exactly what was printed. For one article we walk its front-page slot and
then every inside-page continuation, collect the paragraphs, join `.tail`
fragments back onto the paragraph they were cut from, and hand back the
reconstruction. It must equal the author's paragraph list, element for
element.
"""
from __future__ import annotations

from bs4 import BeautifulSoup


def _classes(tag) -> list[str]:
    return tag.get("class") or []


def reconstruct(laid_out_html: str, title: str) -> list[str]:
    """Rebuild one article's paragraphs from the printed pages."""
    soup = BeautifulSoup(laid_out_html, "html.parser")
    pieces: list[tuple[str, bool]] = []          # (text, is a tail fragment)

    for art in soup.select("#page-1 article.story"):
        h2 = art.find("h2")
        if h2 is None or h2.get_text() != title:
            continue
        for p in art.select(".body > p"):
            cls = _classes(p)
            if "jump" in cls or "deck" in cls or "byline" in cls:
                continue
            pieces.append((p.get_text(), "tail" in cls))

    for art in soup.select(".page.inside .cols > article"):
        h2 = art.find("h2")
        if h2 is None or h2.get_text() != title:
            continue
        for p in art.find_all("p"):
            cls = _classes(p)
            if "cont" in cls or "deck" in cls or "byline" in cls:
                continue
            pieces.append((p.get_text(), "tail" in cls))

    paragraphs: list[str] = []
    for text, is_tail in pieces:
        if is_tail and paragraphs:
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
        else:
            paragraphs.append(text)
    return paragraphs


def assert_verbatim(laid_out_html: str, articles: list[dict], placed: int = 4) -> None:
    """Every placed article must appear exactly as its author wrote it."""
    for article in articles[:placed]:
        got = reconstruct(laid_out_html, article["title"])
        assert got == article["paragraphs"], (
            f"{article['title']!r} was not printed verbatim:\n"
            f"  expected {len(article['paragraphs'])} paragraphs, got {len(got)}"
        )
