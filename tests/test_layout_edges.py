"""
Layout edges: whatever the gather step hands over, the paper renders and
does not explode into a pile of pages.
"""
from __future__ import annotations

import copy

import pytest

from render.render import render
from tests.verbatim import assert_verbatim

MAX_PAGES = 6


def _with(sample_data, **over):
    data = copy.deepcopy(sample_data)
    data.update(over)
    return data


def _render(data, tmp_path, name="out"):
    result = render(data, None, tmp_path / name)
    assert 1 <= result.pages <= MAX_PAGES, f"{result.pages} pages"
    return result


@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_article_counts(sample_data, tmp_path, n):
    articles = sample_data["articles"]
    while len(articles) < n:                       # 5 = four plus a spare
        extra = copy.deepcopy(sample_data["articles"][len(articles) % 4])
        extra["title"] = f"{extra['title']} ({len(articles)})"
        articles = articles + [extra]
    data = _with(sample_data, articles=articles[:n])

    result = _render(data, tmp_path, f"n{n}")
    if n == 0:
        assert "No new stories this morning." in result.laid_out_html
    else:
        assert_verbatim(result.laid_out_html, data["articles"], placed=min(n, 4))
    # The second row is only there when there is a second story.
    assert ('class="row"' in result.laid_out_html) == (n >= 2)


def test_second_row_column_count(sample_data, tmp_path):
    for n in (2, 3, 4):
        data = _with(sample_data, articles=sample_data["articles"][:n])
        result = _render(data, tmp_path, f"row{n}")
        assert f"repeat({n - 1}, 1fr)" in result.laid_out_html


def test_three_thousand_word_article(sample_data, tmp_path):
    long_para = " ".join(f"word{i}" for i in range(500))
    article = copy.deepcopy(sample_data["articles"][0])
    article["paragraphs"] = [long_para] * 6                 # 3,000 words
    data = _with(sample_data, articles=[article])

    result = _render(data, tmp_path, "long")
    assert_verbatim(result.laid_out_html, data["articles"], placed=1)


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
    data = _with(sample_data, articles=[article] + sample_data["articles"][1:])
    result = _render(data, tmp_path, "blocks")
    assert_verbatim(result.laid_out_html, data["articles"])


@pytest.mark.parametrize("n", [0, 15])
def test_event_counts(sample_data, tmp_path, n):
    events = (sample_data["events"] * 3)[:n]
    data = _with(sample_data, events=events)
    _render(data, tmp_path, f"ev{n}")


def test_twelve_long_tasks(sample_data, tmp_path):
    tasks = [f"Task {i}: " + "something that will not fit on one line " * 2 for i in range(12)]
    data = _with(sample_data, tasks=tasks)
    _render(data, tmp_path, "tasks")


def test_deck_may_be_null(sample_data, tmp_path):
    articles = copy.deepcopy(sample_data["articles"])
    for a in articles:
        a["deck"] = None
    data = _with(sample_data, articles=articles)
    result = _render(data, tmp_path, "nodeck")
    assert 'class="deck"' not in result.laid_out_html
    assert_verbatim(result.laid_out_html, data["articles"])


def test_empty_paper(tmp_path):
    """A total gather failure still prints a paper."""
    data = {
        "paper": {"volume": "Vol. I, No. 9", "date": "Wednesday, September 16, 2026"},
        "weather": {"summary": "Forecast unavailable", "high": "—", "low": "—",
                    "wind": "—", "sunrise": "—", "sunset": "—", "hourly": []},
        "events": [],
        "tasks": [],
        "articles": [],
    }
    result = _render(data, tmp_path, "empty")
    assert result.pages == 1
