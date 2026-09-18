"""
The Look tab may change every size, font and toggle in the paper; it may
never change a word of an article, and it may never change the sheet: two
pages, always, filled with whole articles and, at most, one last story
stopped at a paragraph boundary with a line saying where the rest of it is.
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


@pytest.fixture
def two_articles(sample_data):
    """The same sheet with a short queue: enough to fill page 1 and fill
    page 2 behind it, quick enough to lay out once per face."""
    return {**sample_data, "articles": sample_data["articles"][:2]}


def _check(data, look, tmp_path, name):
    result = render(data, look, tmp_path / name)
    assert result.pages == 2, f"{name}: {result.pages} pages"
    assert_verbatim(result, data["articles"])
    return result


@pytest.mark.parametrize("size", [8.0, 11.0])
@pytest.mark.parametrize("body_font", FONT_CHOICES_BODY)
def test_body_size_and_font(two_articles, tmp_path, size, body_font):
    """Every body face, at either end of the size range, still sets a sheet."""
    look = Look(body_size_pt=size, body_font=body_font)
    result = _check(two_articles, look, tmp_path, f"{body_font}-{size}".replace(" ", "_"))
    assert f'--body: "{body_font}"' in result.laid_out_html


@pytest.mark.parametrize("body_font", FONT_CHOICES_BODY)
def test_every_body_font_sets_the_whole_paper(sample_data, tmp_path, body_font):
    """At the default size, with the day's whole queue behind it."""
    result = _check(sample_data, Look(body_font=body_font), tmp_path,
                    f"body-{body_font}".replace(" ", "_"))
    assert f'--body: "{body_font}"' in result.laid_out_html


@pytest.mark.parametrize("headline_font", FONT_CHOICES_HEAD)
def test_headline_fonts(two_articles, tmp_path, headline_font):
    result = _check(two_articles, Look(headline_font=headline_font), tmp_path,
                    headline_font.replace(" ", "_"))
    assert f'--head: "{headline_font}"' in result.laid_out_html


@pytest.mark.parametrize("masthead_font", FONT_CHOICES_MASTHEAD)
def test_masthead_fonts(two_articles, tmp_path, masthead_font):
    result = _check(two_articles, Look(masthead_font=masthead_font), tmp_path,
                    masthead_font.replace(" ", "_"))
    assert f'--masthead: "{masthead_font}"' in result.laid_out_html


# ------------------------------------------------------- the one font table
def test_every_file_the_table_names_is_bundled():
    """`render/fontlist.py` is the contract; a name in it with no file
    behind it would print a fallback face without saying so."""
    from render import fontlist

    assert fontlist.missing_files() == []


@pytest.mark.parametrize("name", sorted({*FONT_CHOICES_BODY, *FONT_CHOICES_HEAD,
                                         *FONT_CHOICES_MASTHEAD}))
def test_every_choice_the_look_tab_offers_has_a_face(name):
    from render import fontlist

    assert fontlist.FONT_FILES.get(name), f"{name} is offered but has no files"
    assert fontlist.STACKS.get(name), f"{name} is offered but has no CSS stack"
    assert fontlist.stack(name).startswith(f'"{name}"')


def test_the_sheet_declares_every_bundled_face(sample_data):
    """The template loops over the table, so the page and the paper are
    set from the same files."""
    from render import fontlist

    html = build_html(sample_data, Look())
    for family, rules in fontlist.FONT_FILES.items():
        for file, weight, style in rules:
            assert f'font-family: "{family}"; font-weight: {weight}; font-style: {style}' in html
            assert f"/{file}" in html


def test_bigger_type_prints_fewer_articles(sample_data, tmp_path):
    """The sheet does not grow, so the type has to be paid for in stories.

    Never in words, though: at every size the sheet is filled, the stories on
    it are the author's, and the last of them may stop at a paragraph
    boundary with a line pointing to the rest.
    """
    counts, partials = {}, {}
    for size in (8.0, 9.0, 10.0, 11.0):
        result = _check(sample_data, Look(body_size_pt=size), tmp_path, f"size{size}")
        counts[size] = len(result.printed)
        partials[size] = result.partial
        assert result.printed, f"{size}pt printed nothing at all"
    ordered = [counts[s] for s in (8.0, 9.0, 10.0, 11.0)]
    assert ordered == sorted(ordered, reverse=True), counts
    assert counts[11.0] < counts[9.0], counts
    # At 11pt only the lead fits; alone on the front it takes the whole page,
    # whole or as far as it fits, rather than leaving a paper with no stories.
    assert counts[11.0] == 1, counts
    assert not partials[11.0] or list(partials[11.0]) == [0], partials[11.0]


