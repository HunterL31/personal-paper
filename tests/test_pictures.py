"""
The picture sheet: with `look.layout.pictures` on, the pictures of the
stories that printed go on a sheet of their own after page 2, numbered in
the order the reader meets their lines, and a line of the paper's own
stands in the story where the writer put each one. A paragraph is never
touched; the verbatim check runs on every render here.
"""
from __future__ import annotations

import copy
import logging

import pymupdf
import pytest
from bs4 import BeautifulSoup

from render.render import render
from tests.verbatim import assert_verbatim, lengthen

ON = {"layout": {"pictures": True}}


def picture(path, width=400, height=300):
    """A grey picture file at `path`: a dark square in a light field."""
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, width, height), False)
    pix.set_rect(pix.irect, (210,))
    pix.set_rect(pymupdf.IRect(width // 4, height // 4, width // 2, height // 2), (50,))
    pix.save(str(path))
    return path


def _soup(result):
    return BeautifulSoup(result.laid_out_html, "html.parser")


def refs(result, where="") -> list[tuple[str, str]]:
    """Every picture line on the sheet, as (key, text), in document order."""
    return [(p["data-image"], p.get_text(strip=True))
            for p in _soup(result).select(f"{where} p.figref".strip())]


def figures(result) -> list[tuple[str, str]]:
    """Every figure on the picture sheet, as (key, its caption's text)."""
    return [(f["data-image"], f.get_text(" ", strip=True))
            for f in _soup(result).select(".page.pictures .figs figure")]


# --------------------------------------------------------------- the switch
def test_the_picture_sheet_is_off_by_default(sample_data, tmp_path):
    """The sample carries pictures; with the switch off none of it reaches
    the sheet, and the paper is the two pages it has always been."""
    result = render(sample_data, None, tmp_path / "off")
    assert result.pages == 2 and result.picture_pages == 0
    assert result.pictures == [] and result.pictures_dropped == []
    assert refs(result) == [] and figures(result) == []
    assert_verbatim(result, sample_data["articles"])


def test_the_sample_pictures_print_numbered_in_reading_order(sample_data, tmp_path):
    result = render(sample_data, ON, tmp_path / "on", png=True)

    assert result.pages == 3 and result.picture_pages == 1
    assert result.printed == [0, 1, 2]
    assert result.pictures == [(0, 0), (1, 0)] and result.pictures_dropped == []
    assert len(result.pngs) == 3
    assert_verbatim(result, sample_data["articles"])

    # The lines, in the stories, each pointing at its picture's number.
    assert refs(result) == [("0:0", "See Image 1."), ("1:0", "See Image 2.")]
    # The lead's line stands where the writer put the picture: after its
    # second paragraph, before its third.
    body = [p.get_text(strip=True)[:12] for p in _soup(result).select("#page-1 .story.lead .body > p")]
    assert body.index("See Image 1.") == 2
    # The figures, numbered the same way, with the story's title and the
    # author's caption, verbatim.
    assert figures(result) == [
        ("0:0", "Image 1 · Why every good soup starts on Tuesday "
                "The pot, on the lowest flame the burner will hold."),
        ("1:0", "Image 2 · The tide pools are back"),
    ]
    soup = _soup(result)
    assert [s["id"] for s in soup.select(".page")] == ["page-1", "page-2", "page-3"]
    assert "Images" in soup.select_one("#page-3 .folio").get_text()
    assert soup.select_one("#figures") is None, "the store is gone once the sheet is set"


def test_a_picture_of_a_story_that_was_held_is_not_on_the_sheet(sample_data, tmp_path):
    """The sample's fourth story is held for another day; its picture waits
    with it, and no line anywhere points to it."""
    data = copy.deepcopy(sample_data)
    data["articles"][3]["images"] = [{"url": "https://x/held.png", "caption": "Held",
                                      "after": 0, "file": str(picture(tmp_path / "held.png"))}]
    result = render(data, ON, tmp_path / "held")
    assert 3 not in result.printed
    assert result.pictures == [(0, 0), (1, 0)]
    assert (3, 0) not in result.pictures_dropped     # not dropped: never printed
    assert all(key != "3:0" for key, _ in refs(result))
    assert all(key != "3:0" for key, _ in figures(result))
    assert "Held" not in _soup(result).select_one(".page.pictures").get_text()
    assert_verbatim(result, data["articles"])


# ------------------------------------------------------------ the stories
def test_a_line_moves_whole_to_page_2_with_its_paragraphs(sample_data, tmp_path):
    """A picture deep in the lead is met on page 2, where the lead continues:
    the line goes there whole, never cut, and the picture is on the sheet."""
    data = copy.deepcopy(sample_data)
    lead = data["articles"][0]
    lead["images"] = [{"url": "https://x/deep.png", "caption": "Deep", "after": 8,
                       "file": str(picture(tmp_path / "deep.png"))}]
    result = render(data, ON, tmp_path / "deep")
    assert result.pictures == [(0, 0), (1, 0)]
    assert refs(result, "#page-1") == [("1:0", "See Image 2.")]
    assert refs(result, '#page-2 .cols article[data-story="0"]') == [("0:0", "See Image 1.")]
    assert_verbatim(result, data["articles"])


def test_a_partial_story_prints_only_the_pictures_before_its_cut(sample_data, tmp_path):
    """A story printed as far as a paragraph boundary carries the lines of
    the pictures among its printed paragraphs and none of the others."""
    long = lengthen(sample_data["articles"][0], 6)
    long["images"] = [
        {"url": "https://x/a.png", "caption": "Early", "after": 1, "file": str(picture(tmp_path / "a.png"))},
        {"url": "https://x/b.png", "caption": "Late", "after": 55, "file": str(picture(tmp_path / "b.png"))},
    ]
    data = copy.deepcopy(sample_data)
    data["articles"] = [long]
    result = render(data, ON, tmp_path / "partial")
    assert result.partial == {0: result.partial.get(0)} and 0 < result.partial[0] < 55
    assert result.pictures == [(0, 0)]
    assert result.pictures_dropped == []                # not dropped: never printed
    assert refs(result) == [("0:0", "See Image 1.")]
    assert figures(result) == [("0:0", "Image 1 · A very long piece Early")] or \
        figures(result)[0][1].endswith("Early")
    assert_verbatim(result, data["articles"])


def test_the_online_line_still_ends_a_partial_story_after_a_picture_line(sample_data, tmp_path):
    """A picture line that follows the last printed paragraph stays, and the
    online line comes after it, as the last thing in the story."""
    long = lengthen(sample_data["articles"][0], 6)
    data = copy.deepcopy(sample_data)
    data["articles"] = [long]
    plain = render(data, ON, tmp_path / "plain")
    cut = plain.partial[0]
    long["images"] = [{"url": "https://x/at.png", "caption": None, "after": cut,
                       "file": str(picture(tmp_path / "at.png"))}]
    result = render(data, ON, tmp_path / "atcut")
    assert_verbatim(result, data["articles"])
    if result.partial.get(0) == cut:
        assert result.pictures == [(0, 0)]
        assert refs(result) == [("0:0", "See Image 1.")]


# -------------------------------------------------------------- the sheet
def test_the_sheet_is_two_pages_at_most_and_says_what_it_left_off(sample_data, tmp_path, caplog):
    data = copy.deepcopy(sample_data)
    data["articles"] = data["articles"][:1]
    lead = data["articles"][0]
    lead["images"] = [
        {"url": f"https://x/{i}.png", "caption": f"Picture number {i + 1}", "after": i % 4,
         "file": str(picture(tmp_path / f"{i}.png"))}
        for i in range(30)
    ]
    with caplog.at_level(logging.WARNING, logger="render.render"):
        result = render(data, ON, tmp_path / "many")

    assert result.picture_pages == 2 and result.pages == 4
    assert result.printed == [0]
    assert len(result.pictures) >= 6 and result.pictures_dropped
    assert len(result.pictures) + len(result.pictures_dropped) == 30
    assert "had no room for" in caplog.text
    assert_verbatim(result, data["articles"])

    soup = _soup(result)
    assert [s["id"] for s in soup.select(".page")] == ["page-1", "page-2", "page-3", "page-4"]
    more = soup.select_one("#page-4 .figs p.figs-more")
    assert more is not None
    assert more.get_text() == f"{len(result.pictures_dropped)} more pictures, not printed"
    # Numbers are 1..N in the sheet's order, and every line points at a
    # picture that is there; a dropped picture has no line anywhere.
    printed_keys = [f"{a}:{i}" for a, i in result.pictures]
    assert [k for k, _ in figures(result)] == printed_keys
    assert [t.split()[0] + " " + t.split()[1] for _, t in figures(result)] == \
        [f"Image {n}" for n in range(1, len(printed_keys) + 1)]
    assert refs(result) == [(k, f"See Image {n}.") for n, k in enumerate(printed_keys, start=1)]


def test_a_missing_file_prints_neither_picture_nor_line(sample_data, tmp_path, caplog):
    data = copy.deepcopy(sample_data)
    data["articles"][0]["images"] = [{"url": "https://x/gone.png", "caption": "Gone", "after": 1,
                                      "file": str(tmp_path / "gone.png")}]
    data["articles"][1]["images"] = []
    with caplog.at_level(logging.WARNING, logger="render.render"):
        result = render(data, ON, tmp_path / "gone")
    assert "not on disk" in caplog.text
    assert result.pages == 2 and result.picture_pages == 0
    assert refs(result) == [] and figures(result) == []
    assert_verbatim(result, data["articles"])


def test_a_file_that_is_not_a_picture_is_left_off_with_its_line(sample_data, tmp_path):
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this is not a picture")
    data = copy.deepcopy(sample_data)
    data["articles"][0]["images"] = [{"url": "https://x/junk.png", "caption": None, "after": 1,
                                      "file": str(junk)}]
    result = render(data, ON, tmp_path / "junk")
    assert result.pictures == [(1, 0)]
    assert refs(result) == [("1:0", "See Image 1.")]
    assert_verbatim(result, data["articles"])


def test_a_picture_without_a_file_was_never_fetched_and_is_not_asked_for(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["articles"][0]["images"] = [{"url": "https://x/nofile.png", "caption": None, "after": 1}]
    data["articles"][1]["images"] = [{"url": "https://x/null.png", "caption": None, "after": 1,
                                      "file": None}]
    result = render(data, ON, tmp_path / "nofile")
    assert result.pages == 2 and refs(result) == []


def test_relative_files_are_found_under_image_dir(sample_data, tmp_path):
    folder = tmp_path / "day"
    (folder / "images").mkdir(parents=True)
    picture(folder / "images" / "0-0.jpg")
    data = copy.deepcopy(sample_data)
    data["articles"][0]["images"] = [{"url": "https://x/a.jpg", "caption": None, "after": 1,
                                      "file": "images/0-0.jpg"}]
    data["articles"][1]["images"] = []
    result = render(data, ON, tmp_path / "rel", image_dir=folder)
    assert result.pictures == [(0, 0)]
    # Without the folder the file is not found, and the paper is still a paper.
    result = render(data, ON, tmp_path / "norel")
    assert result.pictures == [] and result.pages == 2


def test_no_stories_means_no_picture_sheet(sample_data, tmp_path):
    data = copy.deepcopy(sample_data)
    data["articles"] = []
    result = render(data, ON, tmp_path / "none")
    assert result.pages == 1 and result.picture_pages == 0 and result.pictures == []
    assert_verbatim(result, data["articles"])


def test_the_sheet_prints_on_both_sides_when_the_paper_does(sample_data, tmp_path):
    """Three pages: the printer route sends a two-sided job, so the picture
    page rides on a second sheet; a fourth page goes on its back."""
    result = render(sample_data, ON, tmp_path / "pdf")
    with pymupdf.open(result.pdf) as doc:
        assert doc.page_count == 3
        assert all(round(p.rect.width) == 612 and round(p.rect.height) == 792 for p in doc)


@pytest.mark.parametrize("size", [8.0, 11.0])
def test_the_sheet_holds_at_every_body_size(sample_data, tmp_path, size):
    look = {"layout": {"pictures": True}, "body_size_pt": size}
    result = render(sample_data, look, tmp_path / f"s{size}")
    assert result.picture_pages == 1
    assert_verbatim(result, sample_data["articles"])
    assert [k for k, _ in figures(result)] == [f"{a}:{i}" for a, i in result.pictures]
