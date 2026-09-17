"""
Layout edges: whatever the gather step hands over, the paper is one sheet
of two pages, and every article on it is there whole -- except the last,
which may stop at a paragraph boundary with a line saying where the rest
of it is. Nothing is ever reworded, and no paragraph is ever split but at
the front-page jump.
"""
from __future__ import annotations

import copy

import pytest

from render.render import render
from tests.verbatim import assert_verbatim, lengthen


def _with(sample_data, **over):
    data = copy.deepcopy(sample_data)
    data.update(over)
    return data


def _render(data, tmp_path, name="out"):
    result = render(data, None, tmp_path / name)
    assert result.pages == 2, f"{result.pages} pages"
    assert_verbatim(result, data["articles"])
    return result


def _long_article(sample_data, words=3000, word="word"):
    """One article of `words` words, far more than a sheet can hold."""
    article = copy.deepcopy(sample_data["articles"][0])
    article["title"] = f"A very long piece about {word}s"
    article["paragraphs"] = [" ".join(f"{word}{i}" for i in range(n, n + 500))
                             for n in range(0, words, 500)]
    return article


def _short_article(sample_data, word="brief"):
    article = copy.deepcopy(sample_data["articles"][3])
    article["title"] = f"A {word} note on the weather"
    article["paragraphs"] = [f"{word.capitalize()} enough to fit anywhere. " * 3]
    return article


@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_article_counts(sample_data, tmp_path, n):
    articles = copy.deepcopy(sample_data["articles"])
    while len(articles) < n:                       # 5 = four plus a spare
        spare = copy.deepcopy(articles[len(articles) % 4])
        spare["title"] = f"A fifth story, number {len(articles)}"
        spare["paragraphs"] = [f"Spare paragraph {i} of a story nobody has read yet, "
                               "put here to fill a slot." for i in range(4)]
        articles.append(spare)
    data = _with(sample_data, articles=articles[:n])

    result = _render(data, tmp_path, f"n{n}")
    if n == 0:
        assert result.printed == []
        assert "No new stories this morning." in result.laid_out_html
    else:
        assert result.printed == list(range(len(result.printed)))   # always a prefix
        assert set(result.partial) <= {result.printed[-1]}          # only the last is partial
    # The second row is only there when a second story was printed.
    assert ('class="row"' in result.laid_out_html) == (len(result.printed) >= 2)


def test_the_second_row_counts_the_stories_that_printed(sample_data, tmp_path):
    for n in (2, 3, 4):
        data = _with(sample_data, articles=copy.deepcopy(sample_data["articles"][:n]))
        result = _render(data, tmp_path, f"row{n}")
        row = len(result.printed) - 1
        if row >= 1:
            assert f"repeat({row}, 1fr)" in result.laid_out_html


def test_a_three_thousand_word_article_alone_is_printed_as_far_as_it_fits(sample_data, tmp_path):
    """It cannot fit whole, so the sheet takes as much of it as it holds."""
    data = _with(sample_data, articles=[_long_article(sample_data)])
    result = _render(data, tmp_path, "long")
    assert result.printed == [0]
    assert result.partial == {0: 1}, result.partial      # its paragraphs are 500 words each
    assert data["articles"][0]["title"] in result.laid_out_html

    from bs4 import BeautifulSoup

    line = BeautifulSoup(result.laid_out_html, "html.parser").select_one("p.online")
    assert line is not None and "The rest of this story is online" in line.get_text()


def test_the_lowest_priority_article_is_dropped_first(sample_data, tmp_path):
    """[too long, short]: the short one goes, and the lead prints partially."""
    data = _with(sample_data, articles=[_long_article(sample_data), _short_article(sample_data)])
    result = _render(data, tmp_path, "longfirst")
    assert result.printed == [0]
    assert result.partial == {0: 1}, result.partial


def test_four_impossible_articles_leave_the_lead_partially_printed(sample_data, tmp_path):
    """Nothing fits whole, so the sheet is filled with the start of the lead."""
    articles = [_long_article(sample_data, word=w) for w in ("alpha", "beta", "gamma", "delta")]
    data = _with(sample_data, articles=articles)
    result = _render(data, tmp_path, "fourlong")
    assert result.printed == [0]
    assert list(result.partial) == [0] and result.partial[0] >= 1, result.partial


