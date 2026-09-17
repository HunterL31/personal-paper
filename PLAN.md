# Personal Paper — implementation plan

A personalized one-reader newspaper, generated and printed every morning on the
home Brother HL-L2460DW. This repo already contains the render step (template,
fonts, `render.py`, sample data). This plan covers everything around it.

## House rules (non-negotiable)

1. **Articles are printed verbatim.** Never summarize, paraphrase, shorten, or
   reword an article, its title, subtitle, or byline. The only permitted
   operation is what the template's fitting script already does: break the
   text after the last word that fits the front-page slot and continue the
   rest, unchanged, on an inside page.
2. **There are no LLM calls anywhere in this pipeline.** Nothing is generated.
   The paper is assembled from data (calendar, tasks, weather) and from
   articles as published.
3. **The paper prints every morning even when a source fails.** A gatherer
   that errors produces an empty section and a log line; it never blocks the
   run.
4. Traditional broadsheet style, black only, US Letter, duplex. The look is
   set in `template.html`; keep changes there deliberate.

## Architecture

```
                         ┌──────────────────────────── one FastAPI process ───────────────────────────┐
                         │                                                                            │
  browser on the LAN ──▶ │  web UI  (settings, preview, "print now", status)   ──▶ /data/settings.json │
  iPhone Shortcut ─────▶ │  POST /tasks                                        ──▶ /data/tasks.json    │
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
    tasks.py
    weather.py
    substack.py
    crossword.py        interface only in v1 (see below)
  deliver/
    __init__.py         deliver(pdf, settings) -> runs every enabled route
    printer.py          IPP print via pyipp; discover(); test()
    email.py            SMTP, PDF attached
  run.py                one issue: gather → render → deliver → archive (also the CLI)
  app/
    main.py             FastAPI app: web UI routes, POST /tasks, scheduler startup
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
- `tasks[]`: plain strings.
- `weather.hourly[]`: six entries at 7, 10, 13, 16, 19, 22 local time.
- `articles[]`: `title`, `deck` (nullable), `author`, `publication`,
  `published` (short, e.g. `"Sept. 15"`), `paragraphs` (list of plain-text
  strings, in the author's order, untouched). Up to four are placed on the
  front page: index 0 is the lead, 1–3 the second row.

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
| `TASKS_TOKEN` | Bearer token the iPhone Shortcut sends to `POST /tasks`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Outgoing mail for the email route (a Gmail app password works). |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Only if a paid Substack needs the email route. |
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

**Look.** `paper.name`, the two ear texts (initials, the right-ear lines),
imprint and price lines; masthead font, headline font, body font, each a
dropdown of the bundled OFL fonts (`render/fonts/`; ship a few more
choices: EB Garamond and Libre Baskerville for body, Playfair Display for
headlines, UnifrakturCook as a second blackletter); body text size
(8–11 pt in half-point steps, headlines scale with it); lead-story body
height; section toggles for To do, Hour by hour, Notes. The template
takes all of this as a `style` dict and the fitting script keeps
measuring, so a bigger body size simply means less fits on the front page
and more goes inside. Nothing here can change an article's words.

**Sources.** Calendar iCal URLs (add/remove, masked after save, "Check"
button fetches and shows the calendar name and today's event count);
Substack publications, ordered, each with a "paid" checkbox (order =
front-page priority); weather lat/lon with a "Check" that shows the
current forecast; tasks: shows the age of the last sync and the exact
Shortcut URL to configure.

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
images: a layout that pushes to five pages is the thing to catch here.

**Status** (header strip on every tab): last run time and result, last
error, issue number, tail of the log, link to the last PDF.

## Output routes

`deliver/deliver(pdf, settings)` runs each enabled route in its own
try/except and returns a per-route result. A run is a **success** if the
render succeeded and at least one enabled route succeeded; every failed
route is logged and shown on the Status strip and in the failure
notification. The archive copy is written before any route runs.

- **Print** (`deliver/printer.py`): direct IPP with `pyipp`, no CUPS.
  `print(pdf, ip, duplex)` sends `application/pdf` with
  `sides=two-sided-long-edge`, `media=na_letter`. `test(ip)` returns
  model, state and `document-format-supported`. `discover()` browses
  `_ipp._tcp.local.` with `zeroconf` for a few seconds and returns
  (name, ip, model) tuples. mDNS only works if the container sees the LAN
  broadcast domain: run it with `network_mode: host` (the Unraid template
  default for this) or on a macvlan; on plain bridge networking discovery
  returns nothing and the manual IP field is the way in. The Brother
  HL-L2460DW is AirPrint-capable so it accepts PDF over IPP; keep the
  CUPS route from the earlier draft as a documented fallback only.
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
  front-page priority when several have new posts.
- RSS at `https://<name>.substack.com/feed` via `feedparser`. New = published
  since the last run; keep seen post GUIDs in `state.json` so nothing is
  repeated or missed if a run is skipped.
