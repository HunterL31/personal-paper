"""PWG Raster (PWG 5102.4), for printers that will not take a PDF.

An AirPrint-class printer is only required to accept raster pages, not PDF:
the owner's Brother HL-L2460DW lists `image/pwg-raster` and `image/urf` and
no `application/pdf` at all. So the paper is rendered here, page by page,
into the open raster format the printer does take -- no CUPS, no Ghostscript,
just pymupdf for the pixels and this module for the container.

A stream is the four-byte sync word `RaS2` followed, for each page, by a
1796-byte header and the page's compressed lines. The header's field offsets
are PWG 5102.4 Table 1 (`_OFFSETS` below, which is that table); the line data
is the PackBits-like encoding of section 4.4: a line-repeat count, then runs
of pixels, all counts stored as "count - 1" or "257 - count".

`decode_header` and `decode_pages` read a stream back, so the encoder is
checked against its own reading of the spec rather than against a printer.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

#: PWG 5102.4 section 4.2: the stream begins with this and nothing else.
SYNC_WORD = b"RaS2"

#: PWG 5102.4 section 4.3: every page header is exactly this long.
HEADER_SIZE = 1796

#: The string the first CString field must hold (section 4.3.2.1).
MAGIC = "PwgRaster"

#: PWG 5102.4 Table 12, for the two page types a black-only newspaper needs:
#: keyword -> (BitsPerColor, BitsPerPixel, ColorSpace, NumColors).
#: ColorSpace 3 is Black (1 = ink), 18 is Sgray (0 = black, 255 = white).
TYPES: dict[str, tuple[int, int, int, int]] = {
    "black_1": (1, 1, 3, 1),
    "sgray_8": (8, 8, 18, 1),
}

#: ColorOrderEnum: chunky is the only value the format defines (Table 2).
COLOR_ORDER_CHUNKY = 0

#: Byte offsets of every field this module writes, from PWG 5102.4 Table 1.
#: Everything not named here is a Reserved range and stays zero, except the
#: ImagingBoundingBox at 284-299, which the spec reserves but CUPS fills in
#: on the way out; we fill it the same way, with the whole sheet.
_OFFSETS = {
    "PwgRaster": 0,               # CString, "PwgRaster"
    "MediaColor": 64,             # CString, empty = the printer's default
    "MediaType": 128,             # CString, empty = the printer's default
    "PrintContentOptimize": 192,  # CString
    "CutMedia": 268,              # WhenEnum
    "Duplex": 272,                # Boolean
    "HWResolution": 276,          # UnsignedInteger x 2 (cross-feed, feed)
    "ImagingBoundingBox": 284,    # UnsignedInteger x 4, points (l, b, r, t)
    "InsertSheet": 300,           # Boolean
    "Jog": 304,                   # WhenEnum
    "LeadingEdge": 308,           # EdgeEnum
    "MediaPosition": 324,         # MediaPositionEnum
    "MediaWeightMetric": 328,     # UnsignedInteger
    "NumCopies": 340,             # UnsignedInteger
    "Orientation": 344,           # OrientationEnum
    "PageSize": 352,              # UnsignedInteger x 2, points
    "Tumble": 368,                # Boolean
    "Width": 372,                 # UnsignedInteger, pixels
    "Height": 376,                # UnsignedInteger, pixels
    "BitsPerColor": 384,          # UnsignedInteger
    "BitsPerPixel": 388,          # UnsignedInteger
    "BytesPerLine": 392,          # UnsignedInteger
    "ColorOrder": 396,            # ColorOrderEnum
    "ColorSpace": 400,            # ColorSpaceEnum
    "NumColors": 420,             # UnsignedInteger
    "TotalPageCount": 452,        # UnsignedInteger, 0 = not known
    "CrossFeedTransform": 456,    # Integer, 1 or -1
    "FeedTransform": 460,         # Integer, 1 or -1
    "ImageBoxLeft": 464,          # UnsignedInteger, pixels
    "ImageBoxTop": 468,           # UnsignedInteger, pixels
    "ImageBoxRight": 472,         # UnsignedInteger, pixels
    "ImageBoxBottom": 476,        # UnsignedInteger, pixels
    "AlternatePrimary": 480,      # SrgbColor; 0 = black ink
    "PrintQuality": 484,          # PrintQualityEnum; 0 = the printer's default
    "VendorIdentifier": 508,      # UnsignedInteger
    "VendorLength": 512,          # UnsignedInteger
    "RenderingIntent": 1668,      # CString
    "PageSizeName": 1732,         # CString, PWG 5101.1 name
}

#: The sheet the paper is always printed on, in points, and its PWG 5101.1
#: name; A4 is here only so a page of another size is not left unnamed.
LETTER = (612, 792)
A4 = (595, 842)
_SIZE_NAMES = {LETTER: "na_letter_8.5x11in", A4: "iso_a4_210x297mm"}

#: What `PwgRasterDocumentSheetBack` says the back of a duplex sheet needs.
SHEET_BACKS = ("normal", "rotated", "flipped", "manual-tumble")

#: Gray byte -> "1" when it is ink (below half) and "0" when it is paper.
#: `int(s, 2)` then packs a whole line's bits in one C-level step.
_THRESHOLD = bytes.maketrans(bytes(range(256)), b"1" * 128 + b"0" * 128)


# ----------------------------------------------------------------- encoding
def _string_into(header: bytearray, name: str, text: str) -> None:
    """Write a CString: up to 63 US-ASCII characters, then a NUL."""
    offset = _OFFSETS[name]
    raw = text.encode("ascii", "ignore")[:63]
    header[offset : offset + len(raw)] = raw


def _ints_into(header: bytearray, name: str, *values: int) -> None:
    """Write one or more 32-bit fields, in network byte order.

    Signed fields (the two transforms) are written as two's complement, which
    is what `& 0xFFFFFFFF` does and what a reader of an Integer field expects.
    """
    offset = _OFFSETS[name]
    for index, value in enumerate(values):
        struct.pack_into(">I", header, offset + 4 * index, int(value) & 0xFFFFFFFF)


def _page_header(
    *,
    width: int,
    height: int,
    color: str,
    dpi: int,
    page_size: tuple[int, int],
    duplex: bool,
    tumble: bool,
    cross_feed_transform: int = 1,
    feed_transform: int = 1,
    total_pages: int = 0,
    optimize: str = "text",
) -> bytes:
    """One 1796-byte PWG Raster page header."""
    bits_per_color, bits_per_pixel, color_space, num_colors = TYPES[color]
    bytes_per_line = (width * bits_per_pixel + 7) // 8

    header = bytearray(HEADER_SIZE)
    _string_into(header, "PwgRaster", MAGIC)
    _string_into(header, "MediaColor", "")
    _string_into(header, "MediaType", "")
    _string_into(header, "PrintContentOptimize", optimize)
    _string_into(header, "RenderingIntent", "")
    _string_into(header, "PageSizeName", _SIZE_NAMES.get(page_size, ""))

    _ints_into(header, "CutMedia", 0)
    _ints_into(header, "Duplex", 1 if duplex else 0)
    _ints_into(header, "HWResolution", dpi, dpi)
    # Points, (left, bottom, right, top): the whole sheet, because the page
    # image is full bleed and the printer's own margins are not known here.
    _ints_into(header, "ImagingBoundingBox", 0, 0, page_size[0], page_size[1])
    _ints_into(header, "InsertSheet", 0)
    _ints_into(header, "Jog", 0)
    _ints_into(header, "LeadingEdge", 0)
    _ints_into(header, "MediaPosition", 0)
    _ints_into(header, "MediaWeightMetric", 0)
    _ints_into(header, "NumCopies", 1)
    _ints_into(header, "Orientation", 0)
    _ints_into(header, "PageSize", page_size[0], page_size[1])
    _ints_into(header, "Tumble", 1 if tumble else 0)

    _ints_into(header, "Width", width)
    _ints_into(header, "Height", height)
    _ints_into(header, "BitsPerColor", bits_per_color)
    _ints_into(header, "BitsPerPixel", bits_per_pixel)
    _ints_into(header, "BytesPerLine", bytes_per_line)
    _ints_into(header, "ColorOrder", COLOR_ORDER_CHUNKY)
    _ints_into(header, "ColorSpace", color_space)
    _ints_into(header, "NumColors", num_colors)

    _ints_into(header, "TotalPageCount", total_pages)
    _ints_into(header, "CrossFeedTransform", cross_feed_transform)
    _ints_into(header, "FeedTransform", feed_transform)
    _ints_into(header, "ImageBoxLeft", 0)
    _ints_into(header, "ImageBoxTop", 0)
    _ints_into(header, "ImageBoxRight", max(width - 1, 0))
    _ints_into(header, "ImageBoxBottom", max(height - 1, 0))
    _ints_into(header, "AlternatePrimary", 0)
    _ints_into(header, "PrintQuality", 0)
    _ints_into(header, "VendorIdentifier", 0)
    _ints_into(header, "VendorLength", 0)
    return bytes(header)


def _pack_line(gray: bytes, color: str, bytes_per_line: int) -> bytes:
    """One row of 8-bit gray as the page's own pixels, in one line of octets."""
    if color == "sgray_8":
        return gray.ljust(bytes_per_line, b"\xff")  # pad with paper, not ink
    # black_1: 8 pixels to the octet, most significant bit first, 1 = ink.
    # The trailing bits of the last octet are paper, which also makes a blank
    # line a run of identical 0x00 octets.
    bits = gray.translate(_THRESHOLD).decode("ascii")
    bits += "0" * (bytes_per_line * 8 - len(bits))
    return int(bits, 2).to_bytes(bytes_per_line, "big")


