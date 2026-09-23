"""
The stories' pictures, for the picture sheet.

`gather/substack.py` records where each picture sits among an article's
paragraphs and what its caption says; this module fetches the files, when
the reader has switched the picture sheet on (`look.layout.pictures`), and
writes them beside the day's `data.json` as grey JPEGs no bigger than the
sheet needs. Each picture that was fetched gets a `file`, a path relative to
the folder `data.json` is in; one that could not be fetched or read keeps
`file: null` and a log line, and the template prints neither it nor a
reference to it.

House rule 3: nothing here raises, and the whole step gives up at
`BUDGET_S` so the paper is never waiting on a slow picture. House rule 4:
the sheet is black only, so every picture is turned grey here, once.

`fetch(articles, out_dir)` is what run.py calls; `python -m gather.images`
prints what today's queue would fetch, without fetching it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The most pictures fetched for one paper: the sheet holds about a dozen
#: at the size they are printed, and fetching more would be a wait for
#: nothing. They are taken in the queue's order, so the lead's come first.
MAX_IMAGES = 16
#: A file bigger than this is not a picture for a newspaper.
MAX_BYTES = 12 * 1024 * 1024
#: Per request, and for the whole step.
TIMEOUT_S = 15.0
BUDGET_S = 45.0
#: A picture is printed at most 3.7in wide, which at 300 dpi is about
#: 1100 pixels: the longest side is brought down to this before saving.
MAX_SIDE_PX = 1400
JPEG_QUALITY = 82
#: The folder beside data.json the files go in.
FOLDER = "images"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
#: No WebP or AVIF: the file is read with pymupdf, which knows JPEG, PNG,
#: GIF, BMP and TIFF. A CDN that picks the format from this header
#: (Substack's `f_auto`) then sends one of those.
ACCEPT = "image/jpeg, image/png, image/gif, image/*;q=0.8, */*;q=0.5"

#: Substack's image CDN: `https://substackcdn.com/image/fetch/<params>/<url>`.
#: A feed names a WebP transform outright; asking for `f_auto` instead lets
#: the Accept header above choose, and touches nothing else in the address.
_SUBSTACK_CDN = re.compile(r"^(https://substackcdn\.com/image/fetch/)([^/]*)(/.*)$")


def fetch_url(url: str) -> str:
    """The address actually asked for: the same picture in a format the
    paper can read, where the CDN lets that be said in the address."""
    m = _SUBSTACK_CDN.match(url or "")
    if not m:
        return url
    params = [p if not p.startswith("f_") else "f_auto" for p in m.group(2).split(",") if p]
    return m.group(1) + ",".join(params) + m.group(3)


def _get(url: str) -> bytes:
    """Fetch one picture. A seam for the tests -- nothing else does I/O
    on the network. Raises on anything but a picture-sized body."""
    import requests

    with requests.get(
        fetch_url(url),
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
        stream=True,
    ) as resp:
        resp.raise_for_status()
        kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind.startswith("text/"):
            raise ValueError(f"not a picture: {kind}")
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise ValueError(f"too big: {int(declared)} bytes")
        body = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError(f"too big: more than {MAX_BYTES} bytes")
    return bytes(body)


def save_grey(data: bytes, path: Path) -> tuple[int, int]:
    """Write the picture as a grey JPEG no larger than the sheet needs, and
    return its size in pixels. Raises when the bytes are not a picture."""
    import pymupdf

    pix = pymupdf.Pixmap(data)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)          # a laser has no transparent ink
    if pix.n != 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    while max(pix.width, pix.height) > MAX_SIDE_PX and min(pix.width, pix.height) > 1:
        pix.shrink(1)                          # halves both sides
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(path), jpg_quality=JPEG_QUALITY)
    return pix.width, pix.height


def fetch(articles: list[dict], out_dir: Path | str) -> int:
    """Fetch the pictures of `articles`, in their order, into
    `<out_dir>/images/`, and set each fetched picture's `file`.

    Returns how many were fetched. Never raises: a picture that fails is
    logged and left without a `file`; the step stops at `MAX_IMAGES` or
    when `BUDGET_S` is spent, and says so.
    """
    folder = Path(out_dir) / FOLDER
    deadline = time.monotonic() + BUDGET_S
    fetched = 0
    wanted = 0
    left_out: list[str] = []

    for a_index, article in enumerate(articles or []):
        images = (article or {}).get("images") if isinstance(article, dict) else None
        for i_index, image in enumerate(images or []):
            if not isinstance(image, dict):
                continue
            image.setdefault("file", None)
            url = str(image.get("url") or "")
            if not url:
                continue
            wanted += 1
            if fetched >= MAX_IMAGES or time.monotonic() > deadline:
                left_out.append(url)
                continue
            name = f"{a_index}-{i_index}.jpg"
            try:
                body = _get(url)
                width, height = save_grey(body, folder / name)
            except Exception as exc:  # noqa: BLE001 - one picture, not the paper
                logger.warning("picture %s of %r not fetched: %s: %s",
                               i_index + 1, (article.get("title") or "")[:60],
                               type(exc).__name__, exc)
                continue
            image["file"] = f"{FOLDER}/{name}"
            fetched += 1
            logger.debug("picture %s: %dx%d from %s", name, width, height, url)

    if left_out:
        logger.warning("%d picture(s) not fetched: past %d pictures or %.0fs",
                       len(left_out), MAX_IMAGES, BUDGET_S)
    if wanted:
        logger.info("pictures: %d of %d fetched", fetched, wanted)
    return fetched


def planned(articles: list[dict]) -> list[dict[str, Any]]:
    """What `fetch` would ask for, for `__main__` and the log: one row per
    picture, nothing fetched."""
    rows: list[dict[str, Any]] = []
    for a_index, article in enumerate(articles or []):
        for i_index, image in enumerate((article or {}).get("images") or []):
            rows.append({
                "article": a_index,
                "title": (article or {}).get("title") or "",
                "after": image.get("after"),
                "url": image.get("url"),
                "fetch": fetch_url(str(image.get("url") or "")),
                "caption": image.get("caption"),
            })
    return rows


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.settings import Settings
    from gather import substack

    print(json.dumps(planned(substack.fetch(Settings.load())), indent=2, ensure_ascii=False))
