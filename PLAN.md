# The Molly Ledger — implementation plan

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
 06:00 cron ─▶ main.py ─▶ gather/* ─▶ data.json ─▶ render.py ─▶ paper.pdf ─▶ lp ─▶ printer
                  │                                                  │
                  └── /data/state.json (issue counter, seen posts)   └── /data/archive/YYYY-MM-DD.pdf

 always-on ─▶ api.py  (POST /tasks from an iPhone Shortcut → /data/tasks.json)
```

One long-running Docker container on the Unraid box, running two processes:
a cron daemon (supercronic) for the daily run and a small HTTP API for the
task sync. Everything persistent lives under a mounted `/data` volume.

## Repo layout

```
molly-ledger/
  render/               existing render step — do not restructure
    template.html
    render.py
    sample_data.json
    fonts/
  gather/
    __init__.py         run_all(config) -> dict matching sample_data.json
    calendar.py
    tasks.py
    weather.py
    substack.py
    crossword.py        interface only in v1 (see below)
  main.py               orchestrator: gather → render → print → archive
  api.py                FastAPI app, POST /tasks
  config.yaml           non-secret settings
  .env.example          secrets template
  Dockerfile
  docker-compose.yml
  entrypoint.sh         starts supercronic + uvicorn
  crontab
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

## Gatherers

Each module exposes `fetch(config) -> <its part of the contract>` and a
`__main__` that prints its JSON, so each can be tested alone:
`python -m gather.weather`.

### calendar.py

- Source: one or more Google Calendar **secret iCal addresses** (Calendar
  settings → the calendar → "Secret address in iCal format"). Env
  `CALENDAR_ICS_URLS`, comma-separated. Treat as secrets.
- Libraries: `requests`, `icalendar`, `recurring-ical-events` (expands
  recurrences — mandatory, or repeating events vanish).
- Window: today 00:00–24:00 in `config.timezone` (`America/Los_Angeles`).
  Do the date math in that zone; off-by-one-day bugs here are the classic
  failure.
- All-day events first, then by start time. Use `SUMMARY` for `title`, first
  line of `LOCATION` for `where`. Skip events where her attendee status is
  `DECLINED`. Cancelled (`STATUS:CANCELLED`) skipped.

### weather.py

- Source: Open-Meteo forecast API (free, no key). `config.lat/lon`,
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

- Config: `config.substacks`, an ordered list of publications; order is
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
  public API and no account, so the phone has to push. `api.py` exposes
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

v1 ships the interface only: `fetch(config) -> Path | None`, always `None`.
The NYT puzzle has no API and its print PDF sits behind her login; automating
that is fragile and against their terms, so it is a decision for the owner,
not for this implementation. If a PDF is returned it is appended as the back
page(s) via `render.py --crossword`. Any failure here returns `None`; the
paper never waits on it.

## main.py

1. Load `config.yaml` + env. Set `TZ`.
2. `gather.run_all()`: call each gatherer inside its own try/except with a
   30 s timeout; on failure log the traceback and substitute the empty value
   (`[]`, or a weather dict with `summary: "Forecast unavailable"`).
3. Write `/data/out/<date>/data.json`.
4. `render/render.py` with that data (import it or subprocess it). It writes
   `paper.pdf` and reports the page count.
5. Print: `lp -d <queue> -o media=Letter -o sides=two-sided-long-edge`.
6. On success: bump the issue counter, copy the PDF to
   `/data/archive/<date>.pdf`, write `last_success` to `state.json`.
7. Exit non-zero on any failure after gathering (render/print), so the
   scheduler can alert.

A `--dry-run` flag does everything except `lp`; `--date YYYY-MM-DD` renders a
past day from its archived `data.json` (useful for debugging layout).

## Printing

The template is measured and printed by headless Chromium, so the container
needs Playwright's Chromium. Base the image on the official Playwright Python
image (pin the current tag). Two ways to reach the printer; verify which the
printer supports before choosing:

```
apt install cups-ipp-utils
ipptool -tv ipp://<printer-ip>/ipp/print get-printer-attributes.test | grep document-format-supported
```

- If `application/pdf` is listed: print directly over IPP with `ipptool`
  (`print-job.test` with `-f paper.pdf`) or the `pyipp` library. No CUPS.
- Otherwise: install `cups` in the container, start `cupsd` in
  `entrypoint.sh`, and add the queue driverless at first start:
  `lpadmin -p brother -E -v ipp://<printer-ip>/ipp/print -m everywhere`.
  Then `lp` as above. CUPS negotiates the format.

Either way the printer IP is `config.printer.ip` and duplex long-edge is set
per job.

## Scheduling and deployment

- `crontab`: `0 6 * * * cd /app && python main.py >> /data/logs/run.log 2>&1`
  run by supercronic (respects `TZ`). Time is `config.print_time`; document
  that changing it means editing the crontab.
- `entrypoint.sh` starts `uvicorn api:app --host 0.0.0.0 --port 8080` in the
  background, then supercronic in the foreground.
- `docker-compose.yml`: bridge network, port `8080:8080`, volume
  `./data:/data`, `env_file: .env`, `restart: unless-stopped`. Provide the
  same as an Unraid template comment block (image, port, path, variables) so
  it can be added from the Unraid Docker tab.
- Failure notification: if `main.py` exits non-zero, post to Unraid's
  notification endpoint or send an email (config choice). A missing paper at
  6:10 should be visible somewhere other than the log.

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
- Each gatherer: a unit test on a saved fixture (an `.ics` file, an
  Open-Meteo JSON response, a Substack feed XML) so tests run offline.
- One manual acceptance step per milestone: print the real thing and look at
  it in daylight.

## Milestones

1. **Print anything.** Dockerfile, `main.py` with sample data only, printing
   works end to end on the Unraid box. Confirm duplex and margins on paper.
2. **Weather + calendar.** Real data in the ears and rail.
3. **Substack via RSS.** Front page with real posts; then IMAP if any
   subscription is paid.
4. **Tasks.** Endpoint + Shortcut (or the API client, depending on the app).
5. **Operations.** Issue counter, archive, failure notification, `--date`
   replay, tests green.
6. **Later** (not v1): inline italics/links preserved through the extractor
   (paragraphs become restricted HTML, template stops escaping); extra posts
   beyond four poured onto inside pages in full; the crossword decision.

## Things to ask the owner before starting the relevant milestone

- Which "Smart Tasks" app, and does it expose Shortcuts actions?
- The list of Substack publications, in priority order, and which are paid.
- One calendar or several? The secret iCal URL(s).
- The printer's IP (or hostname) on the LAN.
- Wake-up time — when should the paper be sitting in the tray?
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