@pytest.mark.parametrize("size", [8.0, 9.5, 11.0])
def test_the_sheet_is_never_empty_and_never_reworded(sample_data, tmp_path, size):
    """Whatever the type size, there is a story on the sheet, printed as written."""
    result = _check(sample_data, Look(body_size_pt=size), tmp_path, f"fill{size}")
    assert result.printed, f"{size}pt printed nothing at all"
    assert len(result.partial) <= 1
    for i, n in result.partial.items():
        assert i == result.printed[-1]
        assert 1 <= n <= len(sample_data["articles"][i]["paragraphs"])


@pytest.mark.parametrize("justify, align", [(True, "justify"), (False, "left")])
def test_justified_or_ragged_right(sample_data, tmp_path, justify, align):
    result = _check(sample_data, Look(justify=justify), tmp_path, f"align-{align}")
    assert f"--align: {align};" in result.laid_out_html
    # Both settings leave the words alone, marks and all.
    assert "hyphens: manual" in result.laid_out_html


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


def test_the_crossword_is_furniture_not_type(sample_data, tmp_path):
    """Clue size is the script's to set; the Look does not scale it."""
    small = render(sample_data, Look(body_size_pt=8.0), tmp_path / "xw8")
    big = render(sample_data, Look(body_size_pt=11.0), tmp_path / "xw11")
    for result in (small, big):
        assert "The Crossword" in result.laid_out_html
        assert "--xw-clue: 6.5pt" in result.laid_out_html


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
    # Page 2 is headed by the name the reader chose.
    assert html.index("The Evening Gull", html.index('id="page-2"')) > 0


def test_the_old_section_switches_still_hide_their_sections(sample_data, tmp_path):
    """A settings file from before the Look tab could arrange the paper.

    Its three switches are not the arrangement, but while they are set they
    still veto the sections they always vetoed.
    """
    result = render(sample_data, {"show_todo": False, "show_hourly": False, "show_notes": False},
                    tmp_path / "toggles")
    html = result.laid_out_html
    assert "<h3>To do</h3>" not in html
    assert "<h3>Hour by hour</h3>" not in html
    assert "<h3>Notes</h3>" not in html
    assert "<h3>Today</h3>" in html              # the agenda is not a toggle
    assert result.pages == 2
    assert_verbatim(result, sample_data["articles"])


@pytest.mark.skipif(not {"show_todo", "show_hourly", "show_notes"} <= set(Look.model_fields),
                    reason="the Look tab keeps its sections in look.layout now")
def test_section_toggles_hide_rail_sections(sample_data, tmp_path):
    look = Look(show_todo=False, show_hourly=False, show_notes=False)
    result = render(sample_data, look, tmp_path / "toggles-model")
    html = result.laid_out_html
    assert "<h3>To do</h3>" not in html
    assert "<h3>Hour by hour</h3>" not in html
    assert "<h3>Notes</h3>" not in html
    assert "<h3>Today</h3>" in html


# ------------------------------------------------- the reader's arrangement
#: The sections as the paper has always had them, to rearrange in the tests.
AGENDA = {"key": "agenda", "place": "rail"}
TASKS = {"key": "list:tasks", "place": "rail"}
HOURLY = {"key": "hourly", "place": "rail"}
NOTES = {"key": "notes", "place": "rail"}


def _layout(**over):
    """A look that is the default everywhere but in the layout."""
    return {"layout": over}


def _rail(html, where="#page-1 .rail"):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    return [h.get_text() for h in soup.select(f"{where} h3")]


