# Content sources — plans for the meat of the paper

Today `articles` is Substack and nothing else. The calendar, the weather, the
lists and the crossword are furniture: they fill the rail and the back page,
and on a morning with no unread post the paper is one sheet with a puzzle on
the front. This document lays out where else the front page could come
from, what each source would cost, and the one refactor they all need
first. It is a plan, not a change; nothing here is built yet.

Read `PLAN.md` first. The house rules in `CLAUDE.md` decide most of what
follows, and the ones that bite hardest are rule 1 (verbatim, whole
paragraphs, nothing reworded) and rule 2 (no LLM), because together they
rule out every source that offers a summary, a snippet, or a headline with
a link.

## Two tests every source has to pass

**It is easy to switch on.** The reader adds it from the Sources tab in
under a minute, understands what they typed, and the Check button tells
them in plain words what will print tomorrow. Concretely: a website
address, a title from a list, or a switch. No mail servers, no app
passwords, no tokens copied out of a developer console. The one credential
the plan asks for (a free Guardian key) is a form with a name and an email
on it, and it is the least easy thing here. That test is why the email
route is not in this plan: fetching newsletters over IMAP would bring the
most text for the least code, and it asks the reader to create a Gmail
label, a filter and an app password and to type an IMAP host into a
container variable. The paid-Substack route that already does this stays
as it is; nothing new is built on it.

**It gives us the whole text.** For each story, all of:

- **The whole text**, as the author published it, as HTML or plain text
  that splits cleanly into paragraphs. A feed that carries only a
  description, an API that returns an abstract, or a page behind a paywall
  gives us nothing printable. (The one exception, "the rest is online",
  is the *layout* stopping at a paragraph boundary because the sheet is
  full; a source may never hand over a text that was already cut.)
- **A stable identifier** for the queue: printed once, never repeated,
  held for another day when it did not fit.
- **A date**, so the window ("skip posts older than … days") applies.
- **A `url`**, the address printed under a story that only partly fit.
- **A title, an author and a publication name**, verbatim.

And the operational rules: a failing source yields an empty list and a log
line; every fetch fits inside the gather step's 30 s budget
(`gather.TIMEOUT_SECONDS`, shared by every gatherer); credentials are
container variables; the reader's choices are settings; tests run offline
against a fixture.

## The foundation: one queue, many kinds

Two things in the code assume articles are Substack posts, and both have
to go before a second source can exist:

- `gather.GATHERERS["articles"]` points at `gather.substack.fetch`, and
  `run.py` imports `gather.substack.mark_seen` by name to record what
  printed.
- `settings.sources.substacks` is the only ordered list of publications,
  and the Sources tab, the queue view and the Check button are built on
  it.

The refactor, which changes nothing the reader can see and must leave the
default render byte-identical:

### `gather/articles.py` — the merged queue

`fetch(settings)` asks each source's adapter for its unread articles,
applies that source's per-morning cap, and returns the first
`QUEUE_LIMIT`, exactly as `substack.fetch` does today. The widened-window
fallback (`extend_window_when_empty`) stays and applies only to kinds
where "further back" makes sense (websites and newsletters, not news).
Sources are fetched concurrently on daemon threads with a per-source
timeout of about 10 s, so six feeds, an API call and a book still fit
the 30 s budget — the Open-Meteo retry budget in `docs/AGENT-NOTES.md` is
the precedent.

`queue_preview(settings)` and the Sources tab's queue view merge the
adapters' rows the same way, with a column saying where each row came
from.

`record_printed(settings, [(guid, paragraphs_or_None), ...])` is what
`run.py` calls after a delivered run instead of `mark_seen`: it marks the
guids seen and then gives each adapter the chance to act on its own
articles (the book advances its bookmark; a saved link is filed as read).

### `gather/sources/<kind>.py` — one adapter per kind

```python
def fetch(source, seen: set[str], window_days: int) -> list[dict]
    # articles in the render contract's shape, plus `guid`, in this kind's
    # own order; never raises for one bad post, may raise for a dead source
def check(source) -> dict
    # the Sources tab's Check button, in the reader's words
def printed(source, guid: str, paragraphs: int | None) -> None
    # optional; called by record_printed after a delivered run
```

`gather/substack.py` becomes the first adapter, moved rather than
rewritten, with its tests moved with it.

### `gather/extract.py` — the shared extractor

