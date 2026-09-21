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

## What a source has to give us

A source can feed the front page only if it yields, for each story, all of:

- **The whole text**, as the author published it, as HTML or plain text
  that splits cleanly into paragraphs. A feed that carries only a
  description, an API that returns an abstract, or a page behind a paywall
  gives us nothing printable. (The one exception, "the rest is online",
  is the *layout* stopping at a paragraph boundary because the sheet is
  full; a source may never hand over a text that was already cut.)
- **A stable identifier** for the queue: printed once, never repeated,
  held for another day when it did not fit.
- **A date**, so the window ("skip posts older than … days") applies.
- **A `url`**, the address printed under a story that only partly fit. It
  may be empty, in which case the template says the story is online
  without saying where; for some kinds a better line is needed (below).
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

`fetch(settings)` walks `settings.sources.publications` in order, asks
each publication's adapter for its unread articles, applies that
publication's per-issue cap, and returns the first `QUEUE_LIMIT`, exactly
as `substack.fetch` does today. The widened-window fallback
(`extend_window_when_empty`) stays and applies only to kinds where
"further back" makes sense (newsletters and feeds, not news).
Publications are fetched concurrently on daemon threads with a
per-publication timeout of about 10 s, so six feeds, a mailbox and an API
call still fit the 30 s budget — the Open-Meteo retry budget in
`docs/AGENT-NOTES.md` is the precedent.

`queue_preview(settings)` and the Sources tab's queue view merge the
adapters' rows the same way, with a Kind column.

`record_printed(settings, [(guid, paragraphs_or_None), ...])` is what
`run.py` calls after a delivered run instead of `mark_seen`: it marks the
guids seen and then gives each adapter the chance to act on its own
articles (the serial advances its bookmark; a reading-list service can
archive the item).

### `gather/sources/<kind>.py` — one adapter per kind

```python
def fetch(pub: Publication, seen: set[str], window_days: int) -> list[dict]
    # articles in the render contract's shape, plus `guid`, in this kind's
    # own order; never raises for one bad post, may raise for a dead source
def check(pub: Publication) -> dict
    # the Sources tab's Check button
def printed(pub: Publication, guid: str, paragraphs: int | None) -> None
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
`email:<message-id>`, `serial:<slug>:<from>-<to>`) so nothing collides.

### Settings: `sources.publications`

One ordered list, one row per publication, whatever its kind — order is
front-page priority, as it is today for Substacks:

```python
class Publication(BaseModel):
    kind: Literal["substack", "feed", "email", "guardian", "reading_list",
                  "serial", "wikipedia", "folder"]
    name: str                 # substack name, feed URL, mailbox folder or sender,
                              # Guardian section, book address, ...
    paid: bool = False        # substack only
    max_age_days: Optional[int] = None   # overrides sources.article_max_age_days;
                                         # news kinds default to 1
    order: Literal["oldest_first", "newest_first"] = "oldest_first"
    per_issue: int = 0        # 0 = no cap; 2 keeps a news section from
                              # crowding out the newsletters
    selector: str = ""        # feed / reading_list: the CSS container of the
                              # article body, when the page needs one
```

`sources.substacks` is read for one more release and turned into
`publications` rows on load, the way `_lists_from_tasks` rebuilds the
to-do list from a pre-lists file. The Sources tab gets a Kind dropdown per
row and shows only the fields the kind uses — the `EAR_KIND_FIELDS`
pattern on the Look tab.

Why `order` and `max_age_days` per publication: a newsletter is read in
the order it was written, oldest unread first, over a week; a news section
is read newest first and is stale after a day. One global rule cannot
serve both.

### Contract: one new field, defaulting to today

`article.rest`, optional: `"online"` (the default, and today's line, "The
rest of this story is online: <url>"), `"tomorrow"` ("Continued
tomorrow.", for the serial) or `"inbox"` ("The rest of this letter is in
your inbox.", for an email with no web address). `onlineLine()` in
`render/template.html` picks the wording; with the field absent the render
is byte-identical to today, which the PNG tests check.

Nothing else in the contract changes. `publication` already carries
whatever name the kind gives it ("The Guardian · World", the newsletter's
name, the book's author).

## The sources

Ordered roughly by how much front page they buy per hour of work.

### 1. Full-text feeds (`feed`)

Anything with an RSS or Atom feed that carries the whole post: Beehiiv,
Ghost, Buttondown, WordPress and the static-site generators, Medium's free
posts, most personal blogs. Also two publishers whose feeds carry whole
articles under Creative Commons licences that forbid editing — which is
house rule 1 in someone else's words:

- **ProPublica**, `https://www.propublica.org/feeds/propublica/main`:
  RSS 2.0 with the complete piece in `content:encoded` (verified; 15–40
  paragraphs an item), CC BY-NC-ND.
