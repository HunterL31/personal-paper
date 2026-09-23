"""The PWG Raster encoder, checked against the spec and against itself.

No printer is involved: every page is encoded, then read back with the
module's own decoder, and the header fields are also read straight out of the
bytes at the offsets PWG 5102.4 Table 1 gives them, so a wrong offset cannot
hide behind a matching pair of encoder and decoder.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from deliver import pwg

# The rectangle drawn on every test page, in points, and where it lands at
# 300 dpi (points x 300/72).
RECT = (72, 72, 288, 216)
SCALE = 300 / 72


@pytest.fixture
def two_pages(tmp_path: Path) -> Path:
    """Two US Letter pages, each with a solid black rectangle and a line."""
    import pymupdf

    path = tmp_path / "2026-09-16.pdf"
    with pymupdf.open() as document:
        for number in (1, 2):
            page = document.new_page(width=612, height=792)
            page.draw_rect(pymupdf.Rect(*RECT), color=None, fill=(0, 0, 0))
            page.insert_text((72, 400), f"Page {number} of the paper", fontsize=24)
        document.save(path)
    return path


@pytest.fixture
def blank_page(tmp_path: Path) -> Path:
    import pymupdf

    path = tmp_path / "blank.pdf"
    with pymupdf.open() as document:
        document.new_page(width=612, height=792)
        document.save(path)
    return path


@pytest.fixture
def picture_and_type(tmp_path: Path) -> Path:
    """Page 1 carries a photograph (a mid-grey raster) and a line of type;
    page 2 the same grey as a drawn rectangle, which is not a photograph."""
    import pymupdf

    path = tmp_path / "pictures.pdf"
    grey = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 200, 200), False)
    grey.set_rect(grey.irect, (128,))
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_image(pymupdf.Rect(*RECT), pixmap=grey)
        page.insert_text((72, 400), "Type beside a picture", fontsize=24)
        page = document.new_page(width=612, height=792)
        page.draw_rect(pymupdf.Rect(*RECT), color=None, fill=(0.5, 0.5, 0.5))
        document.save(path)
    return path


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _ink_fraction(page, rect=RECT, step: int = 3) -> float:
    """How much of the rectangle (points) is ink, sampled every `step` pixels."""
    x0, y0, x1, y1 = (int(v * SCALE) for v in rect)
    inside = [(x, y) for y in range(y0 + 8, y1 - 8, step) for x in range(x0 + 8, x1 - 8, step)]
    return sum(page.black(x, y) for x, y in inside) / len(inside)


# ------------------------------------------------------------------ the stream
def test_black_1_page_headers_and_bitmap(two_pages):
    data = pwg.encode(two_pages, dpi=300, color="black_1", duplex=True)

    assert data[:4] == b"RaS2"
    pages = pwg.decode_pages(data)
    assert len(pages) == 2

    header = pages[0].header
    assert header.magic == "PwgRaster"
    assert (header.width, header.height) == (2550, 3300)  # US Letter at 300 dpi
    assert header.bits_per_color == 1 and header.bits_per_pixel == 1
    assert header.bytes_per_line == 319  # (2550 * 1 + 7) // 8
    assert header.color_space == 3  # Black: 1 is ink
    assert header.num_colors == 1
    assert header.color_order == 0  # chunky
    assert header.resolution == (300, 300)
    assert header.page_size == (612, 792)
    assert header.page_size_name == "na_letter_8.5x11in"
    assert header.duplex is True and header.tumble is False
    assert header.cross_feed_transform == 1 and header.feed_transform == 1
    assert header.total_page_count == 0  # not known when the file is written
    assert header.media_color == "" and header.media_type == ""
    assert header.print_content_optimize == "text"

    # The rectangle is ink and the rest of the sheet is paper.
    inside = (int(150 * SCALE), int(120 * SCALE))
    assert pages[0].black(*inside) is True
    assert pages[1].black(*inside) is True
    for outside in ((10, 10), (2500, 3200), (int(400 * SCALE), int(600 * SCALE))):
        assert pages[0].black(*outside) is False

    # Every line is there, and every line is a full line.
    assert len(pages[0].lines) == 3300
    assert {len(line) for line in pages[0].lines} == {319}


def test_sgray_8_page_headers_and_bitmap(two_pages):
    data = pwg.encode(two_pages, dpi=300, color="sgray_8", duplex=False)

    pages = pwg.decode_pages(data)
    header = pages[0].header
    assert (header.width, header.height) == (2550, 3300)
    assert header.bits_per_color == 8 and header.bits_per_pixel == 8
    assert header.bytes_per_line == 2550
    assert header.color_space == 18  # Sgray: 0 is black, 255 is white
    assert header.num_colors == 1
    assert header.duplex is False

    inside = (int(150 * SCALE), int(120 * SCALE))
    assert pages[0].lines[inside[1]][inside[0]] == 0  # black
    assert pages[0].lines[10][10] == 255  # paper
    assert pages[0].black(*inside) is True
    assert pages[0].black(10, 10) is False


def test_the_header_fields_sit_where_the_spec_puts_them(two_pages):
    """PWG 5102.4 Table 1, read out of the raw octets."""
    data = pwg.encode(two_pages, dpi=600, color="black_1", duplex=True)
    header = data[4 : 4 + pwg.HEADER_SIZE]

    assert len(header) == 1796
    assert header[0:9] == b"PwgRaster" and header[9] == 0  # CString, NUL padded
    assert header[64:128] == bytes(64)  # MediaColor, empty
    assert header[128:192] == bytes(64)  # MediaType, empty
    assert header[192:196] == b"text"  # PrintContentOptimize
    assert _u32(header, 272) == 1  # Duplex
    assert (_u32(header, 276), _u32(header, 280)) == (600, 600)  # HWResolution
    assert _u32(header, 352) == 612 and _u32(header, 356) == 792  # PageSize
    assert _u32(header, 368) == 0  # Tumble
    assert _u32(header, 372) == 5100  # Width, US Letter at 600 dpi
    assert _u32(header, 376) == 6600  # Height
    assert _u32(header, 384) == 1  # BitsPerColor
    assert _u32(header, 388) == 1  # BitsPerPixel
    assert _u32(header, 392) == 638  # BytesPerLine
    assert _u32(header, 396) == 0  # ColorOrder, chunky
    assert _u32(header, 400) == 3  # ColorSpace, Black
    assert _u32(header, 420) == 1  # NumColors
    assert _u32(header, 340) == 1  # NumCopies
    assert _u32(header, 344) == 0  # Orientation
    assert _u32(header, 452) == 0  # TotalPageCount, not known
    assert _u32(header, 456) == 1 and _u32(header, 460) == 1  # the transforms
    # ImageBox, in pixels: the whole page image.
    assert [_u32(header, 464 + 4 * i) for i in range(4)] == [0, 0, 5099, 6599]
    assert _u32(header, 480) == 0  # AlternatePrimary: black ink
    assert _u32(header, 484) == 0  # PrintQuality: the printer's own default
    assert header[1732:1750].rstrip(b"\0") == b"na_letter_8.5x11in"  # PageSizeName

    # Reserved ranges stay zero, except the ImagingBoundingBox CUPS fills in.
    for start, end in [
        (256, 268), (312, 324), (332, 340), (348, 352), (360, 368),
        (380, 384), (404, 420), (424, 452), (488, 508), (516, 1668),
    ]:
        assert header[start:end] == bytes(end - start), f"reserved {start}-{end - 1}"
    # ImagingBoundingBox, in points: the whole sheet.
    assert [_u32(header, 284 + 4 * i) for i in range(4)] == [0, 0, 612, 792]


def test_the_duplex_flag_follows_the_job(two_pages):
    simplex = pwg.decode_pages(pwg.encode(two_pages, dpi=300, color="black_1", duplex=False))
    assert [page.header.duplex for page in simplex] == [False, False]

    duplex = pwg.decode_pages(pwg.encode(two_pages, dpi=300, color="black_1", duplex=True))
    assert [page.header.duplex for page in duplex] == [True, True]


def test_a_rotated_sheet_back_turns_the_second_side_over(two_pages):
    """A printer whose back side comes out upside down gets it upside down."""
    pages = pwg.decode_pages(
        pwg.encode(two_pages, dpi=300, color="black_1", duplex=True, sheet_back="rotated")
    )

    front, back = pages
    assert (front.header.cross_feed_transform, front.header.feed_transform) == (1, 1)
    assert (back.header.cross_feed_transform, back.header.feed_transform) == (-1, -1)

    x, y = int(150 * SCALE), int(120 * SCALE)
    assert front.black(x, y) is True
    assert back.black(x, y) is False
    assert back.black(2550 - 1 - x, 3300 - 1 - y) is True  # the same ink, turned


def test_a_blank_page_is_a_few_hundred_octets(blank_page):
    """Identical lines are sent once: an empty sheet must not cost a megabyte."""
    data = pwg.encode(blank_page, dpi=300, color="black_1", duplex=False)

    assert len(data) < 5 * 1024
    assert len(data) - 4 - pwg.HEADER_SIZE < 200  # the bitmap itself
    page = pwg.decode_pages(data)[0]
    assert len(page.lines) == 3300
    assert not any(page.black(x, y) for x in (0, 1275, 2549) for y in (0, 1650, 3299))

    grey = pwg.encode(blank_page, dpi=300, color="sgray_8", duplex=False)
    assert len(grey) < 5 * 1024


# ------------------------------------------------------- the line encoding
def test_a_line_is_runs_and_literals(two_pages):
    """PWG 5102.4 section 4.4, with the spec's own worked example."""
    # "1 to 128 repeated colors": count - 1, then the color.
    assert pwg._encode_line(b"\xff\xff\xff") == b"\x02\xff"
    # "2 to 128 non-repeating colors": 257 - count, then the colors.
    assert pwg._encode_line(b"\x8f\x78\xf7") == b"\xfe\x8f\x78\xf7"
    # A single color of its own is a run of one, because 257 - 1 will not fit.
    assert pwg._encode_line(b"\x42") == b"\x00\x42"


