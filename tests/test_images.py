"""
`gather.images`: the stories' pictures fetched for the picture sheet, turned
grey and brought down to size, each one's `file` set only when it is really
on disk -- and nothing raised, whatever a picture does.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
import pytest

from gather import images


def png(width: int, height: int, *, alpha: bool = False, gray: bool = False) -> bytes:
    """A picture of the given size: a dark square in a light field."""
    space = pymupdf.csGRAY if gray else pymupdf.csRGB
    pix = pymupdf.Pixmap(space, pymupdf.IRect(0, 0, width, height), alpha)
    light = (200,) if gray else (200, 220, 240)
    dark = (40,) if gray else (60, 20, 20)
    pix.set_rect(pix.irect, light + ((255,) if alpha else ()))
    pix.set_rect(pymupdf.IRect(width // 4, height // 4, width // 2, height // 2),
                 dark + ((255,) if alpha else ()))
    return pix.tobytes("png")


@pytest.fixture
def served(monkeypatch):
    """Pictures from a dict instead of the network, by the address asked for."""
    store: dict[str, bytes] = {}
    asked: list[str] = []

    def fake_get(url: str) -> bytes:
        asked.append(url)
        if url not in store:
            raise ConnectionError(f"no such picture: {url}")
        return store[url]

    monkeypatch.setattr(images, "_get", fake_get)
    store["asked"] = asked  # type: ignore[assignment]
    return store


def article(*imgs: dict, title: str = "A story") -> dict:
    return {"title": title, "paragraphs": ["One.", "Two."], "images": list(imgs)}


def test_fetched_pictures_are_grey_jpegs_beside_the_data(served, tmp_path):
    served["https://cdn/a.png"] = png(640, 480)
    served["https://cdn/b.png"] = png(300, 500, alpha=True)
    articles = [article({"url": "https://cdn/a.png", "caption": "A", "after": 0}),
                article({"url": "https://cdn/b.png", "caption": None, "after": 1})]

    assert images.fetch(articles, tmp_path) == 2

    assert articles[0]["images"][0]["file"] == "images/0-0.jpg"
    assert articles[1]["images"][0]["file"] == "images/1-0.jpg"
    for a, name in ((0, "0-0.jpg"), (1, "1-0.jpg")):
        path = tmp_path / "images" / name
        assert path.is_file()
        pix = pymupdf.Pixmap(str(path))
        assert pix.n == 1 and not pix.alpha, "black only: the picture is grey"
    assert pymupdf.Pixmap(str(tmp_path / "images" / "0-0.jpg")).width == 640
    # Nothing else about the picture changed hands.
    assert articles[0]["images"][0]["caption"] == "A"


def test_a_big_picture_is_brought_down_to_the_sheets_size(served, tmp_path):
    served["https://cdn/big.png"] = png(4000, 1000)
    articles = [article({"url": "https://cdn/big.png", "caption": None, "after": 0})]
    images.fetch(articles, tmp_path)
    pix = pymupdf.Pixmap(str(tmp_path / "images" / "0-0.jpg"))
    assert max(pix.width, pix.height) <= images.MAX_SIDE_PX
    assert (pix.width, pix.height) == (1000, 250)      # halved twice, in proportion


def test_a_picture_that_fails_keeps_no_file_and_the_rest_are_fetched(served, tmp_path, caplog):
    served["https://cdn/ok.png"] = png(200, 200, gray=True)
    served["https://cdn/junk.jpg"] = b"<html>not a picture</html>"
    articles = [article(
        {"url": "https://cdn/missing.png", "caption": None, "after": 0},   # the fetch raises
        {"url": "https://cdn/junk.jpg", "caption": None, "after": 1},      # not a picture
        {"url": "https://cdn/ok.png", "caption": None, "after": 1},
    )]
    with caplog.at_level(logging.WARNING, logger="gather.images"):
        assert images.fetch(articles, tmp_path) == 1

    files = [im["file"] for im in articles[0]["images"]]
    assert files == [None, None, "images/0-2.jpg"]
    assert "picture 1 of 'A story' not fetched" in caplog.text
    assert "picture 2 of 'A story' not fetched" in caplog.text
    assert not (tmp_path / "images" / "0-1.jpg").exists()


def test_the_step_stops_at_the_cap_and_says_so(served, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(images, "MAX_IMAGES", 1)
    served["https://cdn/1.png"] = png(100, 100)
    served["https://cdn/2.png"] = png(100, 100)
    articles = [article({"url": "https://cdn/1.png", "caption": None, "after": 0}),
                article({"url": "https://cdn/2.png", "caption": None, "after": 0})]
    with caplog.at_level(logging.WARNING, logger="gather.images"):
        assert images.fetch(articles, tmp_path) == 1
    assert articles[0]["images"][0]["file"] == "images/0-0.jpg"
    assert articles[1]["images"][0]["file"] is None
    assert served["asked"] == ["https://cdn/1.png"]          # the second was never asked for
    assert "1 picture(s) not fetched" in caplog.text


def test_the_step_keeps_to_its_budget(served, tmp_path, monkeypatch):
    monkeypatch.setattr(images, "BUDGET_S", -1.0)             # already spent
    served["https://cdn/1.png"] = png(100, 100)
    articles = [article({"url": "https://cdn/1.png", "caption": None, "after": 0})]
    assert images.fetch(articles, tmp_path) == 0
    assert articles[0]["images"][0]["file"] is None
    assert served["asked"] == []


def test_nothing_to_fetch_is_nothing_done(served, tmp_path):
    assert images.fetch([article(), {"title": "no images key", "paragraphs": []}], tmp_path) == 0
    assert images.fetch([], tmp_path) == 0
    assert not (tmp_path / "images").exists()


def test_substacks_cdn_is_asked_for_a_format_the_paper_can_read():
    webp = ("https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp,q_auto:good,"
            "fl_progressive:steep/https%3A%2F%2Fbucket%2Fa.png")
    assert images.fetch_url(webp) == (
        "https://substackcdn.com/image/fetch/w_1456,c_limit,f_auto,q_auto:good,"
        "fl_progressive:steep/https%3A%2F%2Fbucket%2Fa.png"
    )
    # Any other address is asked for exactly as it was given.
    assert images.fetch_url("https://example.com/a.webp") == "https://example.com/a.webp"
    assert images.fetch_url("") == ""


def test_save_grey_refuses_what_is_not_a_picture(tmp_path):
    with pytest.raises(Exception):
        images.save_grey(b"nope", tmp_path / "x.jpg")
    assert not (tmp_path / "x.jpg").exists()


def test_planned_lists_what_would_be_fetched():
    rows = images.planned([article({"url": "https://cdn/a.png", "caption": "A", "after": 1},
                                   title="T")])
    assert rows == [{"article": 0, "title": "T", "after": 1, "url": "https://cdn/a.png",
                     "fetch": "https://cdn/a.png", "caption": "A"}]
