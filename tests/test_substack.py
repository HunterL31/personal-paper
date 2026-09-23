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
from datetime import datetime, timedelta, timezone

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
def settings_for(*sources: SubstackSource, days: int | None = None) -> Settings:
    kwargs = {"substacks": list(sources)}
    if days is not None:
        kwargs["article_max_age_days"] = days
    return Settings(sources=Sources(**kwargs))


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
    assert article["url"] == BREAD
    assert set(article) == {
        "title", "deck", "author", "publication", "published", "paragraphs",
        "images", "url", "guid",
    }


# ------------------------------------------------------------ pictures
BREAD_IMAGES = [
    {"url": "https://substackcdn.com/image/fetch/loaf.jpg",
     "caption": "A loaf, photographed badly, on a Tuesday.", "after": 0},
    {"url": "https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp,q_auto:good/"
            "https%3A%2F%2Fbucket%2Fdough.png",
     "caption": None, "after": 4},
]


def test_pictures_are_recorded_where_the_author_set_them(feeds, no_imap, fake_state):
    """The two figures of the bread post: the captioned one at the top, the
    bare one after the blockquote (four paragraphs in). The emoji inside the
    last paragraph is decoration, not a picture -- and not a word moves."""
    (article,) = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert article["images"] == BREAD_IMAGES
    assert article["paragraphs"] == BREAD_PARAGRAPHS


def test_extract_content_tells_pictures_from_decoration():
    html = (
        "<p>One.</p>"
        "<div class='captioned-image-container'><figure>"
        "<a class='image-link' href='x'><img src='https://cdn/a.jpg' width='1200' height='800'></a>"
        "<figcaption>Caption <em>one</em>.</figcaption></figure></div>"
        "<p>Two <img src='https://cdn/emoji.png' width='16' height='16'> words.</p>"
        "<img src='https://cdn/bare.jpg'>"                       # a bare picture is one too
        "<figure><figcaption>No picture here</figcaption></figure>"  # chrome
        "<img src='https://cdn/icon.png' width='40' height='40'>"    # an icon
        "<img src='data:image/png;base64,AAAA'>"                     # not an address
        "<blockquote><p>Three.</p></blockquote>"
    )
    paragraphs, images = substack.extract_content(html)
    assert paragraphs == ["One.", "Two words.", "Three."]
    assert images == [
        {"url": "https://cdn/a.jpg", "caption": "Caption one.", "after": 1},
        {"url": "https://cdn/bare.jpg", "caption": None, "after": 2},
    ]
    assert "No picture here" not in " ".join(paragraphs)


def test_the_email_edition_keeps_its_pictures_too(monkeypatch, feeds, fake_state, fixtures):
    monkeypatch.setattr(substack, "fetch_from_imap",
                        lambda title, publication: (fixtures / "substack_email.html").read_text())
    articles = substack.fetch(settings_for(SubstackSource(name="slowkitchen", paid=True)))
    cup = {a["guid"]: a for a in articles}[SECOND_CUP]
    # The logo in the email's header is chrome (and icon-sized); the one
    # figure in the body is a picture, after the last paragraph.
    assert cup["images"] == [{
        "url": "https://substackcdn.com/image/fetch/cup.jpg",
        "caption": "The second cup, in the only mug that matters.",
        "after": 5,
    }]


def test_url_is_a_plain_link(feeds, no_imap, fake_state):
    """The URL is printed on paper, so no tracking tail rides along."""
    feeds["https://tracked.substack.com/feed"] = build_feed(
        "Tracked", "tracked",
        [{"title": "Hello", "slug": "hello", "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"}],
    ).replace(
        b"<link>https://tracked.substack.com/p/hello</link>",
        b"<link>https://tracked.substack.com/p/hello?utm_source=substack&amp;utm_medium=email</link>",
    )
    (article,) = substack.fetch(settings_for(SubstackSource(name="tracked")))
    assert article["url"] == "https://tracked.substack.com/p/hello"


def test_post_older_than_a_week_is_skipped(feeds, no_imap, fake_state):
    articles = substack.fetch(settings_for(SubstackSource(name="slowkitchen")))
    assert TOMATOES not in [a["guid"] for a in articles]


