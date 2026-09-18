"""The sample issue renders on one sheet, and every word of it is the author's.

The sheet is filled: whole articles first, and the last of them may stop at
a paragraph boundary with a line saying where the rest of the story is.
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
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


# ------------------------------------------------------------ the layout
#: `look.layout` spelled out with the values that are its defaults. The
#: paper this produces must be the paper the defaults produce, to the pixel:
#: rearranging is something the reader asks for, never something she gets.
DEFAULT_LAYOUT = {
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
}

#: The rail of the paper as it has always been set.
DEFAULT_RAIL = ["Today", "To do", "Hour by hour", "Notes"]


def test_the_default_layout_is_the_paper_as_it_was(sample_data, tmp_path):
    """Spelling the defaults out changes nothing, down to the pixels."""
    from bs4 import BeautifulSoup

    plain = render(sample_data, None, tmp_path / "implicit", png=True)
    spelled = render(sample_data, {"layout": DEFAULT_LAYOUT}, tmp_path / "explicit", png=True)

    assert plain.pages == spelled.pages == 2
    assert plain.printed == spelled.printed == SAMPLE_PRINTED
    assert plain.partial == spelled.partial == {}
    assert [q.read_bytes() for q in plain.pngs] == [q.read_bytes() for q in spelled.pngs], \
        "the default layout must render the same page images as no layout at all"

    for html in (plain.laid_out_html, spelled.laid_out_html):
        soup = BeautifulSoup(html, "html.parser")
        assert [h.get_text() for h in soup.select("#page-1 .rail h3")] == DEFAULT_RAIL
        assert soup.select("#page-2 .page2-rail") == []      # nothing is on page 2 by default
        assert "--rail-w: 1.9in" in html


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


# ------------------------------------------ the morning with nothing queued
#: With no article on the sheet there is nothing to continue: the paper is
#: one page, and the crossword takes the front where the lead would have been.
def test_a_crossword_alone_is_a_one_page_paper(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["articles"] = []
    result = render(data, None, tmp_path / "onlyxw", png=True)

    assert result.pages == 1 and result.printed == []
    assert len(result.pngs) == 1
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert soup.select_one("#page-2") is None, "there is no back of the sheet"
    xw = soup.select_one("#page-1 .stories #xword")
    assert xw is not None, "the puzzle is set where the lead would have been"
    assert "front" in (xw.get("class") or [])
    assert "The Crossword" in xw.get_text()
    for field in ("title", "author"):
        assert data["crossword"][field] in xw.get_text()
    # The line about there being no stories belongs to the morning with no
    # puzzle either; the puzzle is the front page, not an apology.
    assert "No new stories" not in soup.select_one("#page-1").get_text()
    assert_verbatim(result, data["articles"])


def test_the_one_page_pdf_is_one_page(sample_data, tmp_path):
    import pymupdf

    data = copy.deepcopy(sample_data)
    data["articles"] = []
    result = render(data, None, tmp_path / "onepage")
    with pymupdf.open(result.pdf) as doc:
        assert doc.page_count == 1


def test_every_clue_is_printed_on_the_front_page_too(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["articles"] = []
    result = render(data, None, tmp_path / "frontclues")
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    text = " ".join(p.get_text(" ") for p in soup.select("#page-1 #xword .xw-clues p"))
    for side in ("across", "down"):
        for clue in data["crossword"][side]:
            assert clue["clue"] in text
    # Bigger than page 2's: the column is the puzzle's for the morning.
    style = soup.select_one("#xword")["style"]
    cell = float(style.split("--cell:")[1].split("in")[0])
    assert cell > 0.19, style
    assert "--xw-clue: 8pt" in style, style


def test_a_section_the_reader_put_on_page_two_joins_the_rail(sample_data, tmp_path):
    """There is no page 2 that morning, so its sections print on page 1."""
    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["articles"] = []
    look = {"layout": {"sections": [
        {"key": "agenda", "place": "rail"},
        {"key": "hourly", "place": "rail"},
        {"key": "list:tasks", "place": "page2"},
    ]}}
    result = render(data, look, tmp_path / "p2section")

    assert result.pages == 1
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert soup.select_one("#page-2") is None
    # The rail's own sections first, then the ones page 2 would have held.
    assert [h.get_text() for h in soup.select("#page-1 .rail h3")] == \
        ["Today", "Hour by hour", "To do"]
    items = [li.get_text() for li in soup.select("#page-1 .rail .todo li")]
    assert items == sample_data["lists"][0]["items"]


def test_without_a_crossword_the_one_page_paper_says_so(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["articles"] = []
    data["crossword"] = None
    result = render(data, None, tmp_path / "onlynothing")

    assert result.pages == 1 and result.printed == []
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert soup.select_one("#page-2") is None
    assert "No new stories this morning." in soup.select_one("#page-1").get_text()
    assert soup.select("#page-1 .rail h3"), "her own day is still printed"


def test_a_queue_nothing_fits_is_a_one_page_paper(sample_data, tmp_path):
    """One story, one paragraph, too long for the page it would open on.

    Not even its first paragraph fits, so there is nothing to print partially
    and nothing to hold the sheet open: the search ends with an empty front
    page, and the paper is the crossword's.
    """
    from bs4 import BeautifulSoup

    article = copy.deepcopy(sample_data["articles"][0])
    article["title"] = "One paragraph, four thousand words"
    article["paragraphs"] = [" ".join(f"word{i}" for i in range(4000))]
    data = copy.deepcopy(sample_data)
    data["articles"] = [article]
    result = render(data, {"layout": {"front_stories": 1}}, tmp_path / "impossible")

    assert result.printed == [] and result.partial == {}
    assert result.pages == 1
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert soup.select_one("#page-2") is None
    assert soup.select_one("#page-1 .stories #xword") is not None
    assert article["title"] not in soup.select_one("#page-1").get_text()
    assert_verbatim(result, data["articles"])


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


def test_the_folio_drops_empty_imprint_and_price(sample_data, tmp_path):
    """With nothing to say there, the rule under the masthead balances the
    volume and date alone instead of leaving gaps for empty boxes."""
    from bs4 import BeautifulSoup

    def folio_spans(look):
        result = render(sample_data, look, tmp_path / str(len(look)))
        first = BeautifulSoup(result.laid_out_html, "html.parser").select_one("#page-1 .folio")
        return [x.get_text(strip=True) for x in first.select("span")]

    assert len(folio_spans({"imprint": "Printed at home", "price": "Free"})) == 4
    assert len(folio_spans({"imprint": "", "price": "Free"})) == 3
    assert len(folio_spans({"imprint": "", "price": ""})) == 2


# ------------------------------------------------------------- the ears
#: What each kind of ear puts in its box, drawn from the sample data. The
#: countdown is the one that is not in the data: it is counted from the day
#: the paper is made, so the test sets a date twelve days from now.
IN_TWELVE_DAYS = (date.today() + timedelta(days=12)).isoformat()

EAR_KINDS_AND_WORDS = [
    ("weather", {}, ["Fog early, clearing by noon", "68", "W 12 mph"]),
    ("date", {}, ["Wednesday, September 16, 2026"]),
    ("monogram", {"initials": "M. L.", "lines": ["Continued stories inside."]},
     ["M. L.", "Continued stories inside."]),
    ("issue", {}, ["Vol. I", "No. 1"]),
    ("text", {"lines": ["The crossword is on the back page.", "Bins out tonight."]},
     ["The crossword is on the back page.", "Bins out tonight."]),
    ("next_event", {}, ["7:30 Pilates"]),
    ("sun", {}, ["6:53 a.m.", "7:15 p.m.", "12h 22m"]),
    ("countdown", {"countdown_date": IN_TWELVE_DAYS, "countdown_label": "Sarah's visit"},
     ["12 days", "Sarah's visit"]),
    ("puzzle", {}, ["Low Tide", "By Dana Kestrel"]),
    ("none", {}, []),
]


def nameplate_ears(html):
    """The two boxes of page 1's nameplate, left and right, or None for a
    side set to nothing (there is no box there at all)."""
    from bs4 import BeautifulSoup

    plate = BeautifulSoup(html, "html.parser").select_one("#page-1 .nameplate")
    boxes = plate.find_all(recursive=False)
    assert [b.name for b in boxes] == ["div", "h1", "div"], boxes
    assert "masthead" in (boxes[1].get("class") or [])
    return [b if "ear" in (b.get("class") or []) else None for b in (boxes[0], boxes[2])]


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("kind, fields, words", EAR_KINDS_AND_WORDS,
                         ids=[k for k, _f, _w in EAR_KINDS_AND_WORDS])
def test_every_kind_of_ear_sets_its_own_side(sample_data, tmp_path, side, kind, fields, words):
    """Whatever is in a box, the sheet is the same sheet and the stories on
    it are still the authors' own."""
    from app.settings import EarBox

    look = {f"ear_{side}": EarBox(kind=kind, **fields).model_dump()}
    result = render(sample_data, look, tmp_path / f"{kind}-{side}")

    assert result.pages == 2
    assert result.printed == SAMPLE_PRINTED
    assert_verbatim(result, sample_data["articles"])

    left, right = nameplate_ears(result.laid_out_html)
    box, other = (left, right) if side == "left" else (right, left)
    if kind == "none":
        assert box is None, "a box set to nothing is no box"
        # The masthead is still there, centred over what is left of the plate.
        assert "grid-template-columns" in result.laid_out_html
    else:
        assert box is not None
        text = box.get_text(" ", strip=True)
        for word in words:
            assert word in text, (kind, side, text)
    # The other side is untouched: it is still the box it always was.
    assert other is not None
    wanted = "Fog early" if side == "right" else "Continued stories inside."
    assert wanted in other.get_text(" ", strip=True)


