"""
The Look tab may change every size, font and toggle in the paper; it may
never change a word of an article, and it may not blow the page count out.
"""
from __future__ import annotations

import pytest

from app.settings import (
    FONT_CHOICES_BODY,
    FONT_CHOICES_HEAD,
    FONT_CHOICES_MASTHEAD,
    Look,
)
from render.render import build_html, render
from tests.verbatim import assert_verbatim

MAX_PAGES = 5


def _check(data, look, tmp_path, name):
    result = render(data, look, tmp_path / name)
    assert result.pages <= MAX_PAGES, f"{name}: {result.pages} pages"
    assert_verbatim(result.laid_out_html, data["articles"])
    return result


@pytest.mark.parametrize("size", [8.0, 11.0])
@pytest.mark.parametrize("body_font", FONT_CHOICES_BODY)
def test_body_size_and_font(sample_data, tmp_path, size, body_font):
    look = Look(body_size_pt=size, body_font=body_font)
    _check(sample_data, look, tmp_path, f"{body_font}-{size}".replace(" ", "_"))


@pytest.mark.parametrize("headline_font", FONT_CHOICES_HEAD)
def test_headline_fonts(sample_data, tmp_path, headline_font):
    look = Look(headline_font=headline_font)
    _check(sample_data, look, tmp_path, headline_font.replace(" ", "_"))


@pytest.mark.parametrize("masthead_font", FONT_CHOICES_MASTHEAD)
def test_masthead_fonts(sample_data, tmp_path, masthead_font):
    look = Look(masthead_font=masthead_font)
    result = _check(sample_data, look, tmp_path, masthead_font.replace(" ", "_"))
    assert f'--masthead: "{masthead_font}"' in result.laid_out_html


def test_default_look_matches_the_original_sizes(sample_data):
    """9pt is the baseline: the scale must reproduce the original type sizes."""
    html = build_html(sample_data, Look())
    for size in ("--body-size:   9.0pt", "--lead-head:   27.0pt", "--row-head:    14.0pt",
                 "--rail-text:   8.5pt", "--lead-body-height: 2.7in"):
        assert size in html, size


def test_sizes_scale_with_the_body(sample_data):
    html = build_html(sample_data, Look(body_size_pt=10.5))
    assert "--body-size:   10.5pt" in html
    assert "--lead-head:   31.5pt" in html      # 27 * 10.5/9
    assert "--rail-text:   9.917pt" in html


def test_look_supplies_name_imprint_price_and_ears(sample_data, tmp_path):
    look = Look(
        paper_name="The Evening Gull",
        imprint="Set in the kitchen",
        price="Two cents",
    )
    look.ear.initials = "A. B."
    look.ear.lines = ["Stories continue inside.", "No crossword today."]
    result = render(sample_data, look, tmp_path / "ears")
    html = result.laid_out_html
    assert "The Evening Gull" in html and "Set in the kitchen" in html and "Two cents" in html
    assert "A. B." in html and "Stories continue inside." in html and "No crossword today." in html
    # The data's own paper.name is overridden by the Look; volume and date are not.
    assert sample_data["paper"]["volume"] in html
    assert sample_data["paper"]["date"] in html
    assert "M. L." not in html
    assert "The crossword is on the back page." not in html
    # The left ear stays the weather.
    assert sample_data["weather"]["summary"] in html


def test_section_toggles_hide_rail_sections(sample_data, tmp_path):
    look = Look(show_todo=False, show_hourly=False, show_notes=False)
    result = render(sample_data, look, tmp_path / "toggles")
    html = result.laid_out_html
    assert "<h3>To do</h3>" not in html
    assert "<h3>Hour by hour</h3>" not in html
    assert "<h3>Notes</h3>" not in html
    assert "<h3>Today</h3>" in html              # the agenda is not a toggle
    assert result.pages <= MAX_PAGES
    assert_verbatim(html, sample_data["articles"])


def test_unknown_font_falls_back_to_the_default(sample_data, tmp_path):
    look = Look(body_font="Comic Sans MS", headline_font="Nonesuch", masthead_font="Nope")
    result = render(sample_data, look, tmp_path / "unknown")
    assert result.pages <= MAX_PAGES
    html = result.laid_out_html
    assert "Comic Sans MS" not in html and "Nonesuch" not in html
    assert '--body: "PT Serif"' in html
    assert '--head: "Old Standard"' in html
    assert '--masthead: "Maguntia"' in html
    assert_verbatim(html, sample_data["articles"])


def test_render_accepts_a_plain_dict_or_none(sample_data):
    assert "The Molly Ledger" in build_html(sample_data, None)
    assert "The Tide Table" in build_html(sample_data, {"paper_name": "The Tide Table"})