`extract_paragraphs`, `_text_excluding_blocks`, `_clean` and the chrome
lists move here. Every adapter that starts from HTML uses the same walk
(`BLOCK_TAGS` in document order, inline tags flattened, `<br>` a space)
with a chrome-selector list per kind. **Chrome is removed by selector,
never by judging the text.** The verbatim test in `tests/verbatim.py`
covers whatever comes out.

### `gather/seen.py` — the shared state

`seen()`, `mark_seen()`, `mark_unseen()` over `state.json["seen_posts"]`.
The key keeps its name so an existing state file keeps working. Substack
guids stay as they are; every new kind prefixes its own (`feed:<id>`,
`wiki:<pageid>`, `reddit:<post id>`, `book:<slug>:<from>-<to>`) so nothing
collides.

### Settings: what the reader sees

The Sources tab grows from one table to five short groups, and the
settings follow them. Every knob that only an engineer would understand
(per-kind window, sort order, chrome lists) is a default in code, not a
field on the page.

```python
class Publication(BaseModel):          # "Websites and newsletters"
    address: str                       # a site, a Substack name, or a feed URL
    paid: bool = False                 # shown only when Check says it is a Substack
    per_morning: int = Field(0, ge=0, le=4)   # 0 = as many as fit

class BookSource(BaseModel):           # "A book, a little each morning"
    enabled: bool = False
    url: str = ""                      # the Standard Ebooks page picked from the list
    paragraphs_per_morning: int = Field(30, ge=5, le=120)

class WikipediaSource(BaseModel):      # "Something to read every day"
    enabled: bool = False
    pick: Literal["featured", "random_featured", "on_this_day"] = "featured"

class SavedLinks(BaseModel):           # "Saved from your phone"
    enabled: bool = True               # the endpoint is always there; this is whether it prints

class SubredditSource(BaseModel):      # "Subreddits"
    name: str                          # "nosleep", with or without the r/
    mode: Literal["posts", "answers"] = "posts"   # answers: the Q&A mode, v2
    allow_over_18: bool = False
    per_morning: int = Field(1, ge=1, le=4)

class GuardianSource(BaseModel):       # "The news"
    enabled: bool = False
    api_key: str = ""                  # pasted on the tab, masked after save (see below)
    sections: list[str] = ["world"]
    per_morning: int = Field(2, ge=1, le=4)
```

`sources.substacks` is read for one more release and turned into
`publications` rows on load, the way `_lists_from_tasks` rebuilds the
to-do list from a pre-lists file.

**Front-page priority** in v1 is fixed and stated on the tab: the reader's
own picks first (saved links, then the websites table in its order, then
the subreddits), then the news, then Wikipedia, then the book — the book
last because it is the story that continues tomorrow and so the right one
to cut. A drag-to-order
list across groups is a later refinement if anyone wants it.

### Contract: one new field, defaulting to today