def test_the_look_tabs_own_default_is_the_paper_as_it_was(sample_data, tmp_path):
    """`Look()` carries an arrangement now; it must be the old one, to the pixel."""
    plain = render(sample_data, None, tmp_path / "no-look", png=True)
    default = render(sample_data, Look(), tmp_path / "default-look", png=True)
    assert plain.printed == default.printed and plain.partial == default.partial
    assert [q.read_bytes() for q in plain.pngs] == [q.read_bytes() for q in default.pngs]
    assert _rail(default.laid_out_html) == ["Today", "To do", "Hour by hour", "Notes"]


def test_the_rail_is_set_in_the_order_the_reader_chose(sample_data, tmp_path):
    """Hour by hour first means Hour by hour at the top of the rail."""
    result = _check(sample_data, _layout(sections=[HOURLY, AGENDA, TASKS, NOTES]),
                    tmp_path, "reordered")
    rail = _rail(result.laid_out_html)
    assert rail[0] == "Hour by hour"
    assert rail == ["Hour by hour", "Today", "To do", "Notes"]


def test_the_notes_are_ruled_wherever_they_are_put(sample_data, tmp_path):
    """The filler measures the space the notes got, not the bottom of the rail."""
    from bs4 import BeautifulSoup

    result = _check(sample_data, _layout(sections=[NOTES, AGENDA, TASKS, HOURLY]),
                    tmp_path, "notes-first")
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert _rail(result.laid_out_html)[0] == "Notes"
    assert len(soup.select("#page-1 .rail .notes .lines div")) >= 3, "the notes were not ruled"


def test_a_section_can_be_moved_to_page_two(sample_data, tmp_path):
    from bs4 import BeautifulSoup

    result = _check(sample_data, _layout(sections=[AGENDA, dict(TASKS, place="page2"), HOURLY, NOTES]),
                    tmp_path, "list-on-2")
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    assert _rail(result.laid_out_html) == ["Today", "Hour by hour", "Notes"]
    assert _rail(result.laid_out_html, "#page-2 .page2-rail") == ["To do"]
    items = [li.get_text() for li in soup.select("#page-2 .page2-rail .todo li")]
    assert items == sample_data["lists"][0]["items"]
    assert result.pages == 2


def test_a_section_switched_off_is_nowhere_on_the_sheet(sample_data, tmp_path):
    result = _check(sample_data, _layout(sections=[AGENDA, TASKS, HOURLY, dict(NOTES, place="off")]),
                    tmp_path, "notes-off")
    assert _rail(result.laid_out_html) == ["Today", "To do", "Hour by hour"]
    assert "<h3>Notes</h3>" not in result.laid_out_html


def test_an_unknown_key_and_a_missing_list_are_skipped(sample_data, tmp_path):
    """A settings file this template does not understand still prints a paper."""
    result = _check(sample_data, _layout(sections=[
        {"key": "horoscope", "place": "rail"},          # no such section
        {"key": "list:errands", "place": "rail"},       # no such list in the data
        AGENDA,
    ]), tmp_path, "unknown")
    assert _rail(result.laid_out_html) == ["Today"]


def test_a_list_the_default_layout_leaves_out_can_be_asked_for(sample_data, tmp_path):
    result = _check(sample_data, _layout(sections=[AGENDA, TASKS, {"key": "list:groceries", "place": "rail"}]),
                    tmp_path, "groceries")
    assert _rail(result.laid_out_html) == ["Today", "To do", "Groceries"]
    for item in sample_data["lists"][1]["items"]:
        assert item in result.laid_out_html


@pytest.mark.parametrize("style, marker", [("checkbox", "box"), ("plain", None), ("numbered", "num")])
def test_list_styles_set_their_own_markers(sample_data, tmp_path, style, marker):
    import copy

    from bs4 import BeautifulSoup

    data = copy.deepcopy(sample_data)
    data["lists"][0]["style"] = style
    result = _check(data, None, tmp_path / "styles", style)
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    items = soup.select("#page-1 .rail .todo li")
    assert len(items) == len(data["lists"][0]["items"])
    for li, text in zip(items, data["lists"][0]["items"]):
        if marker is None:
            assert li.select(".box") == [] and li.select(".num") == []
            assert li.get_text() == text
        else:
            assert len(li.select(f".{marker}")) == 1
    if style == "numbered":
        assert [li.select_one(".num").get_text() for li in items] == \
            [f"{i}." for i in range(1, len(items) + 1)]


