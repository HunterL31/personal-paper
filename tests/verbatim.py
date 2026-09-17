"""
The verbatim check (house rule 1), shared by the render tests.

The laid-out HTML is the DOM after the fitting script ran, so it contains
exactly what was printed. For one article we walk its front-page slot and
then its page-2 continuation, collect the paragraphs, join `.tail`
fragments back onto the paragraph they were cut from, and hand back the
reconstruction. It must equal the author's paragraph list, element for
element.

The sheet is filled, so the last article on it may be printed partially:
`RenderResult.partial` says how many of its leading paragraphs were, and
those paragraphs must be the author's first N, whole and unchanged, ended
by the one line saying where the rest of the story is. Everything after
them, and every article that did not fit at all, must be nowhere on the
sheet.

A paragraph is split in exactly one place and nowhere else: the front-page
cut, whose remainder is the `.tail` fragment that opens the continuation.

Soft hyphens are the one mark the typesetter adds: render.py puts them in
every word that may be broken, because headless Chromium has no dictionary
to do it with. They are marks, not text, so they come out again here before
anything is compared.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup


#: The soft hyphen render.py inserts; invisible unless a line breaks there.
SOFT_HYPHEN = "­"

#: The line that ends a partial article, as the template writes it.
ONLINE_OPENING = "The rest of this story is online"

#: Paragraphs of the template's own, never the author's.
_FURNITURE = ("jump", "cont", "deck", "byline")


def _classes(tag) -> list[str]:
    return tag.get("class") or []


def _unmarked(text: str) -> str:
    """The words as the author wrote them, without the break marks."""
    return text.replace(SOFT_HYPHEN, "")


def pieces(laid_out_html: str, title: str) -> list[tuple[str, str, str]]:
    """One article's printed parts, in reading order.

    Each is `(page, kind, text)`, where `page` is "1" or "2" and `kind` is
    "text" for a paragraph, "tail" for the remainder of the paragraph the
    front page cut, and "online" for the line saying where the rest is.
    """
    soup = BeautifulSoup(laid_out_html, "html.parser")
    out: list[tuple[str, str, str]] = []

    def collect(page: str, art) -> None:
        for p in art.find_all("p"):
            cls = _classes(p)
            if any(c in cls for c in _FURNITURE):
                continue
            kind = "online" if "online" in cls else "tail" if "tail" in cls else "text"
            out.append((page, kind, _unmarked(p.get_text())))

    for art in soup.select("#page-1 article.story"):
        h2 = art.find("h2")
        if h2 is not None and h2.get_text() == title:
            collect("1", art.select_one(".body") or art)

    for art in soup.select("#page-2 .cols > article"):
        h2 = art.find("h2")
        if h2 is not None and h2.get_text() == title:
            collect("2", art)

    return out


def reconstruct(laid_out_html: str, title: str) -> list[str]:
    """Rebuild one article's paragraphs from the printed pages."""
    paragraphs: list[str] = []
    for _page, kind, text in pieces(laid_out_html, title):
        if kind == "online":
            continue
        if kind == "tail" and paragraphs:
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
        else:
            paragraphs.append(text)
    return paragraphs


def _printed_text(laid_out_html: str) -> str:
    """Everything on the sheet, as words, whatever the markup around it."""
    return re.sub(r"\s+", " ", _unmarked(BeautifulSoup(laid_out_html, "html.parser").get_text(" ")))


def _opening(text: str, words: int = 8) -> str:
    return " ".join(re.sub(r"\s+", " ", text).split()[:words])


def _assert_one_paragraph_cut(parts, title: str) -> None:
    """The only split in an article is the front page's, and it is the last."""
    tails = [i for i, (page, kind, _t) in enumerate(parts) if kind == "tail"]
    assert len(tails) <= 1, f"{title!r} was split in {len(tails)} places, not one"
    if tails:
        i = tails[0]
        assert parts[i][0] == "2", f"{title!r} has a cut fragment on page 1"
        assert all(page == "1" for page, _k, _t in parts[:i]), \
            f"{title!r} was cut somewhere other than the front-page jump"


def assert_verbatim(result, articles: list[dict]) -> None:
    """The sheet holds what it printed, unaltered, and nothing of the rest.

    `result` is a `RenderResult`; `articles` the list it was rendered from.
    The articles that were printed whole are there whole; at most one, the
    last, may be printed as far as a paragraph boundary, ended by the line
    saying where the rest of it is; the others are not there at all.
    """
    assert result.pages == 2, f"the paper is one sheet, not {result.pages} pages"
    assert result.printed == sorted(result.printed), f"printed out of order: {result.printed}"

    partial = dict(result.partial)
    assert len(partial) <= 1, f"at most one article is partial: {partial}"
    for i in partial:
        assert i in result.printed, f"article {i} is partial but was not printed"
        assert i == result.printed[-1], f"the partial article is the last printed, not {i}"

    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert len(soup.select("p.online")) == len(partial), (
        f"{len(soup.select('p.online'))} 'rest is online' line(s) for {len(partial)} partial article(s)"
    )

    held_back: list[tuple[int, dict, list[str]]] = []
    for i, article in enumerate(articles):
        if i not in result.printed:
            held_back.append((i, article, article["paragraphs"]))
            continue

        parts = pieces(result.laid_out_html, article["title"])
        got = reconstruct(result.laid_out_html, article["title"])
        _assert_one_paragraph_cut(parts, article["title"])
        n = partial.get(i)

        if n is None:
            assert "online" not in [k for _p, k, _t in parts], (
                f"{article['title']!r} was printed whole but carries an online line"
            )
            assert got == article["paragraphs"], (
                f"{article['title']!r} was not printed verbatim:\n"
                f"  expected {len(article['paragraphs'])} paragraphs, got {len(got)}"
            )
            continue

        assert 1 <= n <= len(article["paragraphs"]), f"partial count {n} for article {i}"
        assert got == article["paragraphs"][:n], (
            f"{article['title']!r} is partial: the {n} printed paragraph(s) must be the "
            f"author's first {n}, unchanged (got {len(got)})"
        )
        # The line comes last, after the paragraph it follows, and says so.
        kinds = [k for _p, k, _t in parts]
        assert kinds.count("online") == 1, f"{article['title']!r} has {kinds.count('online')} online lines"
        assert kinds[-1] == "online", f"{article['title']!r} ends with {kinds[-1]}, not its online line"
        line = parts[-1][2]
        assert ONLINE_OPENING in line, f"the online line reads {line!r}"
        url = article.get("url")
        if url:
            assert url.split("://", 1)[-1] in line.replace(" ", ""), \
                f"the online line does not carry the article's url: {line!r}"
        else:
            assert line.strip().endswith("online."), f"the online line reads {line!r}"
        held_back.append((i, article, article["paragraphs"][n:]))

    printed_text = _printed_text(result.laid_out_html)
    for i, article, paragraphs in held_back:
        if i not in result.printed:
            assert article["title"] not in printed_text, (
                f"article {i}, {article['title']!r}, was dropped but its title is on the sheet"
            )
        for para in paragraphs:
            opening = _opening(para)
            assert opening not in printed_text, (
                f"article {i}, {article['title']!r}: {opening!r} was held back but is on the sheet"
            )