def test_a_short_lead_survives_an_impossible_second_story(sample_data, tmp_path):
    """The lead prints whole and the giant behind it prints as far as it fits."""
    data = _with(sample_data, articles=[_short_article(sample_data), _long_article(sample_data)])
    result = _render(data, tmp_path, "shortfirst")
    assert result.printed == [0, 1]
    assert list(result.partial) == [1] and result.partial[1] >= 1, result.partial


def _sunday_crossword(n=21):
    """21x21, 140 clues: the biggest puzzle the paper will ever carry."""
    grid = []
    number = 0
    for r in range(n):
        row = []
        for c in range(n):
            if (r % 5 == 4 and c % 4 == 3) or (r % 7 == 3 and c % 6 == 2):
                row.append(None)
                continue
            if (r + c) % 9 == 0:
                number += 1
                row.append({"n": number})
            else:
                row.append({"n": None})
        grid.append(row)
    return {
        "provider": "nyt", "date": "2026-09-20", "title": "The Big One",
        "author": "Dana Kestrel", "editor": "Margaret Ivey",
        "width": n, "height": n, "grid": grid,
        "across": [{"n": i + 1, "clue": f"Clue number {i + 1}, about something or other"}
                   for i in range(70)],
        "down": [{"n": i + 1, "clue": f"Another clue, the {i + 1}th, of moderate length"}
                 for i in range(70)],
    }


def test_a_sunday_size_crossword_still_leaves_room_for_the_lead(sample_data, tmp_path):
    data = _with(sample_data, crossword=_sunday_crossword())
    result = _render(data, tmp_path, "sunday")
    assert 0 in result.printed, "the lead story must still be printed"


def test_headings_blockquotes_and_lists_as_paragraphs(sample_data, tmp_path):
    article = copy.deepcopy(sample_data["articles"][0])
    article["paragraphs"] = [
        "A heading",                                        # h2 in the source post
        "An ordinary paragraph that follows the heading and runs on a while.",
        "A quoted line, standing alone, as a blockquote would.",
        "First list item",
        "Second list item",
        "Third list item, longer than the others so that it wraps at least once.",
        "A closing paragraph.",
    ]
    data = _with(sample_data, articles=[article] + copy.deepcopy(sample_data["articles"][1:]))
    result = _render(data, tmp_path, "blocks")
    assert 0 in result.printed


@pytest.mark.parametrize("n", [0, 15])
def test_event_counts(sample_data, tmp_path, n):
    events = (sample_data["events"] * 3)[:n]
    data = _with(sample_data, events=events)
    _render(data, tmp_path, f"ev{n}")


def test_twelve_long_tasks(sample_data, tmp_path):
    items = [f"Task {i}: " + "something that will not fit on one line " * 2 for i in range(12)]
    lists = [dict(sample_data["lists"][0], items=items)] + sample_data["lists"][1:]
    data = _with(sample_data, lists=lists)
    _render(data, tmp_path, "tasks")


def test_a_data_file_from_before_the_lists_still_renders(sample_data, tmp_path):
    """An archived data.json has one list, under `tasks`. It is "To do"."""
    from bs4 import BeautifulSoup

    data = _with(sample_data, tasks=["Return library books", "Water the fig tree"])
    del data["lists"]
    result = _render(data, tmp_path, "oldtasks")
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert [h.get_text() for h in soup.select("#page-1 .rail h3")] == \
        ["Today", "To do", "Hour by hour", "Notes"]
    items = soup.select("#page-1 .rail .todo li")
    assert [li.get_text() for li in items] == data["tasks"]
    assert all(li.select(".box") for li in items)      # the box the paper has always had


