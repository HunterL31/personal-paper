"""
Substack gatherer.

House rule 1 governs this module: **the text of every paragraph is the
author's, untouched.** The only editing allowed here is removing Substack's
own chrome — subscribe and share buttons, embeds, images and their captions —
and collapsing whitespace runs that only exist because the HTML was indented.
Nothing is summarized, shortened or reworded, and no character is added to a
paragraph (no bullet glyphs, no quotation marks).

The pictures are not printed in the text, but they are not thrown away
either: each article carries `images`, one entry per picture the author set
between the paragraphs -- its address, its caption verbatim, and `after`, the
number of paragraphs before it. `gather/images.py` fetches the files when
the reader has asked for a picture sheet, and the template numbers them.

`fetch(settings)` returns article dicts in the render contract's shape (the
printable `url` among them) plus an extra `guid` key. It deliberately does
**not** record what it has seen: the run is not successful until the paper is
delivered, so run.py calls `mark_seen()` with those guids once it is.

The queue
---------
Posts are a queue, not a news feed. Everything inside the window
(`settings.sources.article_max_age_days`, default a week) that has not been
printed yet waits its turn: publications in the order of the Sources tab, and
within a publication the **oldest unread post first**. That is the deliberate
choice — a newsletter is read in the order it was written, and nothing is
skipped while newer posts jump ahead of it. A post that falls out of the
window before its turn comes is never printed; that is the price of the
window, and it keeps a first run from printing a year of archive.

`fetch` returns up to `QUEUE_LIMIT` candidates even though the sheet holds at
most four, so the layout can fall through to the next one when a story does
not fit. Anything still waiting is logged, not lost.
"""
from __future__ import annotations

import email as email_lib
import email.header
import email.utils
import imaplib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: How many candidates `fetch` hands to the layout. The sheet prints at most
#: four; the extras let it fall through when a story does not fit whole.
QUEUE_LIMIT = 8
#: The window, when the settings do not carry one.
DEFAULT_MAX_AGE_DAYS = 7
FEED_TIMEOUT_S = 20
DECK_MAX_CHARS = 200
SEEN_HISTORY = 500        # guids kept in state.json

# Substack blocks the requests default User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BLOCK_TAGS = ("p", "h2", "h3", "h4", "blockquote", "li")

#: Substack chrome. Removing these is allowed; editing text is not.
CHROME_SELECTORS = (
    ".subscription-widget-wrap",
    ".subscription-widget",
    ".button-wrapper",
    ".captioned-button-wrap",
    "button",
    "form",
    ".embedded-post-wrap",
    ".tweet",
    "iframe",
    "figure",
    "figcaption",
    ".captioned-image-container",
    ".image-link",
)
#: Where Substack puts a picture: the outermost of these around an `img` is
#: one picture, with the `figcaption` inside it as its caption. They are
#: taken out before the chrome is stripped, so the picture is kept and the
#: paragraphs come out exactly as they always have.
IMAGE_CONTAINERS = (".captioned-image-container", "figure")
#: A picture inside one of these is decoration, not one the author set
#: between paragraphs: an emoji in a sentence, an icon on a button.
INLINE_IMAGE_PARENTS = ("p", "li", "h2", "h3", "h4", "button")
#: Smaller than this on either side (when the tag says) is an icon.
MIN_IMAGE_PX = 100
#: The placeholder an extracted picture leaves in the tree, so its place
#: among the paragraphs is counted like theirs. Not a block tag, so it can
#: never add a paragraph, and it has no text of its own.
_IMAGE_TAG = "pp-image"

PAYWALL_SELECTORS = (".paywall", ".paywall-jump")
PAYWALL_PHRASES = (
    "this post is for paid subscribers",
    "this post is for paying subscribers",
    "subscribe to keep reading",
    "keep reading with a 7-day free trial",
    "this post is for subscribers",
)
#: Below this much text, a post that also ends in a "read more" link is a
#: paywalled preview rather than a short post.
TRUNCATION_CHARS = 1200
READ_MORE = re.compile(r"^(read more|keep reading|continue reading)\b", re.I)