def test_a_page_with_a_photograph_is_halftoned_and_a_page_of_type_is_not(picture_and_type):
    """A threshold turns a mid grey into a black blot; a page that carries a
    picture is dithered instead, so the grey prints as half ink, half
    paper. The page of type keeps the plain threshold it always had."""
    data = pwg.encode(picture_and_type, dpi=300, color="black_1", duplex=False)
    photo, drawn = pwg.decode_pages(data)

    assert 0.4 < _ink_fraction(photo) < 0.6       # dots: about half of them ink
    assert _ink_fraction(drawn) == 1.0            # thresholded, as before
    # A mid grey dithers to a checkerboard: no two neighbours alike, in
    # either direction. The type on the picture page is still there, solid.
    x0, y0 = int(RECT[0] * SCALE) + 20, int(RECT[1] * SCALE) + 20
    assert all(photo.black(x0 + i, y0) != photo.black(x0 + i + 1, y0) for i in range(8))
    assert all(photo.black(x0, y0 + r) != photo.black(x0, y0 + r + 1) for r in range(8))
    type_band = [photo.black(x, y) for y in range(int(385 * SCALE), int(400 * SCALE), 2)
                 for x in range(int(72 * SCALE), int(300 * SCALE), 2)]
    assert 0.05 < sum(type_band) / len(type_band) < 0.6

    # 8-bit grey is the printer's to halftone: nothing here changes it.
    grey = pwg.decode_pages(pwg.encode(picture_and_type, dpi=300, color="sgray_8", duplex=False))
    assert grey[0].lines[int(150 * SCALE)][int(150 * SCALE)] == 128


