"""The sample issue renders, and every word of it is the author's."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from render.render import render
from tests.verbatim import assert_verbatim, reconstruct

SAMPLE = Path(__file__).resolve().parent.parent / "render" / "sample_data.json"


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


def test_articles_are_printed_verbatim(rendered):
    data, result = rendered
    assert_verbatim(result.laid_out_html, data["articles"])


def test_every_article_is_continued_inside(rendered):
    """The sample issue is long enough that all four stories jump."""
    data, result = rendered
    for article in data["articles"]:
        front = reconstruct(result.laid_out_html, article["title"])
        assert front == article["paragraphs"]
    assert "Continued from Page" in result.laid_out_html


def test_png_rasterization(sample_data, tmp_path):
    result = render(sample_data, None, tmp_path, png=True)
    assert len(result.pngs) == result.pages
    assert all(p.exists() and p.stat().st_size > 0 for p in result.pngs)


def test_crossword_is_appended(sample_data, tmp_path):
    import pymupdf

    first = render(sample_data, None, tmp_path / "a")
    crossword = tmp_path / "crossword.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(crossword)
    doc.close()

    merged = render(sample_data, None, tmp_path / "b", crossword=crossword)
    with pymupdf.open(merged.pdf) as out:
        assert out.page_count == first.pages + 1
    assert merged.pages == first.pages       # page count is the paper's, not the crossword's