# AP style month abbreviations (March–July are never abbreviated).
AP_MONTHS = (
    "Jan.", "Feb.", "March", "April", "May", "June",
    "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec.",
)

EMAIL_CHROME_SELECTORS = (
    ".email-header", ".email-footer", ".header", ".footer", ".subtitle",
    ".preamble", ".post-meta", ".unsubscribe", ".email-ufi",
    "#header", "#footer",
)
EMAIL_CHROME_PARAGRAPHS = (
    re.compile(r"^view (this )?(post |email )?in browser$", re.I),
    re.compile(r"^unsubscribe.*$", re.I),
    re.compile(r"^you('re| are) (currently )?receiving this.*$", re.I),
    re.compile(r"^©\s*\d{4}.*$"),
    re.compile(r"^substack inc\.?.*$", re.I),
    re.compile(r"^\d+ .*(street|st\.|market st).*$", re.I),
    re.compile(r"^(read|view) online$", re.I),
    re.compile(r"^(like|comment|share|restack|read in app)$", re.I),
)


# ----------------------------------------------------------------- time
def _now() -> datetime:
    """Timezone-aware now, in UTC. A seam for the tests."""
    return datetime.now(timezone.utc)


def _local_tz():
    try:
        from zoneinfo import ZoneInfo

        from app.settings import Env

        return ZoneInfo(Env.tz())
    except Exception:  # pragma: no cover - bad TZ name
        logger.debug("falling back to UTC for display dates", exc_info=True)
        return timezone.utc


def _format_published(dt: datetime) -> str:
    """`Sept. 15` — AP style, in the reader's timezone."""
    local = dt.astimezone(_local_tz())
    return f"{AP_MONTHS[local.month - 1]} {local.day}"


# ----------------------------------------------------------------- state
#: `state.seen_posts` is the guids in the order they were marked printed --
#: paper by paper, and within a paper in the sheet's order, the lead first.
#: `state.printed_at` says when each was marked, so the queue view can list
#: what has printed in that order and say the day it went out.
def _seen_order() -> list[str]:
    try:
        import state  # written by the run agent, at the repo root
    except Exception:
        logger.warning("state module unavailable; treating every post as new")
        return []
    try:
        return [g for g in (state.load_state().get("seen_posts") or []) if isinstance(g, str)]
    except Exception:
        logger.warning("could not read seen_posts from state", exc_info=True)
        return []


def _seen_guids() -> set[str]:
    return set(_seen_order())


def _printed_at() -> dict[str, str]:
    """guid -> the ISO time it was marked printed, for the guids that have one."""
    try:
        import state

        stamps = state.load_state().get("printed_at")
    except Exception:
        return {}
    if not isinstance(stamps, dict):
        return {}
    return {g: t for g, t in stamps.items() if isinstance(g, str) and isinstance(t, str)}


def mark_unseen(guids: list[str]) -> None:
    """Forget that posts were printed, so they queue again (the reader's
    call from the Sources tab). Never raises."""
    guids = {g for g in (guids or []) if g}
    if not guids:
        return
    try:
        import state

        st = state.load_state()
        st["seen_posts"] = [g for g in (st.get("seen_posts") or []) if g not in guids]
        stamps = st.get("printed_at")
        if isinstance(stamps, dict):
            st["printed_at"] = {g: t for g, t in stamps.items() if g not in guids}
        state.save_state(st)
        logger.info("marked %d post(s) as unread", len(guids))
    except Exception:
        logger.exception("could not mark posts unread")


