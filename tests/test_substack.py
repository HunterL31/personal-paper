"""
Substack gatherer tests.

The important one is `test_paragraphs_are_verbatim`: it pins the exact text
the extractor produces against the fixture, so any future change that
summarizes, reorders, prefixes or drops a word of the author's text fails
here (house rule 1).
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from app.settings import Settings, Sources, SubstackSource
from gather import substack

NOW = datetime(2025, 9, 16, 15, 0, tzinfo=timezone.utc)

FEED_URL = "https://slowkitchen.substack.com/feed"
BREAD = "https://slowkitchen.substack.com/p/the-bread-you-meant-to-make"
SECOND_CUP = "https://slowkitchen.substack.com/p/the-case-for-the-second-cup"
TOMATOES = "https://slowkitchen.substack.com/p/end-of-the-summer-tomatoes"

BREAD_PARAGRAPHS = [
    "There is a loaf that exists only in the future tense, and most weeks that is"
    " where it stays. You buy the flour on Saturday with real intent, and on"
    " Thursday the bag is still standing there on the counter, faintly accusing.",
    "Start on Friday",
    "The dough does not care what time it is. It cares about temperature, and about"
    " being left alone, which are the two things a weeknight kitchen is worst at"
    " providing. Friday night is the exception: nothing is expected of you, and the"
    " oven is free.",
    "Flour, water, salt, time. The rest is decoration.",
    "Mix at nine, when the kitchen has finished being a kitchen.",
    "Fold it twice before bed, badly, with wet hands.",
    "Bake it before the coffee, while the oven is the hottest thing in the house.",
    "I have made this loaf badly for six years, which is long enough to say with some"
    " confidence that the bad ones are still better than no bread at all.1",
    "Friday, then. Nine o'clock. Flour, water, salt, and the patience to go to bed"
    " while something else is working.",
]

SECOND_CUP_PARAGRAPHS = [
    "The first cup is chemistry. The second cup is a decision, and it is the decision"
    " I would like to defend.",
    "Nobody needs a second cup of coffee. The caffeine has already done whatever it"
    " was going to do; by the time the second one is poured you are as awake as you"
    " are going to get, and the cup is not doing any work at all.",
    "What you are actually buying",
    "Twenty minutes with a warm thing in your hands and no obligation attached to it."
    " That is the whole product. The first cup belongs to the day; the second one is"
    " the last thing that is still yours before the day starts asking for things.",
    "The second cup is the only appointment I keep with myself.",
    "So: the second cup. Not because you need it, and not because it will make you"
    " productive, which it will not. Because the morning is not over yet.",
]


# ------------------------------------------------------------- helpers
def settings_for(*sources: SubstackSource) -> Settings:
    return Settings(sources=Sources(substacks=list(sources)))


def build_feed(title: str, host: str, items: list[dict]) -> bytes:
    """A minimal but realistic Substack feed, for the ordering/cap cases."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f"<channel><title>{title}</title>"
        f"<link>https://{host}.substack.com</link>",
    ]
    for item in items:
        parts.append(
            "<item>"
            f"<title>{item['title']}</title>"
            f"<description>{item.get('deck', '')}</description>"
            f"<link>https://{host}.substack.com/p/{item['slug']}</link>"
            f'<guid isPermaLink="false">https://{host}.substack.com/p/{item["slug"]}</guid>'
            f"<dc:creator>{item.get('author', 'A. Writer')}</dc:creator>"
            f"<pubDate>{item['pubdate']}</pubDate>"
            f"<content:encoded><![CDATA[<p>{item.get('body', 'Body text.')}</p>]]>"
            "</content:encoded></item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setattr(substack, "_now", lambda: NOW)


@pytest.fixture
def feeds(monkeypatch, fixtures):
    """Serve feeds from a dict instead of the network."""
    served = {FEED_URL: (fixtures / "substack_feed.xml").read_bytes()}

    def fake_get(url: str) -> bytes:
        if url not in served:
            raise AssertionError(f"unexpected fetch: {url}")
        return served[url]

    monkeypatch.setattr(substack, "_get", fake_get)
    return served


@pytest.fixture
def no_imap(monkeypatch):
    for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_state(monkeypatch):
    """A stand-in for the run agent's root `state.py`."""
    store = {"seen_posts": []}
    module = types.ModuleType("state")
    module.load_state = lambda: dict(store)
    module.save_state = lambda d: store.update(d)
    monkeypatch.setitem(sys.modules, "state", module)
    return store


# --------------------------------------------------------------- tests
def test_paragraphs_are_verbatim(feeds, no_imap, fake_state):
    articles = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert [a["title"] for a in articles] == ["The bread you meant to make"]
    assert articles[0]["paragraphs"] == BREAD_PARAGRAPHS


def test_no_substack_chrome_in_paragraphs(feeds, no_imap, fake_state):
    (article,) = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    body = "\n".join(article["paragraphs"])
    for chrome in (
        "Subscribe",
        "Share",
        "Thanks for reading",
        "A loaf, photographed badly",   # figcaption
        "Type your email",
    ):
        assert chrome not in body
    # nothing is prefixed to a list item or a quote
    assert not any(p.startswith(("•", '"', "“", "> ")) for p in article["paragraphs"])


def test_article_fields(feeds, no_imap, fake_state):
    (article,) = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert article["deck"] == "A loaf is mostly a decision you made the night before."
    assert article["author"] == "Nora Feld"
    assert article["publication"] == "The Slow Kitchen"
    assert article["published"] == "Sept. 15"
    assert article["guid"] == BREAD
    assert set(article) == {
        "title", "deck", "author", "publication", "published", "paragraphs", "guid",
    }


def test_post_older_than_a_week_is_skipped(feeds, no_imap, fake_state):
    articles = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert TOMATOES not in [a["guid"] for a in articles]


def test_seen_guids_are_skipped(feeds, no_imap, fake_state):
    fake_state["seen_posts"] = [BREAD]
    assert substack.fetch(settings_for(SubstackSource(name="slowkitchen"))) == []


def test_paywalled_post_skipped_without_imap(feeds, no_imap, fake_state, caplog):
    with caplog.at_level("WARNING"):
        articles = substack.fetch(
            settings_for(SubstackSource(name="slowkitchen", paid=True))
        )
    assert SECOND_CUP not in [a["guid"] for a in articles]
    assert "IMAP is not configured" in caplog.text


def test_paywalled_post_not_printed_truncated(feeds, no_imap, fake_state):
    """A preview must never reach the page, even as a short article."""
    articles = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    for article in articles:
        assert "paid subscribers" not in " ".join(article["paragraphs"]).lower()


def test_paywalled_post_via_imap(monkeypatch, feeds, fake_state, fixtures):
    calls = []

    def fake_imap(title, publication):
        calls.append((title, publication))
        return (fixtures / "substack_email.html").read_text()

    monkeypatch.setattr(substack, "fetch_from_imap", fake_imap)
    articles = substack.fetch(
        settings_for(SubstackSource(name="slowkitchen", paid=True))
    )
    assert calls == [("The case for the second cup", "The Slow Kitchen")]
    by_guid = {a["guid"]: a for a in articles}
    cup = by_guid[SECOND_CUP]
    assert cup["paragraphs"] == SECOND_CUP_PARAGRAPHS
    assert cup["deck"] == "Nobody needs it. That is the entire appeal."
    assert cup["published"] == "Sept. 14"
    body = "\n".join(cup["paragraphs"])
    for chrome in ("View in browser", "Unsubscribe", "Substack Inc", "Restack",
                   "The second cup, in the only mug that matters"):
        assert chrome not in body


def test_cap_at_four(monkeypatch, feeds, no_imap, fake_state, caplog):
    items = [
        {
            "title": f"Post {n}",
            "slug": f"post-{n}",
            "pubdate": f"Mon, 1{n} Sep 2025 16:00:00 GMT",
            "body": f"The body of post {n}.",
        }
        for n in range(6)
    ]
    feeds["https://daily.substack.com/feed"] = build_feed("Daily", "daily", items)
    with caplog.at_level("INFO"):
        articles = substack.fetch(settings_for(SubstackSource(name="daily")))
    assert len(articles) == 4
    assert [a["title"] for a in articles] == ["Post 5", "Post 4", "Post 3", "Post 2"]
    assert "held over" in caplog.text


def test_priority_is_settings_order_then_newest(feeds, no_imap, fake_state):
    feeds["https://first.substack.com/feed"] = build_feed(
        "First", "first",
        [{"title": "Older but first in the list", "slug": "a",
          "pubdate": "Sat, 13 Sep 2025 16:00:00 GMT"}],
    )
    feeds["https://second.substack.com/feed"] = build_feed(
        "Second", "second",
        [{"title": "Newest", "slug": "b", "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"},
         {"title": "Middle", "slug": "c", "pubdate": "Mon, 15 Sep 2025 09:00:00 GMT"}],
    )
    articles = substack.fetch(
        settings_for(SubstackSource(name="first"), SubstackSource(name="second"))
    )
    assert [a["title"] for a in articles] == [
        "Older but first in the list", "Newest", "Middle",
    ]


def test_full_feed_url_is_used_as_given(feeds, no_imap, fake_state):
    feeds["https://example.com/rss"] = build_feed(
        "Elsewhere", "elsewhere",
        [{"title": "Hello", "slug": "h", "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"}],
    )
    articles = substack.fetch(
        settings_for(SubstackSource(name="https://example.com/rss"))
    )
    assert [a["title"] for a in articles] == ["Hello"]


def test_one_broken_feed_does_not_lose_the_others(monkeypatch, feeds, no_imap, fake_state):
    def fake_get(url):
        if "broken" in url:
            raise OSError("connection reset")
        return (feeds[url])

    monkeypatch.setattr(substack, "_get", fake_get)
    articles = substack.fetch(
        settings_for(SubstackSource(name="broken"), SubstackSource(name="slowkitchen"))
    )
    assert [a["title"] for a in articles] == ["The bread you meant to make"]


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 1, 3, 20, tzinfo=timezone.utc), "Jan. 3"),
        (datetime(2026, 3, 9, 20, tzinfo=timezone.utc), "March 9"),
        (datetime(2026, 5, 31, 20, tzinfo=timezone.utc), "May 31"),
        (datetime(2026, 7, 4, 20, tzinfo=timezone.utc), "July 4"),
        (datetime(2026, 9, 15, 20, tzinfo=timezone.utc), "Sept. 15"),
        (datetime(2026, 12, 25, 20, tzinfo=timezone.utc), "Dec. 25"),
    ],
)
def test_published_formatting(when, expected):
    assert substack._format_published(when) == expected


def test_deck_rejects_a_truncated_body():
    entry = {"description": "There is a loaf that exists only in the future…"}
    assert substack._deck(entry, ["There is a loaf that exists only in the future"]) is None
    entry = {"description": "There is a loaf"}
    assert substack._deck(entry, ["There is a loaf that exists only in the future"]) is None
    entry = {"description": "x" * 201}
    assert substack._deck(entry, ["Something else entirely."]) is None


def test_fetch_does_not_write_state(feeds, no_imap, fake_state):
    substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert fake_state["seen_posts"] == []      # run.py owns that, after delivery
    substack.mark_seen([BREAD])
    assert fake_state["seen_posts"] == [BREAD]
    substack.mark_seen([BREAD, SECOND_CUP])    # idempotent
    assert fake_state["seen_posts"] == [BREAD, SECOND_CUP]


def test_check(feeds):
    result = substack.check("slowkitchen")
    assert result == {
        "publication": "The Slow Kitchen",
        "latest_title": "The bread you meant to make",
        "paid_marker_seen": False,
    }


def test_no_sources_is_no_articles(fake_state):
    assert substack.fetch(settings_for()) == []
