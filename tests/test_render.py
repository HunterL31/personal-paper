"""The sample issue renders on one sheet, and every word of it is the author's.

The sheet is filled: whole articles first, and the last of them may stop at
a paragraph boundary with a line saying where the rest of the story is.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from render.render import render
from tests.verbatim import assert_verbatim, reconstruct, lengthen

SAMPLE = Path(__file__).resolve().parent.parent / "render" / "sample_data.json"

#: What the default Look fits on the sheet: the lead and the next two, whole.
#: The fourth story is held for another day -- with four stories on the front
#: the first three overrun page 2, so not even its first paragraph can be
#: printed beside the crossword.
SAMPLE_PRINTED = [0, 1, 2]

#: The sample at 11pt: only the lead, and only its first eight paragraphs.
BIG_TYPE_PARTIAL = {0: 8}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    data = json.loads(SAMPLE.read_text())
    return data, render(data, None, tmp_path_factory.mktemp("sample"))


def test_sample_issue_is_two_pages(rendered):
    _, result = rendered
    assert result.pages == 2


def test_writes_pdf_and_html_together(rendered):
    _, result = rendered
    assert result.pdf.exists() and result.pdf.stat().st_size > 0
    assert result.html.exists()
    assert result.html.parent == result.pdf.parent
    assert result.pngs == []


def test_the_pdf_is_the_two_pages(rendered):
    import pymupdf

    _, result = rendered
    with pymupdf.open(result.pdf) as doc:
        assert doc.page_count == 2
        assert round(doc[0].rect.width) == 612 and round(doc[0].rect.height) == 792


def test_articles_are_printed_verbatim(rendered):
    data, result = rendered
    assert_verbatim(result, data["articles"])


def test_the_whole_stories_that_fit_are_the_ones_printed(rendered):
    data, result = rendered
    assert result.printed == SAMPLE_PRINTED
    assert result.partial == {}                # all three are printed whole
    assert len(data["articles"]) == 4          # the fourth did not fit and was held back

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert soup.select("p.online") == []        # nothing on the sheet is half a story


def test_every_printed_article_is_continued_on_page_two(rendered):
    """The sample stories are all longer than their front-page slots."""
    data, result = rendered
    for i in result.printed:
        assert reconstruct(result.laid_out_html, data["articles"][i]["title"]) == \
            data["articles"][i]["paragraphs"]
    assert "Continued from Page 1" in result.laid_out_html
    assert "Continued on Page 2" in result.laid_out_html


def test_dropping_a_story_reflows_the_second_row(rendered):
    """Three stories printed means two in the row, not three."""
    _, result = rendered
    assert "repeat(2, 1fr)" in result.laid_out_html


def test_png_rasterization(sample_data, tmp_path):
    result = render(sample_data, None, tmp_path, png=True)
    assert len(result.pngs) == result.pages == 2
    assert all(p.exists() and p.stat().st_size > 0 for p in result.pngs)


# ------------------------------------------------------------- crossword
def test_the_crossword_is_typeset_on_page_two(rendered):
    from bs4 import BeautifulSoup

    data, result = rendered
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    xw = soup.select_one("#page-2 #xword")
    assert xw is not None
    assert "The Crossword" in xw.get_text()
    for field in ("title", "author", "editor"):
        assert data["crossword"][field] in xw.get_text()

    rows = xw.select(".xw-grid tr")
    assert len(rows) == data["crossword"]["height"]
    assert all(len(r.find_all("td")) == data["crossword"]["width"] for r in rows)
    black = [td for td in xw.select(".xw-grid td") if "blk" in (td.get("class") or [])]
    assert black, "a crossword has black squares"
    assert len(black) == sum(c is None for row in data["crossword"]["grid"] for c in row)


def test_the_grid_holds_numbers_and_never_a_solution(rendered):
    from bs4 import BeautifulSoup

    data, result = rendered
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    numbers = [c["n"] for row in data["crossword"]["grid"] for c in row if c and c["n"]]
    printed = [td.get_text().strip() for td in soup.select("#xword .xw-grid td")]
    assert [p for p in printed if p] == [str(n) for n in numbers]
    assert all(p.isdigit() for p in printed if p), "no letters may ever be printed in the grid"


def test_every_clue_is_printed(rendered):
    from bs4 import BeautifulSoup

    data, result = rendered
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    text = " ".join(p.get_text(" ") for p in soup.select("#xword .xw-clues p"))
    for side in ("across", "down"):
        for clue in data["crossword"][side]:
            assert clue["clue"] in text
    heads = [h.get_text() for h in soup.select("#xword .xw-clues h4")]
    assert heads == ["Across", "Down"]


def test_the_crossword_keeps_to_its_share_of_the_page(rendered):
    """Measured on the printed page: head, grid and clues inside 55% of it."""
    import pymupdf

    _, result = rendered
    with pymupdf.open(result.pdf) as doc:
        page = doc[1]
        top = min(b[1] for b in page.search_for("The Crossword"))
        height_in = (page.rect.height - 0.4 * 72 - top) / 72        # to the bottom margin
    assert 3.0 <= height_in <= 0.55 * 11, f"the crossword section is {height_in:.2f}in"


# ----------------------------------------------------------- hyphenation
def test_long_words_are_marked_for_breaking(rendered):
    """Chromium has no dictionary, so render.py marks the break points."""
    from render.render import SOFT_HYPHEN

    _, result = rendered
    assert f"Wednes{SOFT_HYPHEN}day" in result.laid_out_html
    assert f"pep{SOFT_HYPHEN}per{SOFT_HYPHEN}corns" in result.laid_out_html


def test_headlines_bylines_and_clues_are_never_marked(rendered):
    from bs4 import BeautifulSoup

    from render.render import SOFT_HYPHEN

    _, result = rendered
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    for sel in ("h2", ".byline", "#xword .xw-clues p", ".xw-head", "h1", "h3", "h4"):
        for tag in soup.select(sel):
            assert SOFT_HYPHEN not in tag.get_text(), f"{sel} was marked: {tag.get_text()[:40]!r}"


def test_the_render_does_not_touch_the_data_it_was_given(sample_data, tmp_path):
    """The marks are typesetting; the saved article stays byte for byte."""
    import copy

    from render.render import SOFT_HYPHEN

    before = copy.deepcopy(sample_data)
    render(sample_data, None, tmp_path / "untouched")
    assert sample_data == before
    assert SOFT_HYPHEN not in json.dumps(sample_data, ensure_ascii=False)


@pytest.mark.parametrize("word, expected", [
    ("extraordinary", "extra|or|di|nary"),           # a long word is marked
    ("soup", "soup"),                                # a short one is not
    ("COVID-19", "COVID-19"),                        # digits and hyphens: left alone
    ("well-known", "well-known"),                    # already hyphenated
    ("https://example.com/kitchen", "https://example.com/kitchen"),
    ("1,000,000", "1,000,000"),
    ("(vegetable)", "(veg|etable)"),                 # punctuation is not the word
    ("yesterday's", "yes|ter|day's"),
])
def test_hyphenate_marks_only_what_it_should(word, expected):
    from render.render import SOFT_HYPHEN, hyphenate

    assert hyphenate(word).replace(SOFT_HYPHEN, "|") == expected


def test_hyphenate_passes_empty_and_non_text_through():
    from render.render import hyphenate

    assert hyphenate(None) is None
    assert hyphenate("") == ""


def test_without_a_crossword_page_two_is_all_continuations(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["crossword"] = None
    result = render(data, None, tmp_path / "nox")
    assert result.pages == 2
    assert 'id="xword"' not in result.laid_out_html
    assert "The Crossword" not in result.laid_out_html
    assert result.printed == [0, 1, 2, 3]      # the room the crossword took is theirs
    assert_verbatim(result, data["articles"])


def test_a_missing_crossword_key_is_the_same_as_none(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    del data["crossword"]
    result = render(data, None, tmp_path / "nokey")
    assert result.pages == 2
    assert 'id="xword"' not in result.laid_out_html


def test_a_crossword_alone_still_prints_a_paper(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["articles"] = []
    result = render(data, None, tmp_path / "onlyxw")
    assert result.pages == 2 and result.printed == []
    assert "No new stories this morning." in result.laid_out_html
    assert "The Crossword" in result.laid_out_html


# ------------------------------------------- the last story, printed partially
#: Big type, so the lead alone is more than the sheet holds.
BIG_TYPE = {"body_size_pt": 11.0}


@pytest.fixture(scope="module")
def partial_issue(tmp_path_factory):
    """Big type and a lead three times its sample length: more than the
    sheet holds, so the lead prints as far as it fits and stops."""
    data = json.loads(SAMPLE.read_text())
    data["articles"][0] = lengthen(data["articles"][0], 3)
    return data, render(data, BIG_TYPE, tmp_path_factory.mktemp("partial"))


def test_a_story_that_cannot_fit_whole_is_printed_as_far_as_it_fits(partial_issue):
    data, result = partial_issue
    assert result.printed == [0]
    total = len(data["articles"][0]["paragraphs"])
    assert list(result.partial) == [0] and 1 <= result.partial[0] < total, (result.partial, total)
    assert_verbatim(result, data["articles"])


def test_the_printed_paragraphs_are_the_author_s_first_ones(partial_issue):
    data, result = partial_issue
    n = result.partial[0]
    assert reconstruct(result.laid_out_html, data["articles"][0]["title"]) == \
        data["articles"][0]["paragraphs"][:n]


def test_the_online_line_ends_the_partial_story(partial_issue):
    """One line, after the last paragraph printed, in a wrapper with it."""
    from bs4 import BeautifulSoup

    from tests.verbatim import pieces

    data, result = partial_issue
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    lines = soup.select("p.online")
    assert len(lines) == 1
    line = lines[0]

    wrapper = line.parent
    assert "ending" in (wrapper.get("class") or []), "the line must travel with its paragraph"
    assert wrapper.find("p") is not line, "the paragraph it follows comes first"

    parts = pieces(result.laid_out_html, data["articles"][0]["title"])
    assert [kind for _page, kind, _text in parts][-1] == "online"


def test_the_online_line_carries_the_url_without_its_scheme(partial_issue):
    from bs4 import BeautifulSoup

    data, result = partial_issue
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    line = soup.select_one("p.online")
    url = data["articles"][0]["url"]
    assert line.get_text() == f"\u2003The rest of this story is online: {url.removeprefix('https://')}"
    assert "https://" not in line.get_text()
    assert line.select_one(".u").get_text() == url.removeprefix("https://")


def test_without_a_url_the_line_just_says_it_is_online(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["articles"][0] = lengthen(data["articles"][0], 3)
    for article in data["articles"]:
        article.pop("url")
    result = render(data, BIG_TYPE, tmp_path / "nourl")
    assert result.partial, "the lead is still printed partially"
    line = BeautifulSoup(result.laid_out_html, "html.parser").select_one("p.online")
    assert line.get_text() == "\u2003The rest of this story is online."
    assert_verbatim(result, data["articles"])


def test_the_online_line_is_set_like_the_jump_line(rendered):
    """Italic, right-aligned, at the byline size, and never left on its own."""
    _, result = rendered
    css = result.laid_out_html[result.laid_out_html.index("p.online"):]
    css = css[:css.index("}")]
    for rule in ("text-align: right", "font-style: italic", "font-size: var(--byline)",
                 "break-inside: avoid"):
        assert rule in css, rule
    assert ".ending { break-inside: avoid; }" in result.laid_out_html
    assert ".page p.online .u { overflow-wrap: break-word; }" in result.laid_out_html