# ------------------------------------------------ the crossword, arranged
def test_the_crossword_can_be_put_at_the_top_of_page_two(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    result = render(sample_data, {"layout": {"crossword_place": "top"}}, tmp_path / "xwtop")
    assert result.pages == 2
    assert_verbatim(result, sample_data["articles"])
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    page2 = soup.select_one("#page-2")
    kids = [k for k in page2.find_all(["section", "div"], recursive=False)]
    order = [" ".join(k.get("class") or []) for k in kids]
    assert order.index("xword top") < next(i for i, c in enumerate(order) if "cols" in c), order
    assert result.printed, "the sheet still carries stories"


@pytest.mark.parametrize("cell, expected", [(0.14, 0.14), (0.26, 0.24)])
def test_the_grid_is_set_to_the_cell_size_asked_for(sample_data, tmp_path, cell, expected):
    """A 15-wide grid never takes more than 3.6in of the 7.6in measure, so
    the clues beside it always have their half of the page."""
    from bs4 import BeautifulSoup

    result = render(sample_data, {"layout": {"crossword_cell_in": cell}}, tmp_path / f"cell{cell}")
    assert result.pages == 2
    assert_verbatim(result, sample_data["articles"])
    style = BeautifulSoup(result.laid_out_html, "html.parser").select_one("#xword")["style"]
    assert f"--cell: {expected}in" in style, style


def test_the_two_cell_sizes_are_two_different_grids(sample_data, tmp_path):
    """Measured on the printed page: the clues begin where the grid ends."""
    import pymupdf

    def grid_width(cell):
        result = render(sample_data, {"layout": {"crossword_cell_in": cell}},
                        tmp_path / f"w{cell}")
        assert result.pages == 2
        with pymupdf.open(result.pdf) as doc:
            across = min(b[0] for b in doc[1].search_for("Across"))
        return (across / 72) - 0.45 - 0.17          # left margin, then the gap

    #: 15 cells of 0.14in, against 15 of 0.24in (0.26 capped so that the grid
    #: keeps to its 3.6in of the measure): an inch and a half between them.
    small, big = grid_width(0.14), grid_width(0.26)
    assert small < big, (small, big)
    assert 1.4 <= big - small <= 1.6, (small, big)


def test_a_smaller_share_of_page_two_still_prints_the_paper(sample_data, tmp_path):
    """25% and a Sunday grid: the clues shrink as far as they go, the sheet
    is still one sheet, and not a word of a story is touched."""
    data = _with(sample_data, crossword=_sunday_crossword())
    result = render(data, {"layout": {"crossword_max_pct": 25}}, tmp_path / "pct25")
    assert result.pages == 2
    assert_verbatim(result, data["articles"])
    assert "--xw-clue: 4.5pt" in result.laid_out_html, "the clues were shrunk as far as they go"


def test_deck_may_be_null(sample_data, tmp_path):
    articles = copy.deepcopy(sample_data["articles"])
    for a in articles:
        a["deck"] = None
    data = _with(sample_data, articles=articles)
    result = _render(data, tmp_path, "nodeck")
    assert 'class="deck"' not in result.laid_out_html


def test_empty_paper(tmp_path):
    """A total gather failure still prints the sheet, both sides of it."""
    data = {
        "paper": {"volume": "Vol. I, No. 9", "date": "Wednesday, September 16, 2026"},
        "weather": {"summary": "Forecast unavailable", "high": "—", "low": "—",
                    "wind": "—", "sunrise": "—", "sunset": "—", "hourly": []},
        "events": [],
        "tasks": [],
        "articles": [],
        "crossword": None,
    }
    result = _render(data, tmp_path, "empty")
    assert result.printed == []
    assert 'id="page-2"' in result.laid_out_html      # the back of the sheet is still there


def test_a_lone_lead_fills_the_front_page(tmp_path, sample_data):
    """One unread post must not leave the front page half blank: with no
    second row, the lead's body grows to the bottom of page 1 instead of
    stopping at the fixed slot height."""
    from bs4 import BeautifulSoup

    def front_words(articles):
        data = copy.deepcopy(sample_data)
        data["articles"] = articles
        result = render(data, None, tmp_path / str(len(articles)))
        assert_verbatim(result, data["articles"])
        body = BeautifulSoup(result.laid_out_html, "html.parser").select_one("#page-1 .story.lead .body")
        return sum(
            len(p.get_text().split())
            for p in body.find_all("p")
            if not ({"jump", "online"} & set(p.get("class") or []))
        )

    long = lengthen(sample_data["articles"][0], 6)
    alone = front_words([long])
    with_row = front_words(sample_data["articles"])   # the same lead, fixed slot, three stories under it
    assert alone > with_row * 1.6, (alone, with_row)