@pytest.mark.parametrize("value, expected", [(0, 1.0), (128, 0.5), (200, 14 / 64), (255, 0.0)])
def test_the_halftone_inks_a_grey_in_proportion(value, expected):
    """Over one 8x8 cell of the matrix, the share of ink is the share of
    black in the grey: all of it for black, none for white, in between for
    the greys between."""
    row = bytes([value]) * 64
    inked = sum(pwg._halftone_bits(row, y).count(b"1") for y in range(8))
    assert inked / (64 * 8) == pytest.approx(expected)


def test_identical_lines_carry_a_repeat_count():
    line = b"\x8f\x78\xf7"
    other = b"\x00\x00\x00"

    assert pwg._encode_lines([line, line, line, other]) == (
        b"\x02" + b"\xfe\x8f\x78\xf7" + b"\x00" + b"\x02\x00"
    )


@pytest.mark.parametrize(
    "line",
    [
        b"\x00" * 1000,  # runs longer than 128
        bytes(range(256)) * 2,  # literals longer than 128
        b"\xff" * 130 + bytes(range(200)) + b"\x00" * 3,
        bytes(range(128)) + b"\x7f" * 5 + bytes(range(3)),
    ],
)
def test_every_line_decodes_to_what_it_was(line):
    encoded = pwg._encode_line(line)
    decoded, offset = pwg._decode_line(encoded, 0, len(line))

    assert decoded == line
    assert offset == len(encoded)


def test_an_unsupported_page_type_is_refused(two_pages):
    with pytest.raises(ValueError, match="cmyk_8"):
        pwg.encode(two_pages, dpi=300, color="cmyk_8", duplex=False)

    with pytest.raises(ValueError):
        pwg.encode(two_pages, dpi=0, color="black_1", duplex=False)


def test_a_stream_without_the_sync_word_is_not_a_stream():
    with pytest.raises(ValueError, match="RaS2"):
        pwg.decode_pages(b"%PDF-1.7\n")