`article.rest`, optional: `"online"` (the default, and today's line, "The
rest of this story is online: <url>") or `"tomorrow"` ("Continued
tomorrow.", for the book). `onlineLine()` in `render/template.html`
picks the wording; with the field absent the render is byte-identical to
today, which the PNG tests check.

Nothing else in the contract changes. `publication` already carries
whatever name the kind gives it ("The Guardian · World", the site's own
title, the book's author).

## The sources

In the order they should be built, which is also, roughly, the order of
how easy they are to switch on.

### 1. Websites and newsletters, by address (`feed`)

The reader pastes the address of a site or a newsletter — `kottke.org`,
`platformer.news`, `someone.beehiiv.com`, a Ghost or Buttondown letter, a
WordPress blog — and the paper finds the feed itself. This is the
Substack table generalised, and for the reader it is the same table with a
wider welcome.

**Finding the feed.** Fetch the page and read its
`<link rel="alternate" type="application/rss+xml|application/atom+xml">`;
failing that, try the usual paths (`/feed`, `/rss`, `/feed.xml`,
`/atom.xml`, `/index.xml`, `/rss.xml`). A `*.substack.com` address, or a
feed whose generator says Substack, is handled by the Substack adapter
exactly as today, Paid checkbox and all; the reader never picks a kind.
Check reports, in words: the feed's title, the latest post's title, and
whether posts arrive whole or as previews.

**Whole posts or previews.** The one new piece of logic: a feed with no
`content:encoded`, or a short body ending in a "read more" link, carries
previews. Such a site yields nothing and Check says so plainly ("This
site's feed carries only the opening of each post, so nothing from it can
be printed"). Nothing is scraped from the post's page in v1; the reader
is told rather than given a stub or a guess.

**Presets.** A dropdown beside the address box, "Or pick one", with a
handful of publications whose feeds are known to carry whole articles and
whose licences forbid editing — house rule 1 in someone else's words:

- **ProPublica**, `https://www.propublica.org/feeds/propublica/main`:
  RSS 2.0 with the complete piece in `content:encoded` (verified; 15–40
  paragraphs an item), CC BY-NC-ND.
- **The Conversation**, `https://theconversation.com/us/articles.atom`,
  with per-section feeds: Atom with the complete article in `content` and
  a `<rights>` line, CC BY-ND, 15–30 paragraphs an item (verified).

Both publishers' republishing pages address other publishers and ask that
stories be picked one at a time rather than mirrored wholesale. One
household's single printed copy is reading, not republishing, but the
terms are worth knowing before anyone adds a "print everything" switch;
`per_morning` is the reader's own throttle. Presets live in
`gather/presets.py` as name, feed URL and chrome list, so adding one is a
three-line change.

**Effort.** Small. `substack.feed_url` already accepts a full feed URL;
the adapter is `substack.py` without the paid branch and with a generic
chrome list (share bars, "related" blocks, `<figure>`, `<iframe>`), plus
the discovery step. Fixtures: a Beehiiv feed, a Ghost feed, a WordPress
summary-only feed, a page with a feed link in its head, the ProPublica and
Conversation feeds.

### 2. Something to read every day: Wikipedia (`wikipedia`)

One switch. Zero credentials, always available, and the article is long
enough that it is always the partial story at the end of the sheet, which
suits a reference article: the reader gets the lead and the first
sections, and the address of the rest.

- The featured-content feed
  (`https://en.wikipedia.org/api/rest_v1/feed/featured/YYYY/MM/DD`,
  anonymous) names today's featured article, but its `extract` is a lead
  of about 220 words — a summary, not the article, so it is never printed
  as the text.
- The action API gives the whole thing:
  `api.php?action=query&prop=extracts&explaintext=1&titles=<title>`
  returns the full article as plain text with `== Section ==` headings and
  blank-line paragraphs (verified on *Persuasion (novel)*: some 15,000
  words).

**Picks** (a radio on the tab): today's featured article; a random
featured article; the article behind one of today's "On this day" events.
`guid` = page id plus revision; `url` the page; `published` today;
`author` empty; `publication` "Wikipedia". The heading marks (`==`) are
markup and are stripped, leaving the heading text as its own paragraph.
The apparatus sections at the end — References, See also, External links,
Notes, Bibliography — are cut by name: they are navigation, the same call
as removing a subscribe button. CC BY-SA.

**Effort.** Small. Fixtures: one featured-feed JSON, one extract JSON.

### 3. A book, a little each morning (`book`)

A public-domain novel, printed in installments, the way Dickens shipped.
The most newspaper-native idea in the list, and the source that guarantees
the paper is never one sheet with a puzzle on the front: there is always
a next installment. For the reader it is a title picked from a list and a
number of paragraphs a morning.

- **Standard Ebooks**: `https://standardebooks.org/ebooks/<author>/<title>/text/single-page`
  is one XHTML document, chapters as `<section epub:type="chapter">` with
  an `<h2>`, paragraphs as plain `<p>`, blockquotes where the author had
  them, CC0 (verified on *Persuasion*). The existing block walk reads it
  with no new extractor; chapter headings come through as paragraphs the
  way Substack `h2`s do. Their catalogue is an OPDS feed, so the picker can
  be a search box over titles and authors rather than a URL field; a short
  built-in list of well-known titles covers the first morning.
- **Project Gutenberg** for books Standard Ebooks lacks:
  `https://www.gutenberg.org/ebooks/<id>.txt.utf-8`, paragraphs at blank
  lines, the licence boilerplate cut at the `*** START OF` / `*** END OF`
  markers. Messier (hard-wrapped lines to rejoin, headings by heuristic),
  so second, and behind a "paste a Gutenberg address" field rather than
  the picker.

**How it queues.** The book is fetched once and cached as a paragraph list
under `<DATA_DIR>/books/<slug>.json`. `state.json["books"][slug]` holds
`{"next": <paragraph index>}`. Each morning the adapter offers one
article: the next `paragraphs_per_morning` paragraphs, or fewer to end at
a chapter boundary; `title` the book's title, `publication` the author,
`deck` the chapter heading when the installment opens one, `url` the
book's page, `rest: "tomorrow"`. It is last in priority, so it is the
story that gets cut when the sheet is full. `record_printed` advances
`next` by exactly the paragraphs that reached the sheet — all of them for
a whole installment, `RenderResult.partial`'s count for a cut one, none if
it did not fit at all. Paragraphs are never split, nothing is reworded,
and tomorrow resumes at the paragraph after the last one printed: rule 1
as the feature. Finishing the book is said on the sheet ("The End") and
on the tab, where the reader picks the next.

**What it changes.** The `rest` field and the wording in `onlineLine()`.

**Effort.** Small to medium. Fixtures: a short Standard Ebooks single-page
document, a Gutenberg text, a slice of the OPDS catalogue.

### 4. Saved from your phone (`saved`)

The link saved during the day is the story on the doorstep next morning.
The reader already has a Shortcut that posts a list to the paper; this is
one more, on the share sheet: "Get URLs from input", then `POST
/saved` with the same bearer rule as `POST /lists/<slug>`. The Sources
tab shows the address and header the Shortcut needs, the same box the
lists have, and under it the links waiting, each with its extracted
paragraph count and a Remove.

**How it works.** Saved links wait in `<DATA_DIR>/saved/`, one JSON each.
The page is fetched and the article extracted at save time, so the count
is on the tab within seconds and a link that cannot be printed is flagged
right away rather than at 6 a.m. `guid` = the URL; `url` the URL;
`published` the day it was saved; `title`, `author` and the site's name
from the page's own metadata (`og:title`, `author`, `og:site_name`),
falling back to the `<title>` and the host name.

**The extractor, and the one real rule 1 risk in this plan.** Pages have
no feed to hand us the body, so the article has to be found in the page.
`trafilatura` (pure Python on lxml, the best-scoring open extractor, run
with `favor_recall=True`) or `readability-lxml`, pinned in
`requirements.txt`. A heuristic extractor can drop a short paragraph it
scores as boilerplate. Mitigations: recall over precision; the queue view
always showing the paragraph count with the first and last paragraph, so
a gutted page is visible before it prints; and a plain rule that a
paywalled page — very short text plus subscribe or sign-in words — is
flagged "This page only shows its opening without signing in" and never
printed. When a site's feed is known (its `<link rel="alternate">`), the
feed's `content:encoded` for that URL is preferred over the page, because
a feed body is the author's HTML and needs no guessing.

**Effort.** Medium: the endpoint and tab furniture are the lists pattern
again; the extractor is the new dependency. Fixtures: three saved pages
(a plain blog post, a news article with heavy chrome, a paywalled stub).

### 5. Subreddits: yesterday's best (`reddit`)

The reader types a subreddit name — `nosleep`, `AskHistorians` — and the
paper prints the best of what it carried in the last day. Setting it up
is a name in a box, the easiest thing on the tab after a switch. Whether
the words belong on a broadsheet is the reader's call, subreddit by
subreddit; the paper prints what it is pointed at, whole.

**What Reddit hands out without an account.** Two doors, no key:

- The listing, `https://www.reddit.com/r/<sub>/top.json?t=day&limit=25`:
  one request, and for every post its title, author, score, `is_self`,
  `selftext_html` (the body as Reddit rendered the author's Markdown),
  `over_18`, `stickied`, `removed_by_category`, `permalink` and
  `created_utc`.
- The feed, `https://www.reddit.com/r/<sub>/top/.rss?t=day`: the same
  posts as Atom, a text post's body inside the entry's content beside a
  "submitted by" line of chrome. The feed is the door meant for feed
  readers and the more likely to stay open; the listing carries the flags
  the paper needs to choose well. Use the listing; fall back to the feed.

Both want a descriptive `User-Agent` (`personal-paper/<version> (by
u/<reader>)`; the `requests` default is refused) and both are held to
about ten requests a minute without a login. A morning needs one request
per subreddit, plus one per post in the Q&A mode.

Neither door could be opened from the container this plan was written in:
Reddit refuses most datacenter addresses, and the proxy here refuses the
host, so the shapes above are from Reddit's documentation and are to be
checked on the box. A home box on a residential connection is the normal
case and the one to test. The failure is a 403 or a 429, which yields an
empty section and a Check line in words ("Reddit refused the request; it
usually allows a home connection"), never a broken paper. Should Reddit
close the anonymous doors, a "script" app registered at
`reddit.com/prefs/apps` — client id and secret as `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET`, application-only OAuth, a hundred requests a
minute on the free tier — is the fallback. It fails the easy test, so it
is optional and documented, never required.

**What is printable.** A subreddit is three kinds of post, and only the
first prints as it is:

- **Text posts** (`is_self`): the author's own words. r/nosleep, r/HFY,
  r/shortscarystories, r/BestofRedditorUpdates, r/TrueOffMyChest, r/tifu —
  subreddits that are, in effect, magazines of short fiction and
  first-person essays. Title = the post's title; author = `u/<name>`;
  publication = `r/<sub>`; paragraphs = the block walk over
  `selftext_html` (entities decoded; Reddit's Markdown comes out as `p`,
  `blockquote`, `li` and headings, which the extractor already reads);
  `url` = the permalink; `published` from `created_utc`.
- **Link posts**: a title and someone else's page. Nothing to print unless
  that page is fetched and extracted, which is the saved-links extractor
  with its paywall problem. Not in v1; Check counts them ("12 posts, 4 of
  them text; the rest are links or pictures").
- **Image and video posts**: nothing to print on a black-and-white sheet
  of text.

**Choosing.** "Top of the day" fetched at 6 a.m. is the last 24 hours, so
the paper prints yesterday's best. Order is Reddit's own, by score; the
window is one day; `per_morning` defaults to 1 a subreddit. Skipped, each
with its reason in the queue view: stickied and moderator posts,
`over_18` unless the row allows it, removed or deleted bodies, posts
already printed, and bodies under a minimum (say 150 words) — a two-line
post is not a story, and declining to print it is selection, not editing.
Everything that prints, prints whole: "Edit: thanks for the gold" and all.

**Q&A subreddits (v2).** On r/AskHistorians, r/explainlikeimfive or
r/AskScience the post is a question and the meat is the best answer,
which is a comment: one more request per post
(`<permalink>.json?sort=top&depth=1`), skipping the moderators' stickied
notice and anything removed, and taking the highest-scored top-level
comment with enough text. The answer is the article, verbatim; the
question is its headline; the byline names both ("Asked by u/x, answered
by u/y"); the question's own body is the deck when it is one short
paragraph (the `_deck` rule) and is otherwise not printed. Printing the
answer and not the question is a choice of what to print, not an edit of
anything, but it is the one judgment in this section to settle before
building it.

**Effort.** Small for text posts: one JSON fixture, the shared extractor,
the "submitted by" chrome stripped on the feed fallback. A little more
for Q&A. Fixtures: a listing with a text post, a link post, an image
post, a stickied post, a removed post and an over-18 post; a comments
listing; a feed.

### 6. The news: The Guardian (`guardian`)

Most news is off the table. The New York Times APIs return abstracts and
URLs. AP and Reuters license their text; Reuters has no public feed since
2020. Almost every newspaper's RSS is a headline and a sentence, and the
page behind it is paywalled, React-rendered, or both.

**The Guardian Open Platform** is the exception: a free developer key for
non-commercial use, and `https://content.guardianapis.com/search` with
`show-fields=body,byline,standfirst` returns the article's whole body as
HTML, the deck as `standfirst`, the byline, `webUrl` and a stable `id`.
One call per section per morning, sections as checkboxes on the tab
(World, UK, US, Science, Culture, Books, Food, …), newest first, a
one-day window, `per_morning` defaulting to 2 so the news does not crowd
out the newsletters.

**The key.** Getting one is a short form (name, email, what for) and a
key by return email — the least easy step in this plan, and still a
five-minute job with the tab's instructions beside the box. Where it
lives is a decision to take: the key is a rate-limiting identifier rather
than a password, and the plan for the calendar's secret iCal addresses
(`PLAN.md`, "Settings and secrets") argued that such things belong to the
reader and go in the settings file, masked after save. Pasting it on the
Sources tab is the easy-to-switch-on answer; a `GUARDIAN_API_KEY`
container variable is the strict reading of house rule 5. Recommended:
the tab, with the variable honoured if set, so Unraid users can do
either.

The documentation pages could not be fetched from this container (the
proxy refuses the host), so the key's daily call limit and the exact
terms are to be checked at implementation; a morning needs a handful of
calls, far below any tier.

**Effort.** Small: one JSON fixture, the shared extractor on
`fields.body`.

### 7. Later, and for people who already have the thing

- **A folder** (`<DATA_DIR>/inbox/*.md`, `*.txt`): anything dropped there
  prints as an article and moves to `inbox/printed/`. Tiny to build, and
  a good fit for an Unraid share or a Syncthing folder — which is exactly
  who it is for; a reader who does not know what a share is never needs
  to see it.
- **Reading services** — Readwise Reader (`GET https://readwise.io/api/v3/list/`,
  `Authorization: Token`, `withHtmlContent=true`, 20 requests a minute;
  verified) and self-hosted Wallabag (`GET /api/entries` returns extracted
  `content`; verified) both hand over text the reader saved from a
  logged-in browser, paywalls and all. They need a token, which fails the
  first test, so they are an option for a reader who already uses one
  rather than a thing to recommend. Pocket shut down on 8 July 2025 and
  Omnivore in 2024; Instapaper's docs are script-rendered and could not
  be read here.
- **Poetry** — PoetryDB (public domain, JSON with a `lines[]` array),
  Poetry Foundation's and Poets.org's poem-of-the-day feeds. Line breaks
  are the text of a poem, and `_clean` collapses whitespace, so verse
  needs a contract extension (a `verse` flag; stanzas as paragraphs with
  newlines kept; `white-space: pre-line`; the fitting script forbidden to
  break inside a stanza). Not before the front page has prose.
- **Chronicling America** (Library of Congress): the OCR text of a
  newspaper from this day a hundred years ago. Fun, free, and the OCR is
  frequently garbage; a stretch.

### Not worth investigating again

| Source | Why not |
|---|---|
| Email newsletters over IMAP | The most text for the least code, and the hardest thing on the tab to set up: label, filter, app password, host in a container variable. Fails the first test. The paid-Substack route that exists stays as it is. |
| NYT with the reader's cookie | The crossword's line, crossed worse: a body scraped from a page that changes without notice, so it breaks silently and often. |
| NYT APIs | Abstracts and links only. |
| AP, Reuters | Licensees only; no public full text. |
| Apple News | No API at all. |
| Medium members-only posts | Truncated in the feed; free posts work by address. |
| X / Twitter | No. |
| Hacker News, Lobsters | Links only: everything depends on the page extractor and hits paywalls. (Reddit differs because text posts are whole articles; see source 5.) |
| Kindle / Readwise highlights | Excerpts by definition, so rail material, never the front page. |
| Pocket, Omnivore | Shut down. |

## Recommended order

1. **The foundation**, with Substack as the only adapter and no behaviour
   change: `articles.py`, `sources/substack.py`, `extract.py`, `seen.py`,
   `sources.publications` with the migration, the `rest` field with its
   default. Every existing test still passes; the sample PNGs are
   byte-identical.
2. **Websites and newsletters by address**, with feed discovery and the
   presets — the Substack table becomes the everything table.
3. **Wikipedia** — a switch, an afternoon, and the paper always has a
   story.
4. **The book** — the first user of `rest`, and the thing people will
   talk about.
5. **Saved from your phone** — the share-sheet Shortcut; brings in the
   page extractor, the one new dependency in the plan.
6. **Subreddits** — a name in a box; text posts first, the Q&A mode
   after. Test it from the box, not from a cloud container.
7. **The Guardian** — news on the front page, behind the one key.
8. The folder whenever it is convenient; services, verse and the rest
   later.

## Tests and fixtures

One fixture per adapter under `tests/fixtures/`, and for each: paragraphs
verbatim against the fixture (the `tests/verbatim.py` check), chrome
gone, the queue order the kind promises, seen guids skipped, the window
applied, a broken source leaving the others intact, `fetch` writing no
state. For feed discovery: a page with a feed link, one without, a
Substack address routed to the Substack adapter. For the book: the
bookmark advances by exactly the paragraphs printed, including zero, and
the end of the book is handled. For saved links: a paywalled stub is
flagged and never printed. For subreddits: a link post, an image post, a
stickied post, a removed post and an over-18 post are each skipped with
a reason, the text post prints whole, and a 403 from Reddit is an empty
section with a log line. For the merged queue: priority order across
kinds, `per_morning` caps, `QUEUE_LIMIT`, and a run whose
`record_printed` reaches the right adapter. For the template: `rest`
wording, and the default render unchanged.

## Things to ask the owner before starting

- Which sites and newsletters, beyond the Substacks already listed, so
  the discovery step and the chrome lists are built against real feeds?
- Which book first, and about how many paragraphs a morning?
- Which subreddits, if any — and is the box on a home connection? Reddit
  refuses most datacenter addresses, so the anonymous route has to be
  tried from where the paper actually runs.
- Does the news belong on the front page at all, and if so which Guardian
  sections?
- The Guardian key: on the Sources tab, or a container variable?
