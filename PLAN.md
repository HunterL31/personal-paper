# Personal Paper — implementation plan

A personalized one-reader newspaper, generated and printed every morning on the
home Brother HL-L2460DW. This repo already contains the render step (template,
fonts, `render.py`, sample data). This plan covers everything around it.

## House rules (non-negotiable)

1. **Articles are printed verbatim.** Never summarize, paraphrase, shorten, or
   reword an article, its title, subtitle, or byline. The only permitted
   operation is what the template's fitting script already does: break the
   text after the last word that fits the front-page slot and continue the
   rest, unchanged, on page 2.
2. **There are no LLM calls anywhere in this pipeline.** Nothing is generated.
   The paper is assembled from data (calendar, lists, weather) and from
   articles as published.
3. **The paper prints every morning even when a source fails.** A gatherer
   that errors produces an empty section and a log line; it never blocks the
   run.
4. Traditional broadsheet style, black only, US Letter, duplex. The look is
   set in `template.html`; keep changes there deliberate.
5. **The paper is one double-sided sheet: exactly two pages, printed
   one-sided on a morning with no articles.**
   Page 1 is the front page; page 2 carries the continuations and, when
   enabled, the crossword. With nothing in the queue there is nothing to
   continue, so there is no page 2: the paper is the front page alone, with
   the crossword set where the lead would have been (and "No new stories
   this morning." when there is no puzzle either), and it goes to the
   printer one-sided. The articles arrive as a queue (see
   substack.py) and the layout fills the sheet from the top of it: whole
   articles first, and when the next one will not fit whole, as many of
   its leading complete paragraphs as fit, closed with a line pointing to
   the rest online. A partially printed article counts as used. Articles
   that got no room are not marked seen, so they print on a later morning.
   Paragraphs are never split except at the front-page jump to page 2, and
   nothing is ever reworded. Hyphenation at line ends (soft hyphens
   inserted at render time, never into the stored text) is typesetting,
   not editing.

## Architecture

```
                         ┌──────────────────────────── one FastAPI process ───────────────────────────┐
                         │                                                                            │
  browser on the LAN ──▶ │  web UI  (settings, preview, "print now", status)   ──▶ /data/settings.json │
  iPhone Shortcut ─────▶ │  POST /lists/<slug>                              ──▶ /data/lists/<slug>.json │
                         │  scheduler (APScheduler, reads print_time/days from settings)               │
                         │      └─▶ run()  ─▶ gather/* ─▶ data.json ─▶ render ─▶ paper.pdf ─▶ deliver │
                         │                                                          ├─▶ printer (IPP) │
                         │                                                          ├─▶ email (SMTP)  │
                         │                                                          └─▶ /data/archive │
                         └────────────────────────────────────────────────────────────────────────────┘
```

One long-running Docker container on the Unraid box, running one process:
a FastAPI app that serves the settings web page, the task-sync endpoint,
and an in-process scheduler for the daily run. Everything persistent lives
under a mounted `/data` volume. (An earlier draft used supercronic and a
static crontab; that is gone because the print time is now set in the web
page and must take effect without a restart.)

## Repo layout

```
personal-paper/
  render/               existing render step — do not restructure
    template.html
    render.py
    sample_data.json
    fonts/
  gather/
    __init__.py         run_all(settings) -> dict matching sample_data.json
    calendar.py
    lists.py            the reader's named lists, pushed from the phone
    weather.py
    substack.py
    crossword.py        the day's NYT puzzle as data (see below)
  deliver/
    __init__.py         deliver(pdf, settings) -> runs every enabled route
    printer.py          IPP print via pyipp; pick_format(); discover(); test()
    pwg.py              PWG Raster encoder, for printers that refuse PDF
    email.py            SMTP, PDF attached
  run.py                one issue: gather → render → deliver → archive (also the CLI)
  app/
    main.py             FastAPI app: web UI routes, POST /lists/<slug>, scheduler startup
    settings.py         pydantic model + load/save of /data/settings.json + defaults
    scheduler.py        APScheduler job wired to settings.schedule
    templates/          Jinja2 pages for the UI (plain HTML, no build step)
    static/
  settings.example.json what /data/settings.json looks like
  .env.example          secrets template (container variables)
  Dockerfile
  docker-compose.yml
  unraid-template.xml   so it can be added from the Unraid Docker tab
  tests/
  PLAN.md
```

## Data contract

`gather.run_all()` must return exactly the shape of `render/sample_data.json`.
Read that file first; it is the contract. Notes per field:

- `paper.volume`: `"Vol. I, No. {n}"`, where `n` is an issue counter in
  `state.json` that increments on each successful print.
- `paper.date`: `"Wednesday, September 16, 2026"` (full weekday and month).
- `events[]`: `time` is a display string — `"All day"` or a bare clock time
  like `"9:30"` / `"3:00"` (order makes a.m./p.m. obvious; the rail column is
  narrow). `where` is optional.
- `lists[]`: one entry per list configured on the Sources tab, in that
  order: `{"name", "slug", "style", "items"}`, `style` one of `checkbox`,
  `plain`, `numbered`, `items` plain strings — `[]` on a morning the phone
  did not sync that list. (It replaced `tasks[]`, a bare list of strings,
  when lists got names.) Where a list is set — rail, page 2 or off — is a
  look setting, `look.layout.sections`, never part of the data.
- `weather.hourly[]`: six entries at 7, 10, 13, 16, 19, 22 local time.
- `articles[]`: `title`, `deck` (nullable), `author`, `publication`,
  `published` (short, e.g. `"Sept. 15"`), `paragraphs` (list of plain-text
  strings, in the author's order, untouched), `url` (the post's own page,
  printed under a story that only partly fit). Up to four are placed on the
  front page: index 0 is the lead, 1–3 the second row.

- `crossword`: nullable. `null` on any morning without a puzzle (source off,
  a day it is switched off for, no cookie, a failed fetch); otherwise the
  object `gather/crossword.py` returns — `provider`, `date`, `title`,
  `author`, `editor`, `width`, `height`, `grid` (rows of `null` for a black
  square or `{"n": <clue number or null>}`), `across` and `down` (lists of
  `{"n", "clue"}`). **Never the answers.**

All text is plain; the template HTML-escapes it. Inline italics/links inside
articles are flattened in v1 (see "Later").

## Settings and secrets

Two stores, split by one rule: **credentials go in the container's
variables; everything the reader would change goes in the web page.**

### Container variables (`.env` / Unraid template variables)

Secrets and infrastructure. Set once, rarely change, and if the `/data`
volume is wiped nothing sensitive was in it.

| Variable | Purpose |
|---|---|
| `WEB_PASSWORD` | HTTP basic auth on the web page. Optional: unset means no login, and the page says so on every tab. |
| `TASKS_TOKEN` | Bearer token the iPhone Shortcut sends to `POST /lists/<slug>`. Optional: each list also accepts its own slug as the bearer token, and with no `TASKS_TOKEN` set a post with no header at all is accepted. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Outgoing mail for the email route (a Gmail app password works). |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Only if a paid Substack needs the email route. |
| `NYT_S` | The subscriber's `NYT-S` cookie from a logged-in nytimes.com browser, for the crossword. Needs a Games subscription; good for about a year. |
| `TZ` | `America/Los_Angeles`. |

Unraid's Docker tab already gives every variable a masked "password" field
and a place to document it, so the template does that job for free. The
web page shows each of these as **"set in container"** or **"not set"**,
read-only, with the variable name so it is obvious where to go.

### `/data/settings.json` (edited from the web page)

Everything else: look, sources, output routes, schedule. Written by the
web page, read at the start of every run, file mode `0600`. The Google
Calendar secret iCal URLs live here too: they are capability URLs rather
than credentials, they belong to the reader not the operator, and she
should be able to paste a new one without touching Docker. The UI masks
them after saving (shows the calendar name it fetched, not the URL).

Why not let the web page hold the real secrets? It would need its own
encryption at rest, a key that then has to live somewhere (back in the
container variables), and it puts the SMTP password one basic-auth
password away from anyone on the LAN. Not worth it for four values.

## The web page

Served by the same app on port 8080, behind `WEB_PASSWORD` when it is set. Plain
server-rendered HTML with a little JavaScript for the preview; no build
step. Four tabs.

### `look.layout` — where the furniture goes

The rail is no longer three fixed boxes with on/off switches. `Layout`
carries:

- `sections`: an ordered list of `{key, place}`. `key` is `agenda`,
  `hourly`, `notes`, or `list:<slug>` for one of `sources.lists`; `place`
  is `rail` (page 1's right column, filled top to bottom, in this order),
  `page2` (a column beside the continuations) or `off`. A key naming a
  list that no longer exists is ignored by the template, and the Sources
  tab drops it when the list goes; adding a list appends its section
  automatically, so a new list prints without a trip to the Look tab.
- `rail_width_in` (1.5–2.8), `front_stories` (1–4),
- `crossword_place` (`bottom`/`top`), `crossword_cell_in` (0.14–0.26),
  `crossword_max_pct` (25–75): how the puzzle sits on page 2.

Nothing here can change an article's words; it only changes how much fits,
which is the fitting script's problem as before.

**Look.** `paper.name`, the two ears (each a `kind` -- the weather, the
date, a monogram, the volume and number, lines of her own, the next thing
on today, sunrise and sunset, a countdown, the day's puzzle, or nothing at
all -- and the words that kind is set from), where the date is printed
(`date_place`: the folio rule, above or below the masthead, or only where
an ear shows it) and the face and size it is set in (`date_font`,
`date_size_pt`), imprint and price lines; masthead font, headline font, body font, each a
group of radio cards, every card set in the face it names with a line of
the text that face will set (the bundled OFL fonts, `render/fonts/`: a
dozen and a half families, listed once in `render/fontlist.py`, which both
`template.html` and the page's own `/fonts.css` are built from so the
sample on the screen is the face on the sheet); body text size
(8–11 pt in half-point steps, headlines scale with it); lead-story body
height; section toggles for To do, Hour by hour, Notes. The template
takes all of this as a `style` dict and the fitting script keeps
measuring, so a bigger body size simply means less fits on the front page
and more goes inside. Nothing here can change an article's words.

**Sources.** Calendar iCal URLs (add/remove, masked after save, "Check"
button fetches and shows the calendar name and today's event count);
Substack publications, ordered, each with a "paid" checkbox (order =
front-page priority); weather lat/lon with a "Check" that shows the
current forecast; the Lists table (name, slug, style, age limit, add and
remove) with a box per list showing the age of the last sync, how many
items arrived, and the exact URL and header the Shortcut needs.

**Output.** Multi-select of routes, each with its own fields, plus the
schedule:

- *Print* — printer chosen from a dropdown filled by discovery (see
  Printing below) or entered as an IP/hostname; "Test" queries the printer
  and shows its model name and whether it accepts PDF; "Print test page"
  sends the sample issue. Duplex on/off.
- *Email the PDF* — one or more addresses, subject template (`{date}`
  substitution). Sender comes from the container's SMTP variables; the
  page says which account will send and offers "Send test".
- *Archive only* — always on; the PDF goes to `/data/archive/` regardless.
- *Schedule* — time of day and days of week (default 06:00, every day).
  Saving reschedules the job in place. Also a **"Run now"** button (real
  data, all enabled routes) and **"Dry run"** (real data, archive only).

**Preview.** "Render example" renders `render/sample_data.json` with the
current Look settings and shows every page as an image, so a font or size
change can be judged before it hits paper; "Render today" does the same
with real data via a dry run. Rendering takes a few seconds in Chromium,
so the button starts a background job and the page polls
`/preview/<job>` until the PNGs are ready. Show the page count next to the
images and which stories made it onto the sheet: a morning where a story
was held over is the thing to notice here.

**Status** (header strip on every tab): last run time and result, last
error, issue number, tail of the log, link to the last PDF.

## Output routes

`deliver/deliver(pdf, settings)` runs each enabled route in its own
try/except and returns a per-route result. A run is a **success** if the
render succeeded and at least one enabled route succeeded; every failed
route is logged and shown on the Status strip and in the failure
notification. The archive copy is written before any route runs.

- **Print** (`deliver/printer.py`): direct IPP with `pyipp`, no CUPS.
  `print_pdf(pdf, host, duplex, pages)` asks the printer once what it takes
  (`pick_format`) and sends it with `sides=two-sided-long-edge`,
  `media=na_letter`, `print-color-mode=monochrome`, `copies=1`:
  - `application/pdf` when the printer lists it;
  - `image/pwg-raster` (PWG 5102.4) otherwise, encoded by `deliver/pwg.py`
    from the same PDF with pymupdf, at the finest resolution the printer
    lists up to 600 dpi and in the plainest page type it lists (`black_1`,
    else `sgray_8`). The Brother HL-L2460DW is AirPrint-capable but lists
    only `application/octet-stream`, `image/urf` and `image/pwg-raster` —
    no PDF — which is why the raster path exists. `image/urf` is not
    written yet, and a printer that offers nothing else fails the check
    with its list of formats in the message.

  `test(ip)` returns model, state and `document-format-supported`.
  `diagnose(ip)` is the Output tab's checklist: resolve, connect, ipp,
  format, state. `discover()` browses `_ipp._tcp.local.` with `zeroconf`
  for a few seconds and returns (name, ip, model) tuples. mDNS only works
  if the container sees the LAN broadcast domain: run it with
  `network_mode: host` (the Unraid template default for this) or on a
  macvlan; on plain bridge networking discovery returns nothing and the
  manual IP field is the way in.
- **Email** (`deliver/email.py`): stdlib `smtplib` + `email.message`,
  STARTTLS, PDF attached, filename `<paper name> YYYY-MM-DD.pdf` from
  `settings.look.paper_name`.

## Gatherers

Each module exposes `fetch(settings) -> <its part of the contract>` and a
`__main__` that prints its JSON, so each can be tested alone:
`python -m gather.weather`.

### calendar.py

- Source: one or more Google Calendar **secret iCal addresses** (Calendar
  settings → the calendar → "Secret address in iCal format"). Env
  `CALENDAR_ICS_URLS`, comma-separated. Treat as secrets.
- Libraries: `requests`, `icalendar`, `recurring-ical-events` (expands
  recurrences — mandatory, or repeating events vanish).
- Window: today 00:00–24:00 in `TZ` (`America/Los_Angeles`).
  Do the date math in that zone; off-by-one-day bugs here are the classic
  failure.
- All-day events first, then by start time. Use `SUMMARY` for `title`, first
  line of `LOCATION` for `where`. Skip events where her attendee status is
  `DECLINED`. Cancelled (`STATUS:CANCELLED`) skipped.

### weather.py

- Source: Open-Meteo forecast API (free, no key). `settings.sources.weather.lat/lon`,
  `temperature_unit=fahrenheit`, `wind_speed_unit=mph`,
  `timezone=America/Los_Angeles`. Daily: max/min temp, weather code, sunrise,
  sunset, dominant wind direction + max speed. Hourly: temp + weather code.
- Map WMO weather codes to short words for the `sky` column (Fog, Overcast,
  Sunny, Clear, Showers, Rain, …).
- `summary` is rule-based from the morning vs. afternoon codes, e.g.
  "Fog early, clearing by noon", "Sunny all day", "Rain through the
  afternoon". Keep it under ~30 characters — it lives in the ear box.
- Sunrise/sunset formatted `"6:53 a.m."`.

### substack.py

- Config: `settings.sources.substacks`, an ordered list of publications; order is
  front-page priority when several have posts waiting.
- RSS at `https://<name>.substack.com/feed` via `feedparser`. Seen post GUIDs
  are kept in `state.json` so nothing is repeated or missed if a run is
  skipped.
- **The queue.** Posts are a queue, not a news feed. Everything unprinted
  inside the window — `settings.sources.article_max_age_days`, set on the
  Sources tab, default 7 — waits its turn: publications in the order of the
  Sources tab, and within a publication the **oldest unread post first**, so
  nothing is skipped while newer posts jump ahead of it. A post that is still
  waiting when it falls out of the window is never printed; that is what keeps
  a first run from printing a year of archive. `fetch` returns up to
  `QUEUE_LIMIT` (8) candidates — more than the four the sheet holds, so the
  layout can fall through to the next one when a story does not fit — and logs
  how many are still waiting behind them.
- Fields: `title` ← item title; `deck` ← item `description`/subtitle when it
  is a real subtitle (not a truncated body); `author` ← `dc:creator`;
  `publication` ← feed title; `published` ← formatted pubDate; `url` ← the
  item link, tracking query stripped. `url` is part of the render contract:
  the template prints "The rest of this story is online: <url>" under a story
  that only partly fit. A partly printed post counts as used — run.py marks
  its guid seen — because the reader has its beginning on paper and the
  address of the rest.
- Paragraphs: parse `content:encoded` with BeautifulSoup and take the text of
  each block element (`p`, `h2`–`h4`, `blockquote`, `li`) in document order.
  **Removing Substack chrome is allowed; editing text is not.** Strip
  subscribe/share buttons, embed widgets, image captions and images
  (nothing to print). Keep everything the author wrote, including sign-offs.
- **Paywalled posts** arrive in RSS as a preview only. Detect the truncation
  (Substack's paywall marker / "read more" block) and, if any subscription is
  paid, use the email route instead: an IMAP mailbox (Gmail app password,
  env `IMAP_*`) with a label that her Substack emails land in; fetch the
  last 24 h, parse the HTML body with the same paragraph extractor. Build
  RSS first; add IMAP only if needed.
- More candidates than the sheet can hold is the normal case: whatever does
  not fit stays unseen and comes back to the front of the queue tomorrow.

### lists.py

The reader's own lists — to do, groceries, packing — configured on the
Sources tab as `sources.lists`: `{name, slug, style, max_age_hours}`. The
slug is derived from the name when the list is first saved (lowercase,
`[a-z0-9]+` joined by `-`, 40 characters) and then never changes: it is
half of the address the phone was set up with.

- There is no API to call, so the phone pushes. `POST /lists/<slug>`
  accepts `{"items": [...]}`, `{"tasks": [...]}` or plain text, one item
  per line, whatever the Content-Type says, and writes
  `<DATA_DIR>/lists/<slug>.json` as `{"items": [...], "updated": <ISO>}`.
  An iPhone Shortcut per list, run by a personal automation before print
  time, is what posts them. `POST /tasks` stays as an alias for the
  `tasks` list, so a Shortcut built before lists had names keeps working,
  and a `<DATA_DIR>/tasks.json` from that time is read as the `tasks` list
  until the first new sync replaces it.
- **The endpoint's auth rule**, in this order: `Authorization: Bearer
  <TASKS_TOKEN>` when the container has one; or `Bearer <slug>` — the
  list's own name as its token, which is no weaker than the URL it is
  already in and saves configuring a second secret on the phone; or, when
  `TASKS_TOKEN` is not set at all, no Authorization header. An unknown or
  malformed slug is a 404 naming the slugs this paper does have.
- `fetch(settings)` returns one entry per configured list, in the order of
  the Sources tab, with `items: []` and a "list <slug> not synced" log line
  when the file is missing, has no usable timestamp, or is older than that
  list's `max_age_hours`. A stale list prints empty; yesterday's list is
  never printed as if it were today's.

### crossword.py

The owner's decision, taken: fetch the day's New York Times puzzle with the
subscriber's own `NYT-S` session cookie, copied out of a logged-in browser
into the container variable `NYT_S`. No password is stored and no login is
scripted. This is automated access their terms discourage, for one
household's own copy of a puzzle it pays for; the owner accepts that.

- `fetch(settings, *, today=None) -> dict | None`, the `crossword` object of
  the data contract. `None` — with a log line, never an exception — when the
  source is off, when today is not one of `settings.sources.crossword.days`,
  when `NYT_S` is unset, or when the fetch fails.
- Endpoints, as module constants so a change is a one-line fix:
  `https://www.nytimes.com/svc/crosswords/v6/puzzle/daily/{YYYY-MM-DD}.json`
  with `Cookie: NYT-S=<cookie>` and a browser User-Agent, falling back to
  `.../v3/puzzle/daily-{YYYY-MM-DD}.json`. (Both verified against xword-dl's
  NYT downloader, which reads the same endpoint with the same cookie.)
- **The answers are dropped here.** The response carries the solution; only
  the black squares, the clue numbers and the clues go on, so no answer ever
  reaches `data.json` or the page. Clues are verbatim: entities unescaped,
  tags flattened, nothing else touched.
- The raw response is kept at `<DATA_DIR>/out/<date>/crossword.json` for
  debugging a bad grid.
- `check()` is the Sources tab's Check button: the same fetch with the
  failures raised, reporting title, constructor, date, size and clue count,
  and recording `crossword_check` in `state.json` for the status strip.
- The template typesets the puzzle; `run.py` only puts it in the data.

## run.py

`run(settings, *, dry_run=False, date=None) -> RunResult`, called by the
scheduler, by the web page's buttons, and by the CLI.

1. Load `/data/settings.json` + env.
2. `gather.run_all()`: call each gatherer inside its own try/except with a
   30 s timeout; on failure log the traceback and substitute the empty value
   (`[]`, or a weather dict with `summary: "Forecast unavailable"`).
3. Write `/data/out/<date>/data.json`.
4. Render with that data and `settings.look` (import `render.render`, which
   becomes a function; the CLI wrapper stays). It writes `paper.pdf` and
   reports the page count, `printed` (the articles that reached the sheet,
   whole or in part) and `partial` (article index → leading paragraphs
   printed). Every printed index's guid is marked seen, a partly printed one
   included; the rest are held for another morning and logged by title.
5. Copy the PDF to `/data/archive/<date>.pdf`, or `<date>-2.pdf`, `-3.pdf`
   when a paper was already made that day: an issue on file is never
   written over. `/data/out/<date>/` is the day's scratch and is reused.
6. `deliver()` over the enabled routes (skipped on `dry_run`).
7. On success: bump the issue counter and write `last_success`, `last_pdf`
   and `last_issue` (the number that sheet carries) to `state.json`. On
   failure: write `last_error`, and send the failure notification.

`run.reprint(settings)` is the other way out: it hands the newest archived
issue to the same routes again, with no gather, no render, no issue counted,
no post marked seen and no state written — only a log line. It is what the
Output tab's first button does.

CLI: `python run.py [--dry-run] [--date YYYY-MM-DD]`; `--date` renders a
past day from its archived `data.json` (useful for debugging layout).
Exit non-zero on failure.

## Printing

The template is measured and printed by headless Chromium, so the container
needs Playwright's Chromium. Base the image on the official Playwright Python
image (pin the current tag). Printing itself is the IPP route described
under Output routes. The web page's "Check the connection" button says which
format the chosen printer will be sent: PDF if it lists one, PWG Raster if
it does not. Nothing else is installed for printing — no CUPS in the
container, no drivers — because the format negotiation and the raster
encoder (`deliver/pwg.py`, pymupdf for the pixels) are in the app.

## Scheduling and deployment

- Scheduler: APScheduler `BackgroundScheduler` started with the app, one
  cron job built from `settings.schedule` (time + days, in `TZ`). Saving
  the Output tab calls `reschedule()`. Runs are serialized with a lock so
  "Run now" during the scheduled run waits rather than double-printing.
  `misfire_grace_time` of 30 minutes: if the container was restarting at
  06:00 the paper still prints at 06:10 rather than never.
- Process: `uvicorn app.main:app --host 0.0.0.0 --port 8080` is the
  container command. Logs to stdout (Docker keeps them) and to
  `/data/logs/run.log`.
- Enhanced logging (`settings.logs.enhanced`, the Log page's one switch):
  each run also writes `/data/logs/runs/<date>-<time>.log` at debug level,
  with the settings the run used, what every gatherer answered and how long
  it took, and what came of the routes. The newest `keep_runs` (7) files are
  kept and the Log page offers each for download; older ones are deleted as
  a run ends. The libraries that put credentials in their debug lines
  (urllib3 and friends) stay at INFO, so no secret is ever written to a file
  the page hands out.
- `docker-compose.yml`: `network_mode: host` (for printer discovery; port
  8080 must then be free on the box) or bridge with `8080:8080` and manual
  printer IP; volume `./data:/data`, `env_file: .env`,
  `restart: unless-stopped`. `unraid-template.xml` carries the same with
  every variable documented and secrets marked as password fields.
- Image publishing: `.github/workflows/docker.yml` builds the image on
  every push to `main` and on `v*` tags and pushes it to Docker Hub as
  `<DOCKERHUB_USERNAME>/personal-paper` with tags `latest`, the version, and
  the short SHA. Needs two repository secrets, `DOCKERHUB_USERNAME` and
  `DOCKERHUB_TOKEN` (a Docker Hub access token, not the account password).
  `docker-compose.yml` and `unraid-template.xml` reference that published
  image so the Unraid box never builds anything.
- Failure notification: if a run fails, post to Unraid's notification
  script (`/usr/local/emhttp/webGui/scripts/notify`, reachable by mounting
  it) or send an email to the same SMTP account; chosen on the Output
  tab. A missing paper at 6:10 should be visible somewhere other than the
  log.

## Tests

- `tests/test_render.py`: render `sample_data.json`, assert 2 pages, and
  **the verbatim check**: for each article, concatenate the text of all its
  `<p>` elements across all pages (excluding `.jump` and `.cont` lines, and
  joining `.tail` fragments back onto their paragraph) and assert it equals
  the original paragraphs joined. This is the test of house rule 1.
- `tests/test_layout_edges.py`: 0, 1, 2 and 5 articles; an article of 3,000
  words; a post with headings, blockquotes and lists; 0 and 15 calendar
  events; 12 long tasks. Each must render without error and without a page
  count explosion. Today the second row assumes three stories — make
  `.row` use `repeat(n, 1fr)` for n in 1–3, and hide the row when there is no
  second story.
- `tests/test_look.py`: render the sample issue at 8 pt and 11 pt body with
  each bundled font and assert the verbatim check still holds and the page
  count stays under five. This is what keeps the customization from ever
  breaking house rule 1.
- Each gatherer: a unit test on a saved fixture (an `.ics` file, an
  Open-Meteo JSON response, a Substack feed XML) so tests run offline.
- `tests/test_settings.py`: defaults load with an empty `/data`; a saved
  settings file round-trips; an unknown font name falls back to the
  default instead of failing the run.
- `tests/test_deliver.py`: email route against a local `aiosmtpd` server;
  printer route against a fake IPP responder (or `pyipp`'s test fixtures),
  whose Get-Printer-Attributes reply each test chooses, so both the PDF and
  the PWG Raster job are exercised; one failed route does not block the other.
- `tests/test_pwg.py`: the raster encoder, read back by its own decoder and
  by the spec's field offsets — page headers, the bitmap, identical-line
  compression, and the run-length encoding of section 4.4.
- `tests/test_lists.py`: each list fetched per its own age limit, a stale
  one empty, the pre-lists `tasks.json` still read, slugs derived, the
  60-item cap.
- `tests/test_web.py`: the UI asks for `WEB_PASSWORD` when set and is open when unset;
  saving the Output tab reschedules the job; "Render example" produces
  PNGs.
- One manual acceptance step per milestone: print the real thing and look at
  it in daylight.

## Milestones

1. **Print anything.** Dockerfile, `run.py` with sample data only, the
   IPP print route with a hardcoded IP, works end to end on the Unraid box.
   Confirm duplex and margins on paper.
2. **Web page: Output + Preview.** Settings model, basic auth, the Output
   tab (printer discovery/test, email route, schedule) and "Render
   example". From here on the paper is configured from the browser, not
   from files.
3. **Weather + calendar.** Real data in the ears and rail; the Sources
   tab for both.
4. **Substack via RSS.** Front page with real posts; Sources tab entries;
   then IMAP if any subscription is paid.
5. **Lists.** Endpoint + Shortcut, one list per Shortcut, and the Layout
   table that says where each one prints.
6. **Look tab.** Fonts, sizes, ear text, section toggles, with the
   `test_look.py` guard. Also removes the hardcoded "M. L." and "The
   crossword is on the back page." from the template: both become ear
   settings, and the crossword line defaults to off.
7. **Operations.** Issue counter, failure notification, `--date` replay,
   Status strip, tests green.
8. **Later** (not v1): inline italics/links preserved through the extractor
   (paragraphs become restricted HTML, template stops escaping); extra posts
   beyond four; custom font upload.
9. **More than Substack on the front page.** `docs/CONTENT-SOURCES.md` is
   the plan: one merged queue with an adapter per kind (any website or
   newsletter by address, Wikipedia's featured article, a public-domain
   book in installments, links saved from the phone, The Guardian), each
   one a thing the reader can switch on from the Sources tab without a
   credential, and the refactor they all need first.

## Things to ask the owner before starting the relevant milestone

- Which tasks app, and does it expose Shortcuts actions? (Answered by
  lists: whatever produces text, one item per line, can post to a list.)
- The list of Substack publications, in priority order, and which are paid.
- One calendar or several? The secret iCal URL(s).
- Host networking on the Unraid box (for printer discovery), or bridge
  with a manual printer IP?
- Which routes on day one: print only, or print and email?
- Wake-up time — when should the paper be sitting in the tray? (Changeable
  later from the Output tab.)

## Notes on the existing render step

- `render.py` renders the Jinja2 template, opens it in Chromium under
  **print media** (so measurement matches the PDF), waits for
  `window.__layoutDone`, and prints with `prefer_css_page_size`.
- The fitting script in `template.html` truncates by binary search on word
  count against the slot's real box; page 2 is a fixed element built from
  `<template id="inside-page">`. `window.__pages` reports the count.
- Fonts are local files under `render/fonts/` (all SIL Open Font License) and
  are referenced by relative path; keep them beside the output or pass
  `font_dir`.
- Don't add a Google Fonts link or any network dependency to the template;
  the render must work with the container's network off.