- Fields: `title` ← item title; `deck` ← item `description`/subtitle when it
  is a real subtitle (not a truncated body); `author` ← `dc:creator`;
  `publication` ← feed title; `published` ← formatted pubDate.
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
- If more than four posts arrive, v1 prints the top four and logs the rest.

### tasks.py

Depends on which app "Smart Tasks" is — **ask before building this module.**

- **If it is the iOS/macOS app "Smart Tasks: Lists Made Easy":** it has no
  public API and no account, so the phone has to push. The app exposes
  `POST /tasks` (bearer token from env `TASKS_TOKEN`) accepting
  `{"tasks": ["..."]}` and writing `/data/tasks.json` with a timestamp. An
  iPhone Shortcut personal automation at 05:50 collects today's tasks (only
  possible if the app exposes Shortcuts actions — check the Shortcuts app;
  if it doesn't, Apple Reminders or Todoist via the same endpoint is the
  fallback) and posts them to `http://<unraid-ip>:8080/tasks`. `tasks.py`
  reads the file; if it is older than 24 h, return an empty list and log
  "tasks not synced".
- **If it is SmartTask.co:** plain REST client with an API token.

### crossword.py

v1 ships the interface only: `fetch(settings) -> Path | None`, always `None`.
The NYT puzzle has no API and its print PDF sits behind her login; automating
that is fragile and against their terms, so it is a decision for the owner,
not for this implementation. If a PDF is returned it is appended as the back
page(s) via `render.py --crossword`. Any failure here returns `None`; the
paper never waits on it.

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
   reports the page count.
5. Copy the PDF to `/data/archive/<date>.pdf`.
6. `deliver()` over the enabled routes (skipped on `dry_run`).
7. On success: bump the issue counter and write `last_success` to
   `state.json`. On failure: write `last_error`, and send the failure
   notification.

CLI: `python run.py [--dry-run] [--date YYYY-MM-DD]`; `--date` renders a
past day from its archived `data.json` (useful for debugging layout).
Exit non-zero on failure.

## Printing

The template is measured and printed by headless Chromium, so the container
needs Playwright's Chromium. Base the image on the official Playwright Python
image (pin the current tag). Printing itself is the IPP route described
under Output routes. The web page's "Test" button is the check that the
chosen printer lists `application/pdf`; if some other printer does not,
the fallback is CUPS in the container (`cupsd` in the entrypoint,
`lpadmin -p <name> -E -v ipp://<ip>/ipp/print -m everywhere`, then `lp`),
which negotiates the format. Not built unless needed.

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
  printer route against a fake IPP responder (or `pyipp`'s test fixtures);
  one failed route does not block the other.
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
5. **Tasks.** Endpoint + Shortcut (or the API client, depending on the app).
6. **Look tab.** Fonts, sizes, ear text, section toggles, with the
   `test_look.py` guard. Also removes the hardcoded "M. L." and "The
   crossword is on the back page." from the template: both become ear
   settings, and the crossword line defaults to off.
7. **Operations.** Issue counter, failure notification, `--date` replay,
   Status strip, tests green.
8. **Later** (not v1): inline italics/links preserved through the extractor
   (paragraphs become restricted HTML, template stops escaping); extra posts
   beyond four poured onto inside pages in full; the crossword decision;
   custom font upload.

## Things to ask the owner before starting the relevant milestone

- Which "Smart Tasks" app, and does it expose Shortcuts actions?
- The list of Substack publications, in priority order, and which are paid.
- One calendar or several? The secret iCal URL(s).
- Host networking on the Unraid box (for printer discovery), or bridge
  with a manual printer IP?
- Which routes on day one: print only, or print and email?
- Wake-up time — when should the paper be sitting in the tray? (Changeable
  later from the Output tab.)
- The crossword: manual for now, or revisit later.

## Notes on the existing render step

- `render.py` renders the Jinja2 template, opens it in Chromium under
  **print media** (so measurement matches the PDF), waits for
  `window.__layoutDone`, and prints with `prefer_css_page_size`.
- The fitting script in `template.html` truncates by binary search on word
  count against the slot's real box; inside pages are generated from
  `<template id="inside-page">`. `window.__pages` reports the count.
- Fonts are local files under `render/fonts/` (all SIL Open Font License) and
  are referenced by relative path; keep them beside the output or pass
  `font_dir`.
- Don't add a Google Fonts link or any network dependency to the template;
  the render must work with the container's network off.