def _encode_line(line: bytes) -> bytes:
    """PWG 5102.4 section 4.4, one line: runs and literals of whole colors.

    A run of 1..128 identical colors is `count - 1` then the color; a stretch
    of 2..128 non-repeating colors is `257 - count` then the colors. 128 is
    not used, and a single leftover color is written as a run of one.
    """
    out = bytearray()
    literal = bytearray()

    def flush() -> None:
        if not literal:
            return
        if len(literal) == 1:
            out.append(0)
            out.append(literal[0])
        else:
            out.append(257 - len(literal))
            out.extend(literal)
        literal.clear()

    for value, run in groupby(line):
        count = sum(1 for _ in run)
        if count == 1:
            literal.append(value)
            if len(literal) == 128:
                flush()
            continue
        flush()
        while count:
            step = min(count, 128)
            out.append(step - 1)
            out.append(value)
            count -= step
    flush()
    return bytes(out)


def _encode_lines(lines: list[bytes]) -> bytes:
    """Every line of a page, with identical neighbours sent once.

    A line begins with its repeat count as `count - 1`, so up to 256 identical
    lines cost one extra octet -- which is most of a newspaper page's margins.
    """
    out = bytearray()
    for line, group in groupby(lines):
        repeats = sum(1 for _ in group)
        encoded = _encode_line(line)
        while repeats:
            step = min(repeats, 256)
            out.append(step - 1)
            out += encoded
            repeats -= step
    return bytes(out)