def test_the_window_comes_from_the_settings(feeds, no_imap, fake_state):
    """A post eight days old: out with the default 7, in with 10."""
    feeds["https://window.substack.com/feed"] = build_feed(
        "Window", "window",
        [{"title": "Eight days ago", "slug": "eight",
          "pubdate": "Mon, 08 Sep 2025 15:00:00 GMT"},      # NOW is Sept 16, 15:00
         {"title": "Yesterday", "slug": "yesterday",
          "pubdate": "Mon, 15 Sep 2025 15:00:00 GMT"}],
    )
    source = SubstackSource(name="window")

    default_window = substack.fetch(settings_for(source))
    assert [a["title"] for a in default_window] == ["Yesterday"]

    wider = substack.fetch(settings_for(source, days=10))
    assert [a["title"] for a in wider] == ["Eight days ago", "Yesterday"]


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


def test_queue_is_capped_and_the_rest_are_logged(feeds, no_imap, fake_state, caplog):
    items = [
        {
            "title": f"Post {n}",
            "slug": f"post-{n}",
            "pubdate": f"Mon, 1{n} Sep 2025 16:00:00 GMT",
            "body": f"The body of post {n}.",
        }
        for n in range(6)
    ]
    items += [
        {"title": f"Extra {n}", "slug": f"extra-{n}",
         "pubdate": f"Mon, 1{n} Sep 2025 18:00:00 GMT", "body": f"Extra body {n}."}
        for n in range(4)
    ]
    feeds["https://daily.substack.com/feed"] = build_feed("Daily", "daily", items)
    with caplog.at_level("INFO"):
        articles = substack.fetch(settings_for(SubstackSource(name="daily")))

    assert len(articles) == substack.QUEUE_LIMIT == 8
    # ten posts inside the window, oldest first: the two newest wait.
    assert [a["title"] for a in articles[:2]] == ["Post 0", "Extra 0"]
    assert [a["title"] for a in articles[-2:]] == ["Post 3", "Extra 3"]
    assert "2 post(s) still in the queue" in caplog.text
    assert "'Post 5'" in caplog.text


def test_queue_order_is_source_order_then_oldest_first(feeds, no_imap, fake_state):
    """Source order decides between publications; inside one, the queue."""
    feeds["https://first.substack.com/feed"] = build_feed(
        "First", "first",
        [{"title": "First, newer", "slug": "a",
          "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"},
         {"title": "First, older", "slug": "b",
          "pubdate": "Sat, 13 Sep 2025 16:00:00 GMT"}],
    )
    feeds["https://second.substack.com/feed"] = build_feed(
        "Second", "second",
        [{"title": "Second, newest", "slug": "c", "pubdate": "Tue, 16 Sep 2025 10:00:00 GMT"},
         {"title": "Second, oldest", "slug": "d", "pubdate": "Fri, 12 Sep 2025 09:00:00 GMT"},
         {"title": "Second, middle", "slug": "e", "pubdate": "Mon, 15 Sep 2025 09:00:00 GMT"}],
    )
    articles = substack.fetch(
        settings_for(SubstackSource(name="first"), SubstackSource(name="second"))
    )
    assert [a["title"] for a in articles] == [
        "First, older", "First, newer",
        "Second, oldest", "Second, middle", "Second, newest",
    ]