- **The Conversation**, `https://theconversation.com/us/articles.atom`
  (per-section feeds exist): Atom with the complete article in `content`
  and a `<rights>` line, CC BY-ND, 15–30 paragraphs an item.

Both publishers' republishing pages address other publishers and ask that
stories be picked one at a time rather than mirrored wholesale. One
household's single printed copy is reading, not republishing, but the
terms are worth knowing before anyone adds a "print everything" switch.

**What it needs.** Almost nothing: `substack.feed_url` already accepts a
full feed URL, and the adapter is `substack.py` without the paid branch
and with a generic chrome list (share bars, "related" blocks, `<figure>`,
`<iframe>`). The one new piece is detecting a summary-only feed: no
`content:encoded`, or a short body ending in a "read more" link. Such a
feed yields nothing, with a log line and a "this feed carries only
previews" verdict from Check — unless the publication has a `selector`,
in which case the page extractor (source 6) fetches the post's own page.

**Effort.** Small. Fixtures: a Beehiiv feed, a Ghost feed, a WordPress
summary-only feed, the ProPublica and Conversation feeds.

### 2. Email newsletters (`email`)

The mailbox as a source. A publication is an IMAP folder (a Gmail label)
or a sender address; every message in it inside the window that has not
printed is a queued article. This is probably the largest single win:

- The reader already curates it, by subscribing.
- Paid posts arrive whole. Money Stuff, Axios, The Browser, Morning Brew,
  every Beehiiv and Ghost letter with no public feed: all of it comes in
  by email and nowhere else.
- The credential already exists (`IMAP_*`), and the extractor for email
  HTML exists (`extract_email_paragraphs`).
- It makes the paid-Substack dance (RSS preview, then IMAP by subject)
  unnecessary: a paid Substack is just an email publication.

**Mapping.** `guid` = `Message-ID`; `title` = Subject; `author` = the From
display name; `publication` = the reader's name for the row (or the From
name); `published` = the Date header; `url` = the "View in browser" link
when the message has one (Substack, Beehiiv, Ghost, Buttondown and
Mailchimp all put one in), else empty with `rest: "inbox"`.

**Risks.** Email HTML is table soup. `BLOCK_TAGS` walks `p`, `h2`–`h4`,
`blockquote` and `li`; text that sits bare in a `<td>` or `<div>` is not
picked up, and the platforms differ. Per-platform chrome selectors are
needed (Beehiiv, Mailchimp and Ghost footers on top of the Substack ones in
`EMAIL_CHROME_SELECTORS`), and the Check button must show the paragraph
count with the first and last paragraph so a gutted issue is visible before
it prints. The IMAP `\Seen` flag is never touched (the mailbox is opened
read-only); `state.json` remembers what printed, as for everything else.

**Effort.** Medium. Fixtures: one `.eml` per platform.

### 3. A serial (`serial`)

A public-domain novel, printed a little every morning, the way Dickens
shipped. This is the most newspaper-native idea in the list, and it is
the source that guarantees the paper is never one sheet with a puzzle on
the front: there is always a next installment.

- **Standard Ebooks** first: `https://standardebooks.org/ebooks/<author>/<title>/text/single-page`
  is one XHTML document, chapters as `<section epub:type="chapter">` with
  an `<h2>`, paragraphs as plain `<p>`, blockquotes where the author had
  them, CC0 (verified on *Persuasion*). The existing block walk reads it
  with no new extractor; chapter headings come through as paragraphs the
  way Substack `h2`s do.
- **Project Gutenberg** for books Standard Ebooks lacks:
  `https://www.gutenberg.org/ebooks/<id>.txt.utf-8`, paragraphs at blank
  lines, the licence boilerplate cut at the `*** START OF` / `*** END OF`
  markers. Messier (hard-wrapped lines to rejoin, headings by heuristic),
  so second.