def _gray_lines(page, dpi: int) -> tuple[int, int, list[bytes]]:
    """Render one PDF page to 8-bit gray, one `bytes` per row."""
    import pymupdf

    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    samples = pixmap.samples
    stride = pixmap.stride
    width = pixmap.width
    return (
        width,
        pixmap.height,
        [samples[y * stride : y * stride + width] for y in range(pixmap.height)],
    )


def _turn_back_side(
    lines: list[bytes], sheet_back: str, tumble: bool
) -> tuple[list[bytes], int, int]:
    """Orient the back of a sheet the way the printer says it feeds it.

    `PwgRasterDocumentSheetBack` says where the back side's origin is; the
    page image is turned to match and the two transform fields record it, as
    CUPS does. Rows are reversed for a feed-direction flip and each row's
    pixels for a cross-feed flip -- done on the gray rows, before packing, so
    it costs nothing and cannot get the bit order wrong.
    """
    if sheet_back == "rotated" or (sheet_back == "manual-tumble" and tumble):
        return [row[::-1] for row in reversed(lines)], -1, -1
    if sheet_back == "flipped":
        if tumble:
            return [row[::-1] for row in lines], -1, 1
        return list(reversed(lines)), 1, -1
    return lines, 1, 1


def encode(
    pdf: Path,
    *,
    dpi: int,
    color: str,
    duplex: bool,
    tumble: bool = False,
    sheet_back: str = "normal",
) -> bytes:
    """Render `pdf` and return it as one PWG Raster stream.

    `color` is a PWG type keyword (`black_1` or `sgray_8`), `dpi` the
    resolution the printer listed, `duplex`/`tumble` the sides it will be
    printed on. Every page is rendered full bleed at `dpi`, so US Letter at
    300 dpi is 2550x3300 pixels.
    """
    if color not in TYPES:
        raise ValueError(f"Unsupported PWG raster type: {color!r}")
    if dpi <= 0:
        raise ValueError(f"Unsupported PWG raster resolution: {dpi!r}")
    sheet_back = (sheet_back or "normal").lower()
    if sheet_back not in SHEET_BACKS:
        _LOGGER.warning(
            "unknown sheet-back %r; sending the back sides the usual way up",
            sheet_back,
        )
        sheet_back = "normal"

    import pymupdf

    bits_per_pixel = TYPES[color][1]
    out = bytearray(SYNC_WORD)

    with pymupdf.open(pdf) as document:
        for number, page in enumerate(document, start=1):
            width, height, lines = _gray_lines(page, dpi)
            cross_feed, feed = 1, 1
            if duplex and number % 2 == 0:
                lines, cross_feed, feed = _turn_back_side(lines, sheet_back, tumble)

            rect = page.rect
            page_size = (round(rect.width), round(rect.height))
            bytes_per_line = (width * bits_per_pixel + 7) // 8

            out += _page_header(
                width=width,
                height=height,
                color=color,
                dpi=dpi,
                page_size=page_size,
                duplex=duplex,
                tumble=tumble,
                cross_feed_transform=cross_feed,
                feed_transform=feed,
            )
            out += _encode_lines(
                [_pack_line(row, color, bytes_per_line) for row in lines]
            )

    _LOGGER.debug(
        "encoded %s as %d bytes of PWG raster (%d dpi, %s)", pdf, len(out), dpi, color
    )
    return bytes(out)