def test_every_article_carries_a_url(feeds, no_imap, fake_state):
    feeds["https://first.substack.com/feed"] = build_feed(
        "First", "first",
        [{"title": "One", "slug": "one", "pubdate": "Mon, 15 Sep 2025 09:00:00 GMT"}],
    )
    articles = substack.fetch(
        settings_for(SubstackSource(name="first"), SubstackSource(name="slowkitchen"))
    )
    assert [a["url"] for a in articles] == [
        "https://first.substack.com/p/one", BREAD,
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


# -------------------------------------------------------- queue preview
def test_queue_preview_matches_what_fetch_would_offer(feeds, no_imap, fake_state):
    """The preview is the queue: same order, numbered from 1."""
    feeds["https://first.substack.com/feed"] = build_feed(
        "First", "first",
        [{"title": "First, newer", "slug": "a",
          "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"},
         {"title": "First, older", "slug": "b",
          "pubdate": "Sat, 13 Sep 2025 16:00:00 GMT"}],
    )
    feeds["https://second.substack.com/feed"] = build_feed(
        "Second", "second",
        [{"title": "Second, newest", "slug": "c", "pubdate": "Tue, 16 Sep 2025 10:00:00 GMT"},
         {"title": "Second, oldest", "slug": "d", "pubdate": "Fri, 12 Sep 2025 09:00:00 GMT"}],
    )
    settings = settings_for(SubstackSource(name="first"), SubstackSource(name="second"))

    preview = substack.queue_preview(settings)
    assert preview["window_days"] == 7
    assert [row["title"] for row in preview["queued"]] == [
        a["title"] for a in substack.fetch(settings)
    ]
    assert [row["position"] for row in preview["queued"]] == [1, 2, 3, 4]
    assert {row["status"] for row in preview["queued"]} == {"queued"}
    assert preview["errors"] == [] and preview["printed"] == []

    first = preview["queued"][0]
    assert first["publication"] == "First"
    assert first["url"] == "https://first.substack.com/p/b"
    assert first["published"] == "2025-09-13"       # Sept 13 in America/Los_Angeles
    assert first["age_days"] == 3.0          # Sept 13 16:00 UTC, rounded
    assert set(first) == {
        "guid", "publication", "title", "url", "published", "age_days", "position", "status",
        "printed",
    }
    assert first["printed"] is None


def test_queue_preview_marks_printed_posts(feeds, no_imap, fake_state):
    fake_state["seen_posts"] = [BREAD]
    preview = substack.queue_preview(settings_for(SubstackSource(name="slowkitchen")))
    assert preview["queued"] == []
    assert [(row["title"], row["status"], row["position"]) for row in preview["printed"]] == [
        ("The bread you meant to make", "printed", None)
    ]


# ----------------------------------------------------- the printed record
def _daily_feed(feeds) -> None:
    feeds["https://daily.substack.com/feed"] = build_feed("Daily", "daily", [
        {"title": "Post A", "slug": "a", "pubdate": "Sun, 14 Sep 2025 16:00:00 GMT"},
        {"title": "Post B", "slug": "b", "pubdate": "Mon, 15 Sep 2025 16:00:00 GMT"},
        {"title": "Post C", "slug": "c", "pubdate": "Sat, 13 Sep 2025 16:00:00 GMT"},
        {"title": "Post D", "slug": "d", "pubdate": "Tue, 16 Sep 2025 09:00:00 GMT"},
    ])


def _daily(slug: str) -> str:
    return f"https://daily.substack.com/p/{slug}"


def test_printed_posts_are_listed_as_the_server_printed_them(monkeypatch, feeds, no_imap, fake_state):
    """Not by when they were published: the latest paper first, and within a
    paper the sheet's order, the lead first. Each row says the day it went out."""
    _daily_feed(feeds)
    # Monday's paper carried B (the lead) then A; Tuesday's carried D.
    monkeypatch.setattr(substack, "_now", lambda: NOW - timedelta(days=1))
    substack.mark_seen([_daily("b"), _daily("a")])
    monkeypatch.setattr(substack, "_now", lambda: NOW)
    substack.mark_seen([_daily("d")])

    view = substack.queue_preview(settings_for(SubstackSource(name="daily")))
    assert [r["title"] for r in view["printed"]] == ["Post D", "Post B", "Post A"]
    assert [r["printed"] for r in view["printed"]] == ["2025-09-16", "2025-09-15", "2025-09-15"]
    assert [r["title"] for r in view["queued"]] == ["Post C"]
    assert view["queued"][0]["printed"] is None


def test_posts_printed_before_there_were_stamps_still_list_latest_first(feeds, no_imap, fake_state):
    """A state file from before `printed_at`: the order of `seen_posts` is
    the order they printed, so the last marked is listed first, undated."""
    _daily_feed(feeds)
    fake_state["seen_posts"] = [_daily("a"), _daily("d"), _daily("b")]
    view = substack.queue_preview(settings_for(SubstackSource(name="daily")))
    assert [r["title"] for r in view["printed"]] == ["Post B", "Post D", "Post A"]
    assert [r["printed"] for r in view["printed"]] == [None, None, None]


def test_stamped_papers_come_before_unstamped_posts(monkeypatch, feeds, no_imap, fake_state):
    _daily_feed(feeds)
    fake_state["seen_posts"] = [_daily("d")]            # printed before the stamps
    substack.mark_seen([_daily("a")])
    view = substack.queue_preview(settings_for(SubstackSource(name="daily")))
    assert [(r["title"], r["printed"]) for r in view["printed"]] == [
        ("Post A", "2025-09-16"), ("Post D", None),
    ]


def test_mark_seen_stamps_the_time_and_mark_unseen_forgets_it(data_dir, monkeypatch):
    import state
    from gather.substack import mark_seen, mark_unseen

    mark_seen(["a", "b"])
    stamps = state.load_state()["printed_at"]
    assert set(stamps) == {"a", "b"}
    assert stamps["a"] == stamps["b"] == "2025-09-16T08:00:00-07:00"   # NOW, in the reader's zone

    # Marking a post that is already printed keeps the stamp it has.
    monkeypatch.setattr(substack, "_now", lambda: NOW + timedelta(days=1))
    mark_seen(["a", "c"])
    stamps = state.load_state()["printed_at"]
    assert stamps["a"] == "2025-09-16T08:00:00-07:00"
    assert stamps["c"] == "2025-09-17T08:00:00-07:00"

    mark_unseen(["a"])
    st = state.load_state()
    assert st["seen_posts"] == ["b", "c"] and set(st["printed_at"]) == {"b", "c"}


def test_the_stamps_are_pruned_with_the_history(data_dir, monkeypatch):
    import state
    from gather.substack import mark_seen

    monkeypatch.setattr(substack, "SEEN_HISTORY", 2)
    mark_seen(["a", "b", "c"])
    st = state.load_state()
    assert st["seen_posts"] == ["b", "c"] and set(st["printed_at"]) == {"b", "c"}


def test_an_odd_printed_at_in_the_state_file_is_ignored(data_dir):
    """A hand-edited or corrupt value never stops the queue view."""
    import state
    from gather.substack import mark_seen

    state.update_state(printed_at="not a dict", seen_posts=["a"])
    assert substack._printed_at() == {}
    mark_seen(["b"])
    assert set(state.load_state()["printed_at"]) == {"b"}
    state.update_state(printed_at={"b": 12345})
    assert substack._printed_at() == {}
    assert substack._printed_day("not a time") is None


def test_queue_preview_flags_a_preview_and_an_old_post(feeds, no_imap, fake_state):
    """The paid post with no email route, and the one out of the window."""
    preview = substack.queue_preview(
        settings_for(SubstackSource(name="slowkitchen", paid=True))
    )
    by_title = {row["title"]: row for row in preview["skipped"]}
    assert by_title["The case for the second cup"]["status"] == "preview-only"
    old = by_title["What to do with the end of the summer tomatoes"]
    assert old["status"] == "too-old"
    assert old["age_days"] > 7
    assert [row["title"] for row in preview["queued"]] == ["The bread you meant to make"]


def test_queue_preview_does_not_write_state(feeds, no_imap, fake_state):
    substack.queue_preview(settings_for(SubstackSource(name="slowkitchen")))
    assert fake_state["seen_posts"] == []


def test_queue_preview_reports_a_broken_feed_and_keeps_the_rest(
    monkeypatch, feeds, no_imap, fake_state
):
    def fake_get(url):
        if "broken" in url:
            raise OSError("connection reset")
        return feeds[url]

    monkeypatch.setattr(substack, "_get", fake_get)
    preview = substack.queue_preview(
        settings_for(SubstackSource(name="broken"), SubstackSource(name="slowkitchen"))
    )
    assert preview["errors"] == [
        {"publication": "broken", "error": "OSError: connection reset"}
    ]
    assert [row["title"] for row in preview["queued"]] == ["The bread you meant to make"]
    assert preview["queued"][0]["position"] == 1


def test_queue_preview_marks_the_posts_beyond_the_limit(feeds, no_imap, fake_state):
    items = [
        {"title": f"Post {n}", "slug": f"post-{n}",
         "pubdate": f"Mon, 1{n} Sep 2025 16:00:00 GMT", "body": f"The body of post {n}."}
        for n in range(6)
    ]
    items += [
        {"title": f"Extra {n}", "slug": f"extra-{n}",
         "pubdate": f"Mon, 1{n} Sep 2025 18:00:00 GMT", "body": f"Extra body {n}."}
        for n in range(4)
    ]
    feeds["https://daily.substack.com/feed"] = build_feed("Daily", "daily", items)
    preview = substack.queue_preview(settings_for(SubstackSource(name="daily")))

    assert len(preview["queued"]) == 10          # nothing is dropped from the view
    printable = preview["queued"][:substack.QUEUE_LIMIT]
    assert [row["position"] for row in printable] == list(range(1, 9))
    assert {row["status"] for row in printable} == {"queued"}
    waiting = preview["queued"][substack.QUEUE_LIMIT:]
    assert [(row["status"], row["position"]) for row in waiting] == [
        ("beyond-limit", None), ("beyond-limit", None)
    ]
    assert [row["title"] for row in waiting] == ["Post 4", "Post 5"]


def test_mark_unseen_puts_a_post_back_in_the_queue(data_dir):
    import state
    from gather.substack import mark_seen, mark_unseen

    mark_seen(["a", "b", "c"])
    mark_unseen(["b", "zzz"])
    assert state.load_state()["seen_posts"] == ["a", "c"]
    mark_unseen([])                                  # a no-op, never raises
    assert state.load_state()["seen_posts"] == ["a", "c"]


def test_queue_rows_carry_the_guid_for_marking(data_dir, monkeypatch, fixtures):
    """Every row the page can mark carries the post's identifier."""
    from app.settings import Settings, SubstackSource
    from gather import substack

    feed = (fixtures / "substack_feed.xml").read_bytes()
    monkeypatch.setattr(substack, "_get", lambda url: feed)
    s = Settings()
    s.sources.substacks = [SubstackSource(name="slowkitchen")]
    view = substack.queue_preview(s)
    rows = view["queued"] + view["printed"] + view["skipped"]
    assert rows and all(r.get("guid") for r in rows)


def test_an_empty_window_can_look_further_back(feeds, no_imap, fake_state):
    """Nothing unread within 7 days: with the option on, the paper widens the
    window step by step and prints the oldest unread post it finds."""
    feeds["https://window.substack.com/feed"] = build_feed(
        "Window", "window",
        [{"title": "Twenty days ago", "slug": "twenty",
          "pubdate": "Wed, 27 Aug 2025 15:00:00 GMT"},      # NOW is Sept 16, 15:00
         {"title": "Twenty-five days ago", "slug": "twentyfive",
          "pubdate": "Fri, 22 Aug 2025 15:00:00 GMT"}],
    )
    source = SubstackSource(name="window")

    assert substack.fetch(settings_for(source)) == []

    s = settings_for(source)
    s.sources.extend_window_when_empty = True
    found = substack.fetch(s)
    assert [a["title"] for a in found] == ["Twenty-five days ago", "Twenty days ago"]

    view = substack.queue_preview(s)
    assert view["window_extended"] is True and view["window_days"] == 30
    assert [r["title"] for r in view["queued"]] == ["Twenty-five days ago", "Twenty days ago"]
    assert view["queued"][0]["position"] == 1
    assert not [r for r in view["skipped"] if r["status"] == "too-old"]

    plain = substack.queue_preview(settings_for(source))
    assert plain["window_extended"] is False and plain["queued"] == []
    assert {r["status"] for r in plain["skipped"]} == {"too-old"}


def test_a_post_printed_from_a_widened_window_is_reported_as_printed(
    feeds, no_imap, fake_state
):
    """A post found by looking further back is older than the window by
    definition. Once it has been printed the queue view has to say so:
    before, the age test ran first and filed it under "older than 7 days",
    where the reader could neither see that it had printed nor mark it
    unread."""
    feeds["https://window.substack.com/feed"] = build_feed(
        "Window", "window",
        [{"title": "Twenty days ago", "slug": "twenty",
          "pubdate": "Wed, 27 Aug 2025 15:00:00 GMT"},      # NOW is Sept 16, 15:00
         {"title": "Twenty-five days ago", "slug": "twentyfive",
          "pubdate": "Fri, 22 Aug 2025 15:00:00 GMT"}],
    )
    source = SubstackSource(name="window")
    s = settings_for(source)
    s.sources.extend_window_when_empty = True

    printed = substack.fetch(s)[0]           # the oldest one, as the paper would
    substack.mark_seen([printed["guid"]])

    view = substack.queue_preview(s)
    assert [(r["title"], r["status"]) for r in view["printed"]] == [
        ("Twenty-five days ago", "printed")
    ]
    assert not [r for r in view["skipped"] if r["title"] == "Twenty-five days ago"]
    # and it is not offered again, whatever the view says
    assert [a["title"] for a in substack.fetch(s)] == ["Twenty days ago"]


def test_an_old_printed_post_is_printed_not_too_old(feeds, no_imap, fake_state):
    """The same rule without the look-back option: having been printed is a
    fact about the post, not about the window."""
    feeds["https://window.substack.com/feed"] = build_feed(
        "Window", "window",
        [{"title": "Twenty days ago", "slug": "twenty",
          "pubdate": "Wed, 27 Aug 2025 15:00:00 GMT"}],
    )
    source = SubstackSource(name="window")
    substack.mark_seen(["https://window.substack.com/p/twenty"])

    view = substack.queue_preview(settings_for(source))
    assert [r["status"] for r in view["printed"]] == ["printed"]
    assert view["skipped"] == [] and view["queued"] == []


def test_wider_windows_step_up_from_the_setting():
    assert substack.wider_windows(7) == [14, 30, 60, 90, 180, 365]
    assert substack.wider_windows(60) == [90, 180, 365]
    assert substack.wider_windows(365) == []