def test_the_rail_can_be_set_down_the_left_of_the_sheet(sample_data, tmp_path):
    """Rail side "left": the rail is written first on both pages, its rule
    moves to its right edge, and the sheet is still one sheet."""
    from bs4 import BeautifulSoup

    result = _check(
        sample_data,
        _layout(rail_side="left", sections=[AGENDA, HOURLY, dict(NOTES, place="page2")]),
        tmp_path, "leftrail",
    )
    soup = BeautifulSoup(result.laid_out_html, "html.parser")

    grid = soup.select_one("#page-1 .front-grid")
    assert "rail-left" in (grid.get("class") or [])
    kids = [k.name + "." + " ".join(k.get("class") or []) for k in grid.find_all(recursive=False)]
    assert kids[0].startswith("aside.rail"), kids
    assert any(k.startswith("div.stories") for k in kids[1:]), kids

    page2 = soup.select_one("#page-2 .page2-main")
    assert "rail-left" in (page2.get("class") or [])
    p2kids = [k.name + "." + " ".join(k.get("class") or []) for k in page2.find_all(recursive=False)]
    assert p2kids[0].startswith("aside.rail"), p2kids
    assert p2kids[1].startswith("div.cols"), p2kids

    # The columns swap and the rule between them swaps with them.
    assert ".front-grid.rail-left { grid-template-columns: var(--rail-w) 1fr; }" \
        in result.laid_out_html
    assert "border-right: 1px solid #000" in result.laid_out_html
    assert _rail(result.laid_out_html) == ["Today", "Hour by hour"]
    assert _rail(result.laid_out_html, "#page-2 .page2-rail") == ["Notes"]


def test_the_right_rail_is_the_default_and_is_written_last(sample_data, tmp_path):
    """Nothing is mirrored unless the reader asks: the default is untouched."""
    from bs4 import BeautifulSoup

    result = _check(sample_data, _layout(rail_side="right"), tmp_path, "rightrail")
    grid = BeautifulSoup(result.laid_out_html, "html.parser").select_one("#page-1 .front-grid")
    assert "rail-left" not in (grid.get("class") or [])
    kids = [k.name for k in grid.find_all(recursive=False)]
    assert kids == ["div", "aside"], kids


@pytest.mark.parametrize("width", [1.5, 2.8])
def test_the_rail_is_as_wide_as_the_reader_asked(sample_data, tmp_path, width):
    result = _check(sample_data, _layout(rail_width_in=width), tmp_path, f"w{width}")
    assert f"--rail-w: {width}in" in result.laid_out_html


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_front_stories_caps_the_front_page(sample_data, tmp_path, n):
    from bs4 import BeautifulSoup

    result = _check(sample_data, _layout(front_stories=n), tmp_path, f"front{n}")
    soup = BeautifulSoup(result.laid_out_html, "html.parser")
    stories = soup.select("#page-1 article.story")
    assert 1 <= len(stories) <= n
    assert len(result.printed) <= n
    if n == 1:
        # The lead alone: the solo slot, the whole page, and no second row.
        assert result.printed == [0]
        assert soup.select("#page-1 .row") == []
        assert "solo" in (soup.select_one("#page-1 .stories").get("class") or [])


def test_unknown_font_falls_back_to_the_default(sample_data, tmp_path):
    look = Look(body_font="Comic Sans MS", headline_font="Nonesuch", masthead_font="Nope")
    result = render(sample_data, look, tmp_path / "unknown")
    assert result.pages == 2
    html = result.laid_out_html
    assert "Comic Sans MS" not in html and "Nonesuch" not in html
    assert '--body: "PT Serif"' in html
    assert '--head: "Old Standard"' in html
    assert '--masthead: "Maguntia"' in html
    assert_verbatim(result, sample_data["articles"])


def test_render_accepts_a_plain_dict_or_none(sample_data):
    assert "Personal Paper" in build_html(sample_data, None)
    assert "The Tide Table" in build_html(sample_data, {"paper_name": "The Tide Table"})