# ----------------------------------------------------------------- decoding
@dataclass
class PageHeader:
    """A page header read back out of a stream, in the spec's own names."""

    magic: str
    media_color: str
    media_type: str
    print_content_optimize: str
    width: int
    height: int
    bits_per_color: int
    bits_per_pixel: int
    bytes_per_line: int
    color_order: int
    color_space: int
    num_colors: int
    duplex: bool
    tumble: bool
    resolution: tuple[int, int]
    page_size: tuple[int, int]
    page_size_name: str
    imaging_bounding_box: tuple[int, int, int, int]
    image_box: tuple[int, int, int, int]
    total_page_count: int
    cross_feed_transform: int
    feed_transform: int
    num_copies: int
    print_quality: int
    alternate_primary: int


@dataclass
class Page:
    """A decoded page: its header and its uncompressed lines."""

    header: PageHeader
    lines: list[bytes] = field(default_factory=list)

    def black(self, x: int, y: int) -> bool:
        """Is the pixel at (x, y) ink?"""
        line = self.lines[y]
        if self.header.bits_per_pixel == 1:
            return bool(line[x // 8] >> (7 - x % 8) & 1)
        return line[x] < 128


def _u32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from(">I", data, offset)[0])


def _i32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from(">i", data, offset)[0])


def _cstring(data: bytes, offset: int) -> str:
    raw = data[offset : offset + 64]
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def decode_header(data: bytes, offset: int = 0) -> PageHeader:
    """Read one page header, from a whole stream or from the header alone."""
    if data[offset : offset + 4] == SYNC_WORD:
        offset += 4
    if len(data) - offset < HEADER_SIZE:
        raise ValueError("Truncated PWG raster page header")

    def integer(name: str) -> int:
        return _u32(data, offset + _OFFSETS[name])

    def second(name: str) -> int:
        """The second of a pair of integers (HWResolution, PageSize)."""
        return _u32(data, offset + _OFFSETS[name] + 4)

    def signed(name: str) -> int:
        return _i32(data, offset + _OFFSETS[name])

    def text(name: str) -> str:
        return _cstring(data, offset + _OFFSETS[name])

    return PageHeader(
        magic=text("PwgRaster"),
        media_color=text("MediaColor"),
        media_type=text("MediaType"),
        print_content_optimize=text("PrintContentOptimize"),
        width=integer("Width"),
        height=integer("Height"),
        bits_per_color=integer("BitsPerColor"),
        bits_per_pixel=integer("BitsPerPixel"),
        bytes_per_line=integer("BytesPerLine"),
        color_order=integer("ColorOrder"),
        color_space=integer("ColorSpace"),
        num_colors=integer("NumColors"),
        duplex=bool(integer("Duplex")),
        tumble=bool(integer("Tumble")),
        resolution=(integer("HWResolution"), second("HWResolution")),
        page_size=(integer("PageSize"), second("PageSize")),
        page_size_name=text("PageSizeName"),
        imaging_bounding_box=tuple(  # type: ignore[arg-type]
            _u32(data, offset + _OFFSETS["ImagingBoundingBox"] + 4 * i) for i in range(4)
        ),
        image_box=(
            integer("ImageBoxLeft"),
            integer("ImageBoxTop"),
            integer("ImageBoxRight"),
            integer("ImageBoxBottom"),
        ),
        total_page_count=integer("TotalPageCount"),
        cross_feed_transform=signed("CrossFeedTransform"),
        feed_transform=signed("FeedTransform"),
        num_copies=integer("NumCopies"),
        print_quality=integer("PrintQuality"),
        alternate_primary=integer("AlternatePrimary"),
    )