def test_an_ear_kind_the_template_does_not_know_is_an_empty_side(sample_data, tmp_path):
    """An odd settings file still prints a paper."""
    result = render(sample_data, {"ear_left": {"kind": "horoscope"}}, tmp_path / "odd")
    assert result.pages == 2
    left, right = nameplate_ears(result.laid_out_html)
    assert left is None and right is not None


def test_a_puzzleless_morning_says_so_in_the_ear(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["crossword"] = None
    result = render(data, {"ear_left": {"kind": "puzzle"}}, tmp_path / "nopuzzle")
    left, _right = nameplate_ears(result.laid_out_html)
    assert left.get_text(" ", strip=True) == "No puzzle today"


def test_a_day_with_nothing_at_a_time_on_it_says_so_in_the_ear(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["events"] = [{"time": "All day", "title": "Recycling goes out tonight"}]
    result = render(data, {"ear_left": {"kind": "next_event"}}, tmp_path / "allday")
    left, _right = nameplate_ears(result.laid_out_html)
    assert left.get_text(" ", strip=True) == "Nothing scheduled"


@pytest.mark.parametrize("days, expected", [(0, "Today"), (1, "1 day"), (12, "12 days"),
                                            (-1, "1 day since"), (-5, "5 days since")])
def test_the_countdown_counts_from_the_day_the_paper_is_made(days, expected):
    from render.render import countdown

    assert countdown((date.today() + timedelta(days=days)).isoformat()) == expected
    assert countdown("not a date") == ""
    assert countdown("") == ""


def test_an_ear_too_full_for_its_box_is_set_smaller_never_cut(sample_data, tmp_path):
    """The box is 1.3in by 1.12in of furniture: the words fit inside it."""
    from bs4 import BeautifulSoup

    lines = ["The crossword is on the back page today.",
             "Recycling goes out tonight, and the compost too.",
             "Remember the library books are due back on Thursday.",
             "Extraordinarily unpronounceable supercalifragilistic."]
    result = render(sample_data, {"ear_left": {"kind": "text", "lines": lines}},
                    tmp_path / "toofull")
    assert result.pages == 2
    left, _right = nameplate_ears(result.laid_out_html)
    text = left.get_text(" ", strip=True)
    for line in lines:                       # every word of it is still there
        assert line in text
    # ... set smaller by the fitting script, which is the only thing it may do.
    assert "font-size" in (left.get("style") or ""), left.get("style")
    assert BeautifulSoup(result.laid_out_html, "html.parser").select_one("#page-1 .masthead")


# ------------------------------------------------- where the date is printed
def folio_of(html):
    from bs4 import BeautifulSoup

    folio = BeautifulSoup(html, "html.parser").select_one("#page-1 .folio")
    return [x.get_text(strip=True) for x in folio.select("span")]


def dateline_of(html):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").select_one("#page-1 .dateline")


SAMPLE_DATE = "Wednesday, September 16, 2026"


def test_the_date_is_on_the_folio_rule_by_default(rendered):
    _data, result = rendered
    assert SAMPLE_DATE in folio_of(result.laid_out_html)
    assert dateline_of(result.laid_out_html) is None


@pytest.mark.parametrize("place", ["above", "below"])
def test_the_date_can_be_a_line_of_its_own(sample_data, tmp_path, place):
    result = render(sample_data, {"date_place": place, "date_size_pt": 12.0},
                    tmp_path / f"date-{place}")
    assert result.pages == 2
    line = dateline_of(result.laid_out_html)
    assert line is not None and line.get_text(strip=True) == SAMPLE_DATE
    # It is the only place the date is printed on the front: the rule drops it.
    assert SAMPLE_DATE not in folio_of(result.laid_out_html)
    assert folio_of(result.laid_out_html) == \
        ["Vol. I, No. 1", "Printed at home before sunrise", "Single copy, free"]

    # Above the masthead, or between it and the rule.
    order = [t.name for t in line.parent.find_all(recursive=False)]
    where = order.index("p")
    assert (where < order.index("header")) == (place == "above"), order
    assert where < order.index("div"), order        # always above the folio rule


def test_the_date_can_be_left_to_an_ear(sample_data, tmp_path):
    result = render(sample_data, {"date_place": "none", "ear_right": {"kind": "date"}},
                    tmp_path / "date-ear")
    assert dateline_of(result.laid_out_html) is None
    assert SAMPLE_DATE not in folio_of(result.laid_out_html)
    _left, right = nameplate_ears(result.laid_out_html)
    assert right.get_text(" ", strip=True) == SAMPLE_DATE


def test_the_date_can_be_printed_nowhere_at_all(sample_data, tmp_path):
    result = render(sample_data, {"date_place": "none"}, tmp_path / "date-nowhere")
    assert result.pages == 2
    assert dateline_of(result.laid_out_html) is None
    assert folio_of(result.laid_out_html) == \
        ["Vol. I, No. 1", "Printed at home before sunrise", "Single copy, free"]


@pytest.mark.parametrize("place", ["folio", "above", "below", "none"])
def test_the_date_is_set_in_the_face_and_size_the_reader_chose(sample_data, tmp_path, place):
    """Wherever it is printed, including the ear that shows it."""
    look = {"date_place": place, "date_font": "Bodoni Moda", "date_size_pt": 13.5,
            "ear_left": {"kind": "date"}}
    result = render(sample_data, look, tmp_path / f"face-{place}")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    set_in = [t["style"] for t in soup.select("#page-1 .dateline, #page-1 .ear-date")]
    assert set_in, place
    for style in set_in:
        assert '"Bodoni Moda"' in style and "13.5pt" in style, style