def mark_seen(guids: list[str]) -> None:
    """
    Record posts as printed. Called by run.py **after** a successful run, so a
    failed or undelivered run reprints the same posts tomorrow rather than
    losing them. Keeps the most recent `SEEN_HISTORY` guids, and stamps each
    newly printed one with the time, in `printed_at`; a post already
    recorded keeps the stamp it has.
    """
    guids = [g for g in (guids or []) if g]
    if not guids:
        return
    try:
        import state

        st = state.load_state()
        seen = list(st.get("seen_posts") or [])
        known = set(seen)
        stamps = st.get("printed_at")
        when = dict(stamps) if isinstance(stamps, dict) else {}
        now = _now().astimezone(_local_tz()).isoformat(timespec="seconds")
        for g in guids:
            if g not in known:
                seen.append(g)
                known.add(g)
                when[g] = now
        st["seen_posts"] = seen[-SEEN_HISTORY:]
        kept = set(st["seen_posts"])
        st["printed_at"] = {g: t for g, t in when.items() if g in kept}
        state.save_state(st)
        logger.info("marked %d post(s) as seen", len(guids))
    except Exception:
        logger.error("could not record seen posts", exc_info=True)


# ------------------------------------------------------------ fetching
def feed_url(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("http://") or name.startswith("https://"):
        return name
    return f"https://{name}.substack.com/feed"


def _get(url: str) -> bytes:
    """Fetch a feed. A seam for the tests — nothing else does network I/O."""
    import requests

    resp = requests.get(
        url,
        timeout=FEED_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"},
    )
    resp.raise_for_status()
    return resp.content


def _parse_feed(raw: bytes):
    import feedparser

    return feedparser.parse(raw)


# ---------------------------------------------------------- extraction
def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", "html.parser")


def _strip(soup, selectors: Iterable[str]) -> None:
    for selector in selectors:
        try:
            for el in soup.select(selector):
                el.decompose()
        except Exception:  # pragma: no cover - a bad selector must not break a run
            logger.debug("selector %s failed", selector, exc_info=True)


def _text_excluding_blocks(node) -> str:
    """
    The node's own text, skipping any nested block element (whose text is
    emitted as its own paragraph). Inline tags flatten to text (v1); a <br>
    becomes a space so words never run together.
    """
    from bs4 import NavigableString

    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
            continue
        name = getattr(child, "name", None)
        if name in BLOCK_TAGS:
            continue
        if name == "br":
            out.append(" ")
            continue
        out.append(_text_excluding_blocks(child))
    return "".join(out)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _image_src(img) -> str:
    """The picture's address, or "" for one that is decoration or has none."""
    src = _clean(img.get("src") or "")
    if not src.startswith(("http://", "https://")):
        return ""
    for side in ("width", "height"):
        raw = img.get(side)
        try:
            if raw is not None and int(str(raw).strip()) < MIN_IMAGE_PX:
                return ""
        except ValueError:
            pass
    return src


def _attached(node, soup) -> bool:
    """Is the node still in the document, or inside something lifted out?"""
    while node.parent is not None:
        node = node.parent
    return node is soup


def _lift_images(soup) -> None:
    """Replace every picture with a placeholder carrying its address and
    caption, in the place it had among the paragraphs.

    A captioned container is one picture, whatever the markup inside it; a
    bare `img` (an email edition sets some that way) is one too, unless it
    is inline in a paragraph or an icon. Done before the chrome is stripped,
    so a container with no picture in it goes with the rest of the chrome.
    """
    for selector in IMAGE_CONTAINERS:
        for box in soup.select(selector):
            if not _attached(box, soup):      # inside a container already lifted
                continue
            img = next((i for i in box.find_all("img") if _image_src(i)), None)
            if img is None:
                continue
            caption_el = box.find("figcaption")
            # The caption flattens like a paragraph: inline tags to text, a
            # <br> to a space, nothing added and nothing lost.
            caption = _clean(_text_excluding_blocks(caption_el)) if caption_el is not None else ""
            marker = soup.new_tag(_IMAGE_TAG)
            marker["data-src"] = _image_src(img)
            if caption:
                marker["data-caption"] = caption
            box.replace_with(marker)
    for img in soup.find_all("img"):
        src = _image_src(img)
        if not src or img.find_parent(INLINE_IMAGE_PARENTS) is not None:
            continue
        marker = soup.new_tag(_IMAGE_TAG)
        marker["data-src"] = src
        img.replace_with(marker)


def extract_content(html: str, *, email: bool = False) -> tuple[list[str], list[dict]]:
    """
    Every block element the author wrote, in document order, verbatim, and
    every picture the author set between them.

    Substack chrome is removed first. Headings, blockquote paragraphs and list
    items come through as plain paragraphs with nothing prefixed to them. A
    picture is `{"url", "caption", "after"}`: `after` is how many of the
    paragraphs come before it (0 for one at the top), and `caption` is the
    author's, verbatim, or None. `email` also drops an email edition's own
    chrome.
    """
    soup = _soup(html)
    _lift_images(soup)
    _strip(soup, CHROME_SELECTORS)
    if email:
        _strip(soup, EMAIL_CHROME_SELECTORS)
    paragraphs: list[str] = []
    images: list[dict] = []
    for el in soup.find_all(BLOCK_TAGS + (_IMAGE_TAG,)):
        if el.name == _IMAGE_TAG:
            images.append({
                "url": el.get("data-src") or "",
                "caption": el.get("data-caption") or None,
                "after": len(paragraphs),
            })
            continue
        text = _clean(_text_excluding_blocks(el))
        if not text:
            continue
        if email and any(p.match(text) for p in EMAIL_CHROME_PARAGRAPHS):
            continue
        paragraphs.append(text)
    return paragraphs, images


def extract_paragraphs(html: str) -> list[str]:
    """The paragraphs alone; see `extract_content`."""
    return extract_content(html)[0]


def _entry_html(entry) -> str:
    content = entry.get("content") or []
    if content:
        value = content[0].get("value") if isinstance(content[0], dict) else None
        if value:
            return value
    return entry.get("summary") or ""


def looks_paywalled(html: str, paragraphs: list[str]) -> bool:
    """Is this the free preview of a paid post rather than the whole thing?"""
    soup = _soup(html)
    for selector in PAYWALL_SELECTORS:
        if soup.select(selector):
            return True
    lowered = soup.get_text(" ").lower()
    if any(phrase in lowered for phrase in PAYWALL_PHRASES):
        return True
    # A short body that ends in a "read more" link is Substack's cut-off
    # preview: the content:encoded is much shorter than the post really is.
    # (A subscribe widget alone means nothing — every free post carries one.)
    if sum(len(p) for p in paragraphs) < TRUNCATION_CHARS:
        for anchor in soup.find_all("a"):
            if READ_MORE.match(_clean(anchor.get_text(" "))):
                return True
    return False


def _deck(entry, paragraphs: list[str]) -> Optional[str]:
    """
    The item description, but only when it is a real subtitle: short, not
    ellipsized, and not simply the opening of the body.
    """
    raw = entry.get("subtitle") or entry.get("description") or entry.get("summary") or ""
    deck = _clean(_soup(raw).get_text(" "))
    if not deck:
        return None
    if len(deck) > DECK_MAX_CHARS:
        return None
    if deck.endswith("…") or deck.endswith("..."):
        return None
    if paragraphs and paragraphs[0].startswith(deck):
        return None
    return deck


def _published_dt(entry) -> Optional[datetime]:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            logger.debug("unparseable date %r", raw, exc_info=True)
    return None


def _guid(entry) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title") or ""


def _url(entry) -> str:
    """
    The post's own page, for the "the rest of this story is online" line.

    The feed's `link`, without the tracking query Substack appends to some of
    them: it is printed on paper, where a `?utm_source=` tail is unreadable
    and unusable. Nothing else about the address is touched.
    """
    link = _clean(entry.get("link") or "")
    if not link:
        guid = _clean(_guid(entry))
        link = guid if guid.startswith(("http://", "https://")) else ""
    if not link:
        return ""
    return link.split("?", 1)[0].split("#", 1)[0]


# --------------------------------------------------------------- IMAP
def _imap_config() -> Optional[dict]:
    import os

    from app.settings import Env

    host, user, password = (Env.get(n) for n in Env.IMAP)
    if not (host and user and password):
        return None
    return {
        "host": host,
        "user": user,
        "password": password,
        "mailbox": os.environ.get("IMAP_MAILBOX") or "INBOX",
    }


def fetch_from_imap(title: str, publication: str) -> Optional[str]:
    """
    The HTML body of the email edition of a post, or None.

    Used only for paid posts whose RSS item is a paywalled preview: the email
    she already receives contains the whole thing. Searches the last 24 hours
    for a message whose subject is exactly the post title. Returns None (with a
    log line) when IMAP is not configured — a truncated article is never
    printed.
    """
    config = _imap_config()
    if not config:
        logger.warning(
            "paywalled post %r from %s skipped: IMAP is not configured "
            "(IMAP_HOST/IMAP_USER/IMAP_PASSWORD)", title, publication,
        )
        return None
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(config["host"])
        conn.login(config["user"], config["password"])
        conn.select(config["mailbox"], readonly=True)
        since = (_now() - timedelta(hours=24)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since, "HEADER", "SUBJECT", title)
        if status != "OK":
            logger.warning("IMAP search failed for %r: %s", title, status)
            return None
        ids = (data[0] or b"").split()
        for msg_id in reversed(ids):
            status, raw = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not raw or not raw[0]:
                continue
            message = email_lib.message_from_bytes(raw[0][1])
            if _clean(_decode_header(message.get("Subject", ""))) != _clean(title):
                continue
            html = _html_part(message)
            if html:
                logger.info("using the email edition of %r", title)
                return html
        logger.warning("no email found for paywalled post %r", title)
        return None
    except Exception:
        logger.error("IMAP fallback failed for %r", title, exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def _decode_header(value: str) -> str:
    try:
        parts = email_lib.header.decode_header(value)
        return "".join(
            p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p
            for p, enc in parts
        )
    except Exception:
        return value or ""


def _html_part(message) -> Optional[str]:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, "replace")
        return None
    if message.get_content_type() == "text/html":
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            return payload.decode(charset, "replace")
    return None


def extract_email_paragraphs(html: str) -> list[str]:
    """The same extractor, after also dropping the email's own chrome."""
    return extract_content(html, email=True)[0]


# --------------------------------------------------------------- fetch
def _article(entry, publication: str, paragraphs: list[str], dt: datetime,
             images: Optional[list[dict]] = None) -> dict:
    return {
        "title": _clean(entry.get("title") or ""),
        "deck": _deck(entry, paragraphs),
        "author": _clean(entry.get("author") or ""),
        "publication": publication,
        "published": _format_published(dt),
        "paragraphs": paragraphs,
        #: part of the render contract: the template prints it under a story
        #: that only partly fit the sheet.
        "url": _url(entry),
        #: the pictures between the paragraphs, for the picture sheet;
        #: gather/images.py adds `file` to the ones it fetched.
        "images": list(images or []),
        "guid": _guid(entry),
    }


def _articles_for_source(source, seen: set[str], max_age_days: int) -> list[dict]:
    url = feed_url(source.name)
    parsed = _parse_feed(_get(url))
    publication = _clean((parsed.feed or {}).get("title") or source.name)
    cutoff = _now() - timedelta(days=max_age_days)
    out: list[tuple[datetime, dict]] = []

    for entry in parsed.entries or []:
        title = _clean(entry.get("title") or "")
        guid = _guid(entry)
        if guid and guid in seen:
            logger.debug("%s: already printed %r", publication, title)
            continue
        dt = _published_dt(entry)
        if dt is None:
            logger.warning("%s: %r has no usable date; skipped", publication, title)
            continue
        if dt < cutoff:
            logger.debug(
                "%s: %r is older than %d days; it will never print",
                publication, title, max_age_days,
            )
            continue

        html = _entry_html(entry)
        paragraphs, images = extract_content(html)
        if looks_paywalled(html, paragraphs):
            if not source.paid:
                logger.warning(
                    "%s: %r is paywalled and the source is not marked paid; skipped",
                    publication, title,
                )
                continue
            email_html = fetch_from_imap(title, publication)
            if not email_html:
                continue  # fetch_from_imap logged why
            paragraphs, images = extract_content(email_html, email=True)
            if not paragraphs:
                logger.warning("%s: email edition of %r was empty", publication, title)
                continue
        if not paragraphs:
            logger.warning("%s: %r had no printable text; skipped", publication, title)
            continue
        out.append((dt, _article(entry, publication, paragraphs, dt, images)))

    # A queue, not a feed: the oldest unread post is the one whose turn it is.
    out.sort(key=lambda pair: pair[0])
    return [article for _, article in out]


WINDOW_STEPS = (14, 30, 60, 90, 180, 365)


def wider_windows(days: int) -> list[int]:
    """The windows tried, in order, when the reader allows looking further back."""
    return [d for d in WINDOW_STEPS if d > days]


def _extends_window(settings) -> bool:
    return bool(getattr(getattr(settings, "sources", None), "extend_window_when_empty", False))


def _gather_window(sources, seen: set[str], max_age_days: int) -> list[dict]:
    """Every unread post inside the window, in queue order."""
    articles: list[dict] = []
    for source in sources:
        try:
            found = _articles_for_source(source, seen, max_age_days)
        except Exception:
            logger.error("substack source %r failed", source.name, exc_info=True)
            continue
        logger.info("%s: %d post(s) waiting within %d days", source.name, len(found), max_age_days)
        articles.extend(found)
    return articles


def _max_age_days(settings) -> int:
    """The window, from the Sources tab, clamped to the field's range."""
    try:
        days = int(getattr(settings.sources, "article_max_age_days", DEFAULT_MAX_AGE_DAYS))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS
    return max(1, min(60, days))


def fetch(settings) -> list[dict]:
    """
    The queue of unprinted posts, in the order they should be printed:
    publications in the order of `settings.sources.substacks`, and within a
    publication the oldest unread post first. Anything published longer ago
    than `settings.sources.article_max_age_days` is out of the window and
    never prints.

    At most `QUEUE_LIMIT` candidates come back — more than the four the sheet
    holds, so the layout has something to fall through to when a story does
    not fit. Whatever is still waiting is logged.

    Each article carries an extra `guid`; run.py passes those to `mark_seen()`
    once the issue has actually been delivered.
    """
    sources = list(settings.sources.substacks or [])
    if not sources:
        logger.info("no Substack sources configured")
        return []

    max_age_days = _max_age_days(settings)
    seen = _seen_guids()
    articles = _gather_window(sources, seen, max_age_days)
    if not articles and _extends_window(settings):
        for days in wider_windows(max_age_days):
            articles = _gather_window(sources, seen, days)
            if articles:
                logger.info("nothing unread within %d days; looked back %d days", max_age_days, days)
                break

    waiting = len(articles) - QUEUE_LIMIT
    if waiting > 0:
        logger.info(
            "%d post(s) still in the queue beyond the %d offered to the layout: %s",
            waiting, QUEUE_LIMIT,
            "; ".join(f"{a['title']!r} — {a['publication']}" for a in articles[QUEUE_LIMIT:]),
        )
    return articles[:QUEUE_LIMIT]


# ------------------------------------------------------- queue preview
#: Statuses a previewed post can carry, for the Sources tab's queue view.
QUEUE_STATUSES = ("queued", "beyond-limit", "printed", "preview-only", "too-old")
#: How many out-of-window posts the preview bothers to list.
TOO_OLD_SHOWN = 10


def _printed_day(stamp: Optional[str]) -> Optional[str]:
    """The day a `printed_at` stamp falls on, in the reader's zone, or None."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone(_local_tz()).date().isoformat()
    except (TypeError, ValueError):
        return None


def _preview_row(entry, publication: str, dt: datetime, status: str,
                 position: Optional[int] = None, printed_at: Optional[str] = None) -> dict:
    """One row of the queue view. No article text: the sheet prints that.
    `printed` is the day the post went out, for a printed one that has a
    stamp; None otherwise."""
    local = dt.astimezone(_local_tz())
    return {
        "guid": _guid(entry),
        "publication": publication,
        "title": _clean(entry.get("title") or ""),
        "url": _url(entry),
        "published": local.date().isoformat(),
        "age_days": round((_now() - dt).total_seconds() / 86400.0, 1),
        "position": position,
        "status": status,
        "printed": _printed_day(printed_at),
    }


def _in_printed_order(rows: list[dict]) -> list[dict]:
    """The printed rows as the server printed them: the latest paper first
    and, within a paper, the sheet's order (the lead first).

    The stamp in `printed_at` orders the papers; a paper's own posts share
    a stamp and keep their place in `seen_posts`, which is the sheet's order.
    Posts recorded before there were stamps come after, latest first by
    that place alone.
    """
    stamps = _printed_at()
    place = {g: i for i, g in enumerate(_seen_order())}
    stamped = [r for r in rows if stamps.get(r.get("guid") or "")]
    unstamped = [r for r in rows if not stamps.get(r.get("guid") or "")]
    stamped.sort(key=lambda r: (stamps[r["guid"]], -place.get(r["guid"], -1)), reverse=True)
    unstamped.sort(key=lambda r: place.get(r.get("guid") or "", -1), reverse=True)
    return stamped + unstamped


def _preview_for_source(source, seen: set[str], max_age_days: int) -> tuple[list, list, list]:
    """
    One feed, sorted into (queued, printed, skipped) — the same decisions
    `_articles_for_source` makes, kept as rows instead of dropped.

    A paywalled post is only really printable when the source is marked paid
    *and* the email route exists; IMAP itself is not opened here, because the
    preview is meant to be cheap and to change nothing.

    Having been printed is a fact about the post, not about the window, so it
    is settled first: a post the paper found by looking further back (it is
    older than the window by definition) is reported as printed, not filed
    under the posts that are too old to print. The printed rows come back
    in feed order; `queue_preview` puts them in the order they printed.
    """
    parsed = _parse_feed(_get(feed_url(source.name)))
    publication = _clean((parsed.feed or {}).get("title") or source.name)
    cutoff = _now() - timedelta(days=max_age_days)
    email_route = bool(getattr(source, "paid", False)) and _imap_config() is not None
    stamps = _printed_at()

    queued: list[tuple[datetime, dict]] = []
    printed: list[tuple[datetime, dict]] = []
    skipped: list[tuple[datetime, dict]] = []

    for entry in parsed.entries or []:
        dt = _published_dt(entry)
        if dt is None:
            logger.debug("%s: %r has no usable date; not previewed", publication,
                         _clean(entry.get("title") or ""))
            continue
        guid = _guid(entry)
        if guid and guid in seen:
            printed.append((dt, _preview_row(entry, publication, dt, "printed",
                                             printed_at=stamps.get(guid))))
            continue
        if dt < cutoff:
            skipped.append((dt, _preview_row(entry, publication, dt, "too-old")))
            continue
        html = _entry_html(entry)
        paragraphs = extract_paragraphs(html)
        if looks_paywalled(html, paragraphs):
            if not email_route:
                skipped.append((dt, _preview_row(entry, publication, dt, "preview-only")))
                continue
        elif not paragraphs:
            continue  # nothing printable; `fetch` skips it too
        queued.append((dt, _preview_row(entry, publication, dt, "queued")))

    # The queue's own order: the oldest unprinted post is next.
    queued.sort(key=lambda pair: pair[0])
    # The skipped read better newest first — they are a record. The printed
    # are a record too, of what the server did: `queue_preview` orders them.
    skipped.sort(key=lambda pair: pair[0], reverse=True)
    return (
        [row for _, row in queued],
        [row for _, row in printed],
        [row for _, row in skipped],
    )


def queue_preview(settings) -> dict:
    """
    What the Sources tab's "Show queue" button shows: every configured feed
    fetched now, every post in it sorted into what would happen to it.

    `queued` is exactly what `fetch` would offer, in its order — positions 1
    upwards, and `beyond-limit` (position `None`) for the ones still waiting
    behind `QUEUE_LIMIT`. `printed` is every post in the feeds that has
    already been in the paper, whatever its age — a post printed from a
    widened window is older than the window and still belongs here — in
    the order the server printed them: the latest paper first, the lead
    first within it, each row saying the day it went out (`printed`).
    `skipped` is what never will be: paywalled previews with no email route, and
    the posts that fell out of the window (the most recent `TOO_OLD_SHOWN`
    of those). A feed that fails is an `errors` entry, not an exception.

    Nothing is written: this marks no post as seen.
    """
    max_age_days = _max_age_days(settings)
    seen = _seen_guids()
    result: dict[str, Any] = {
        "window_days": max_age_days,
        "queued": [], "printed": [], "skipped": [], "errors": [],
    }
    too_old: list[dict] = []

    for source in list(settings.sources.substacks or []):
        try:
            queued, printed, skipped = _preview_for_source(source, seen, max_age_days)
        except Exception as exc:  # noqa: BLE001 - one bad feed, not one bad preview
            logger.error("substack source %r failed", source.name, exc_info=True)
            result["errors"].append(
                {"publication": source.name, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        result["queued"].extend(queued)
        result["printed"].extend(printed)
        for row in skipped:
            (too_old if row["status"] == "too-old" else result["skipped"]).append(row)

    # With nothing unread in the window and the reader's leave to look
    # further back, the paper would widen the window: show what it would find.
    result["window_extended"] = False
    if not result["queued"] and _extends_window(settings):
        for days in wider_windows(max_age_days):
            wider: list[dict] = []
            for source in list(settings.sources.substacks or []):
                try:
                    queued, _, _ = _preview_for_source(source, seen, days)
                except Exception:
                    continue
                wider.extend(queued)
            if wider:
                result["queued"] = wider
                result["window_days"] = days
                result["window_extended"] = True
                found = {row.get("guid") for row in wider}
                too_old = [row for row in too_old if row.get("guid") not in found]
                break

    for index, row in enumerate(result["queued"]):
        if index < QUEUE_LIMIT:
            row["position"] = index + 1
        else:
            row["status"] = "beyond-limit"
    result["printed"] = _in_printed_order(result["printed"])
    too_old.sort(key=lambda row: row["age_days"])   # most recent first
    result["skipped"].extend(too_old[:TOO_OLD_SHOWN])
    return result


def check(name: str) -> dict:
    """What the Sources tab's "Check" button shows for one publication."""
    result: dict[str, Any] = {
        "publication": "", "latest_title": "", "paid_marker_seen": False,
    }
    parsed = _parse_feed(_get(feed_url(name)))
    result["publication"] = _clean((parsed.feed or {}).get("title") or name)
    entries = parsed.entries or []
    if entries:
        result["latest_title"] = _clean(entries[0].get("title") or "")
        html = _entry_html(entries[0])
        result["paid_marker_seen"] = looks_paywalled(html, extract_paragraphs(html))
    return result


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.settings import Settings

    print(json.dumps(fetch(Settings.load()), indent=2, ensure_ascii=False))