def _decode_line(data: bytes, offset: int, bytes_per_line: int) -> tuple[bytes, int]:
    line = bytearray()
    while len(line) < bytes_per_line:
        count = data[offset]
        offset += 1
        if count < 128:
            line += bytes([data[offset]]) * (count + 1)
            offset += 1
        elif count == 128:
            raise ValueError("Invalid PWG raster run count 128")
        else:
            length = 257 - count
            line += data[offset : offset + length]
            offset += length
    if len(line) != bytes_per_line:
        raise ValueError("PWG raster line overruns BytesPerLine")
    return bytes(line), offset


def decode_pages(data: bytes) -> list[Page]:
    """Read a whole stream back: every page, with its lines uncompressed."""
    if data[:4] != SYNC_WORD:
        raise ValueError("Not a PWG raster stream (no RaS2 sync word)")

    pages: list[Page] = []
    offset = 4
    while offset < len(data):
        header = decode_header(data, offset)
        offset += HEADER_SIZE
        page = Page(header=header)
        while len(page.lines) < header.height:
            repeats = data[offset] + 1
            offset += 1
            line, offset = _decode_line(data, offset, header.bytes_per_line)
            page.lines.extend([line] * repeats)
        if len(page.lines) != header.height:
            raise ValueError("PWG raster page has more lines than its Height")
        pages.append(page)
    return pages