**How it queues.** The book is fetched once and cached as a paragraph list
under `<DATA_DIR>/serials/<slug>.json`. `state.json["serials"][slug]`
holds `{"next": <paragraph index>}`. Each morning the adapter offers one
article: the next `paragraphs_per_day` paragraphs (a per-row setting,
say 30), or fewer to end at a chapter boundary; `title` the book's title,
`publication` the author, `deck` the chapter heading when the installment
opens one, `url` the book's page, `rest: "tomorrow"`. The row's
`per_issue` is 1 and it belongs at the bottom of the priority list, so it
is the story that gets cut when the sheet is full. `record_printed`
advances `next` by exactly the paragraphs that reached the sheet — all of
them for a whole installment, `RenderResult.partial`'s count for a cut
one, none if it did not fit at all. Paragraphs are never split, nothing is
reworded, and tomorrow resumes at the paragraph after the last one
printed: rule 1 as the feature.

**What it changes.** The `rest` field and the wording in `onlineLine()`.

**Effort.** Small to medium. Fixtures: a short Standard Ebooks single-page
document, a Gutenberg text.

### 4. Wikipedia (`wikipedia`)

Zero credentials, always available, and the article is long enough that
it is always the partial story at the end of the sheet, which suits a
reference article: the reader gets the lead and the first sections, and
the address of the rest.

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

**Modes** (a per-row setting): today's featured article; a random featured
article; the article behind one of today's "On this day" events. `guid` =
page id plus revision; `url` the page; `published` today; `author` empty;
`publication` "Wikipedia". The heading marks (`==`) are markup and are
stripped, leaving the heading text as its own paragraph. The apparatus
sections at the end — References, See also, External links, Notes,
Bibliography — are cut by name: they are navigation, the same call as
removing a subscribe button. CC BY-SA.

**Effort.** Small. Fixtures: one featured-feed JSON, one extract JSON.

### 5. Real news: The Guardian (`guardian`), and what does not work

Most news is off the table. The New York Times APIs return abstracts and
URLs. AP and Reuters license their text; Reuters has no public feed since
2020. Almost every newspaper's RSS is a headline and a sentence, and the
page behind it is paywalled, React-rendered, or both.

**The Guardian Open Platform** is the exception: a free developer key for
non-commercial use, and `https://content.guardianapis.com/search` with
`show-fields=body,byline,standfirst` returns the article's whole body as
HTML, the deck as `standfirst`, the byline, `webUrl` and a stable `id`.
One call per section per morning, `section=` or `tag=` to choose,
`order=newest_first`, `max_age_days=1`, `per_issue=2` so world news does
not crowd out the newsletters. Env `GUARDIAN_API_KEY`. The documentation
pages could not be fetched from this container (the proxy refuses the
host), so the key's daily call limit and the exact terms are to be checked
at implementation; a morning needs a handful of calls, far below any tier.

**The NYT with the reader's cookie** is the same line the crossword
already crosses, but worse: the article body is scraped out of a page that
changes without notice, so it breaks silently and often. Only if the
reader asks, and last.

**Effort** (Guardian). Small: one JSON fixture, the shared extractor on
`fields.body`.

### 6. The reader's own reading list (`reading_list`)

The link saved during the day is the story on the doorstep next morning.
Two flavours, and they share the page extractor described under Risks.

**(a) The share sheet, no third party.** `POST /articles` with a URL, the
same bearer rule as `POST /lists/<slug>`; an iPhone Shortcut on the share
sheet ("Get URLs from input", then the post). Saved links wait in
`<DATA_DIR>/reading/`, one JSON each; the Sources tab lists them with
their extracted paragraph count and a Remove. The container fetches the
page and extracts the article at run time (or at save time, so the queue
view can show the count).

**(b) A reading service.** The reader saves from a logged-in browser and
the service already has the text, paywall and all, which is the strongest
argument for this flavour.

- **Readwise Reader** (verified): `GET https://readwise.io/api/v3/list/`
  with `Authorization: Token <READWISE_TOKEN>`, filtered by `location`
  (`later`, `shortlist`) or up to five `tag=`; `withHtmlContent=true`
  returns `html_content`; 20 requests a minute. After printing, the
  optional `printed` hook moves the item to `archive`.
- **Wallabag**: self-hosted, so it fits the Unraid box; OAuth2 client
  credentials; `GET /api/entries` returns each entry's extracted
  `content` HTML (verified); archive via the entries endpoint.
- **Instapaper**: the full API is xAuth and has a full-text endpoint, but
  the docs page is script-rendered and could not be read here; verify
  before promising it.
- **Pocket** shut down on 8 July 2025 and **Omnivore** in 2024; neither is
  an option.

