"""
The verbatim check (house rule 1), shared by the render tests.

The laid-out HTML is the DOM after the fitting script ran, so it contains
exactly what was printed. For one article we walk its front-page slot and
then its page-2 continuation, collect the paragraphs, join `.tail`
fragments back onto the paragraph they were cut from, and hand back the
reconstruction. It must equal the author's paragraph list, element for
element.

The paper is one sheet, so an article that does not fit whole is not
printed at all: `RenderResult.printed` says which ones were, and the rest
must be nowhere on either page.

Soft hyphens are the one mark the typesetter adds: render.py puts them in
every word that may be broken, because headless Chromium has no dictionary
to do it with. They are marks, not text, so they come out again here before
anything is compared.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup


#: The soft hyphen render.py inserts; invisible unless a line breaks there.
SOFT_HYPHEN = "\u00ad"


def _classes(tag) -> list[str]:
    return tag.get("class") or []


def _unmarked(text: str) -> str:
    """The words as the author wrote them, without the break marks."""
    return text.replace(SOFT_HYPHEN, "")


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
            pieces.append((_unmarked(p.get_text()), "tail" in cls))

    for art in soup.select("#page-2 .cols > article"):
        h2 = art.find("h2")
        if h2 is None or h2.get_text() != title:
            continue
        for p in art.find_all("p"):
            cls = _classes(p)
            if "cont" in cls or "deck" in cls or "byline" in cls:
                continue
            pieces.append((_unmarked(p.get_text()), "tail" in cls))

    paragraphs: list[str] = []
    for text, is_tail in pieces:
        if is_tail and paragraphs:
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
        else:
            paragraphs.append(text)
    return paragraphs


def _printed_text(laid_out_html: str) -> str:
    """Everything on the sheet, as words, whatever the markup around it."""
    return re.sub(r"\s+", " ", _unmarked(BeautifulSoup(laid_out_html, "html.parser").get_text(" ")))


def _opening(text: str, words: int = 8) -> str:
    return " ".join(re.sub(r"\s+", " ", text).split()[:words])


def assert_verbatim(result, articles: list[dict]) -> None:
    """The sheet holds the printed articles, whole, and nothing of the rest.

    `result` is a `RenderResult`; `articles` the list it was rendered from.
    """
    assert result.pages == 2, f"the paper is one sheet, not {result.pages} pages"
    assert result.printed == sorted(result.printed), f"printed out of order: {result.printed}"

    for i in result.printed:
        article = articles[i]
        got = reconstruct(result.laid_out_html, article["title"])
        assert got == article["paragraphs"], (
            f"{article['title']!r} was not printed verbatim:\n"
            f"  expected {len(article['paragraphs'])} paragraphs, got {len(got)}"
        )

    printed_text = _printed_text(result.laid_out_html)
    for i, article in enumerate(articles):
        if i in result.printed:
            continue
        assert article["title"] not in printed_text, (
            f"article {i}, {article['title']!r}, was dropped but its title is on the sheet"
        )
        for para in article["paragraphs"]:
            opening = _opening(para)
            assert opening not in printed_text, (
                f"article {i}, {article['title']!r}, was dropped but {opening!r} is on the sheet"
            )
