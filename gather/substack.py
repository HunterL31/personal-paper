"""
Substack gatherer.

House rule 1 governs this module: **the text of every paragraph is the
author's, untouched.** The only editing allowed here is removing Substack's
own chrome — subscribe and share buttons, embeds, images and their captions —
and collapsing whitespace runs that only exist because the HTML was indented.
Nothing is summarized, shortened or reworded, and no character is added to a
paragraph (no bullet glyphs, no quotation marks).

`fetch(settings)` returns article dicts in the render contract's shape plus an
extra `guid` key. It deliberately does **not** record what it has seen: the run
is not successful until the paper is delivered, so run.py calls `mark_seen()`
with those guids once it is.
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

MAX_ARTICLES = 4
MAX_AGE_DAYS = 7
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
def _seen_guids() -> set[str]:
    try:
        import state  # written by the run agent, at the repo root
    except Exception:
        logger.warning("state module unavailable; treating every post as new")
        return set()
    try:
        return set(state.load_state().get("seen_posts") or [])
    except Exception:
        logger.warning("could not read seen_posts from state", exc_info=True)
        return set()


def mark_seen(guids: list[str]) -> None:
    """
    Record posts as printed. Called by run.py **after** a successful run, so a
    failed or undelivered run reprints the same posts tomorrow rather than
    losing them. Keeps the most recent `SEEN_HISTORY` guids.
    """
    guids = [g for g in (guids or []) if g]
    if not guids:
        return
    try:
        import state

        st = state.load_state()
        seen = list(st.get("seen_posts") or [])
        known = set(seen)
        for g in guids:
            if g not in known:
                seen.append(g)
                known.add(g)
        st["seen_posts"] = seen[-SEEN_HISTORY:]
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


def extract_paragraphs(html: str) -> list[str]:
    """
    Every block element the author wrote, in document order, verbatim.

    Substack chrome is removed first. Headings, blockquote paragraphs and list
    items come through as plain paragraphs with nothing prefixed to them.
    """
    soup = _soup(html)
    _strip(soup, CHROME_SELECTORS)
    paragraphs: list[str] = []
    for el in soup.find_all(BLOCK_TAGS):
        text = _clean(_text_excluding_blocks(el))
        if text:
            paragraphs.append(text)
    return paragraphs


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
    soup = _soup(html)
    _strip(soup, CHROME_SELECTORS)
    _strip(soup, EMAIL_CHROME_SELECTORS)
    paragraphs: list[str] = []
    for el in soup.find_all(BLOCK_TAGS):
        text = _clean(_text_excluding_blocks(el))
        if not text:
            continue
        if any(p.match(text) for p in EMAIL_CHROME_PARAGRAPHS):
            continue
        paragraphs.append(text)
    return paragraphs


# --------------------------------------------------------------- fetch
def _article(entry, publication: str, paragraphs: list[str], dt: datetime) -> dict:
    return {
        "title": _clean(entry.get("title") or ""),
        "deck": _deck(entry, paragraphs),
        "author": _clean(entry.get("author") or ""),
        "publication": publication,
        "published": _format_published(dt),
        "paragraphs": paragraphs,
        "guid": _guid(entry),
    }


def _articles_for_source(source, seen: set[str]) -> list[dict]:
    url = feed_url(source.name)
    parsed = _parse_feed(_get(url))
    publication = _clean((parsed.feed or {}).get("title") or source.name)
    cutoff = _now() - timedelta(days=MAX_AGE_DAYS)
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
            logger.debug("%s: %r is older than %d days", publication, title, MAX_AGE_DAYS)
            continue

        html = _entry_html(entry)
        paragraphs = extract_paragraphs(html)
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
            paragraphs = extract_email_paragraphs(email_html)
            if not paragraphs:
                logger.warning("%s: email edition of %r was empty", publication, title)
                continue
        if not paragraphs:
            logger.warning("%s: %r had no printable text; skipped", publication, title)
            continue
        out.append((dt, _article(entry, publication, paragraphs, dt)))

    out.sort(key=lambda pair: pair[0], reverse=True)   # newest first within a source
    return [article for _, article in out]


def fetch(settings) -> list[dict]:
    """
    New posts from the configured publications, front-page priority order:
    the order of `settings.sources.substacks` first, newest first within a
    publication. At most four; anything older than a week is skipped so the
    first run does not print a year of archive.

    Each article carries an extra `guid`; run.py passes those to `mark_seen()`
    once the issue has actually been delivered.
    """
    sources = list(settings.sources.substacks or [])
    if not sources:
        logger.info("no Substack sources configured")
        return []

    seen = _seen_guids()
    articles: list[dict] = []
    for source in sources:
        try:
            found = _articles_for_source(source, seen)
        except Exception:
            logger.error("substack source %r failed", source.name, exc_info=True)
            continue
        logger.info("%s: %d new post(s)", source.name, len(found))
        articles.extend(found)

    if len(articles) > MAX_ARTICLES:
        for extra in articles[MAX_ARTICLES:]:
            logger.info(
                "held over (more than %d new posts): %r — %s",
                MAX_ARTICLES, extra["title"], extra["publication"],
            )
    return articles[:MAX_ARTICLES]


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