**Risks.** Flavour (a) needs a page extractor, and a heuristic one
(`trafilatura` or `readability-lxml`, pinned in `requirements.txt`) can
drop a short paragraph it scores as boilerplate. That is a rule 1
problem. Mitigations, in order: a per-row `selector` (every block inside
that container, minus a chrome list, deterministic, the Substack way);
`trafilatura` with `favor_recall=True` only when there is no selector;
Check and the queue view always showing the paragraph count with the first
and last paragraph. A paywalled page extracts to a stub: very short text
plus subscribe or sign-in words is skipped with a log line and never
printed. Flavour (b) has none of this, because the service extracted the
text while the reader was logged in.

**Effort.** Medium for (a), small for (b) once (a)'s extractor exists (or
small alone, since a service hands over HTML the shared extractor can
already walk).

### 7. A folder (`folder`)

`<DATA_DIR>/inbox/*.md` and `*.txt`: anything dropped there prints as an
article and is moved to `inbox/printed/` after a delivered run. A first
`# ` line is the title, the file's mtime the date, blank lines split
paragraphs, Markdown inline marks flatten to text (as inline HTML does in
v1). No network, no account, and it turns an Unraid share, a Syncthing
folder or an Obsidian export into a source: the reader's own drafts read
on paper, a letter someone typed, a journal entry from a year ago today
that a two-line script drops in the night before.

**Effort.** Tiny. Could go in at any point after the foundation.

### 8. Later: verse, and a few oddities

- **Poetry** — PoetryDB (public domain, JSON with a `lines[]` array),
  Poetry Foundation's and Poets.org's poem-of-the-day feeds. Line breaks
  are the text of a poem, and `_clean` collapses whitespace, so verse
  needs a contract extension (a `verse` flag; stanzas as paragraphs with
  newlines kept; `white-space: pre-line`; the fitting script forbidden to
  break inside a stanza). Not before the front page has prose.
- **Chronicling America** (Library of Congress): the OCR text of a
  newspaper from this day a hundred years ago. Fun, free, and the OCR is
  frequently garbage; a stretch.
- **Wikinews**: CC BY, low volume, uneven.

### Not worth investigating again

| Source | Why not |
|---|---|
| NYT APIs | Abstracts and links only. |
| AP, Reuters | Licensees only; no public full text. |
| Apple News | No API at all. |
| Medium members-only posts | Truncated in the feed; free posts work as a `feed`. |
| Reddit | API terms and OAuth; self-posts are the only full text. |
| X / Twitter | No. |
| Hacker News, Lobsters | Links only: everything depends on the page extractor and hits paywalls; comments are not articles. |
| Kindle / Readwise highlights | Excerpts by definition, so rail material, never the front page. |
| Pocket, Omnivore | Shut down. |

## Recommended order

1. **The foundation**, with Substack as the only adapter and no behaviour
   change: `articles.py`, `sources/substack.py`, `extract.py`, `seen.py`,
   `sources.publications` with the migration, the `rest` field with its
   default, the Kind column on the Sources tab. Every existing test still
   passes; the sample PNGs are byte-identical.
2. **`feed`** — nearly free after step 1, and it opens Beehiiv, Ghost,
   Buttondown, blogs, ProPublica and The Conversation.
3. **`email`** — the reader's real subscriptions, whole, on one credential
   that already exists.
4. **`serial` and `wikipedia`** — the pair that means there is always
   something to print; the first users of `rest`.
5. **`guardian`** — news on the front page, legitimately.
6. **`reading_list`** — the share sheet, then Readwise; the page
   extractor is the one new dependency in the whole plan.
7. **`folder`** whenever it is convenient; **verse** and the rest later.

## Tests and fixtures

One fixture per adapter under `tests/fixtures/`, and for each: paragraphs
verbatim against the fixture (the `tests/verbatim.py` check), chrome
gone, the queue order the kind promises, seen guids skipped, the window
applied, a broken source leaving the others intact, `fetch` writing no
state. For the serial: the bookmark advances by exactly the paragraphs
printed, including zero. For the merged queue: priority order across
kinds, `per_issue` caps, `QUEUE_LIMIT`, and a run whose `record_printed`
reaches the right adapter. For the template: `rest` wording, and the
default render unchanged.

## Things to ask the owner before starting

- Which newsletters arrive only by email, and is the mailbox Gmail (the
  documented label-and-app-password route)?
- Which Guardian sections, if any, and whether news belongs above or below
  the newsletters in the priority list?
- Which book first, and how many paragraphs a morning?
- A reading app already in use (Readwise Reader, Wallabag, Instapaper), or
  the share sheet alone?
