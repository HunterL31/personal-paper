# Notes for whoever works on this next

Things learned building Personal Paper that are not obvious from the code
and cost time to rediscover. CLAUDE.md holds the rules; this holds the
reasons and the traps. Add to it when you hit one.

## Environment and tooling

- **Playwright is pinned to 1.56** because the dev container ships Chromium
  build 1194 under `/opt/pw-browsers`. A newer `pip install playwright`
  wants a different build and fails to launch. Never run
  `playwright install` here. `CHROMIUM_PATH` overrides the executable when
  needed (the Docker image finds its own).
- **`sgmllib3k`** (a feedparser dependency) does not build with current
  setuptools. Install it first with
  `SETUPTOOLS_USE_DISTUTILS=stdlib pip install sgmllib3k`; the CI workflow
  does exactly this.
- **Headless Chromium on Linux has no hyphenation dictionary**, so CSS
  `hyphens: auto` does nothing and justified narrow columns get wide word
  gaps. Hyphenation is done with `pyphen` at render time by inserting soft
  hyphens (U+00AD) into the HTML only. The stored article text never
  contains them; `tests/verbatim.py` strips them before comparing.
- **pymupdf does everything PDF-shaped**: page PNGs for previews and tests,
  page counting, rasterizing for the printer. There is no poppler in the
  image.
- The full suite takes about 2.5 to 3 minutes and needs `DATA_DIR` set;
  `tests/conftest.py` gives every test a private one. A render is about
  0.6 s, so keep render-heavy parametrizations modest.

## Testing traps

- **httpx's TestClient treats `data=[(k, v), ...]` as raw bytes**, not a
  form. For repeated keys use a dict with list values:
  `data={"cal_new": ["n0", "n1"], ...}`.
- **`aiosmtpd.Controller(port=0)` does not work** (it dials the literal
  port). Pick a free port first.
- **Repeated paragraphs in test articles make the verbatim checks
  ambiguous** (the same sentence is both printed and held back). Use
  `tests.verbatim.lengthen()`, which prefixes each paragraph with an
  ordinal.
- **Logging an exception object (or `exc_info=True`) inside
  `deliver.printer.page_count` kept the traceback, and through it the
  open socket, alive** until the handler released the record, which hung
  the fake IPP server's teardown. It logs a pre-formatted string on
  purpose.
- **Default output is locked by byte-identical PNG tests** for the default
  Look and Layout. Any template change must leave the default render
  unchanged; add new behaviour behind a setting whose default reproduces
  today.

## Rendering and the fitting script (`render/template.html`)

- The script measures with `getBoundingClientRect()` against the slot's
  real box under print media emulation, which is why `render.py` calls
  `page.emulate_media(media="print")` before measuring.
- `truncate()` mutates `textContent` while searching. Capture the original
  text before calling it; an early version lost a paragraph when the first
  word did not fit.
- Article selection is a search: try k = front_stories..1 whole articles;
  before dropping article k, try printing it partially (leading whole
  paragraphs plus the "rest is online" line). Result is N whole plus at
  most one partial, always the last.
- A fourth front-page story narrows the row columns and pushes more of
  stories 2 and 3 onto page 2, so adding a partial fourth story can fail
  even when it looks like there is room. That is why the sample prints
  three of four at 9 pt.
- A lone story on the front (no second row) gets the `solo` class and its
  body grows to the foot of the page; otherwise the fixed lead slot leaves
  page 1 half blank.
- `window.__pages` is 2 with articles and 1 with none (the crossword moves
  to the front); `render()` asserts exactly that pairing.
- Chromium's `break-inside: avoid` on a wrapper taller than a column pushes
  the wrapper to a new column and overflows. The online-line wrapper
  measures with `avoid` first and relaxes when it does not fit.
- Plain `p.online` lost to the later `.cols p` rule on page 2; selectors
  for page-2 overrides need `.page` or higher specificity.
- Ears are 1.3 in by 1.12 in inside a fixed-height nameplate; an over-full
  ear grows out of the plate rather than overflowing its box, so
  `fitEars()` measures the box against the plate and scales fonts in 5%
  steps.
- All fonts are declared from one table, `render/fontlist.py`, used by both
  the template and the web page's `/fonts.css`. Variable fonts are renamed
  `Name-var.ttf` because `[wght]` in a filename breaks URLs.

### The rail (`layoutRail` and friends)

- **The rail is fitted before the stories, and that order is load-bearing.**
  A section that overruns page 1 continues into a column on page 2, which is
  made on demand by wrapping `.cols` in `.page2-main`. That narrows the
  continuations, so the article search has to run after it and not before.
  `onePage()` lays the rail out again with `layoutRail(false)`, before
  `fitFrontCrossword`, because the column decides how wide the story slot is.
- Every pass rebuilds the rail from clones taken at script start (`RAIL_P1`,
  `RAIL_P2`), the way the article search rebuilds page 1 from `pristine`.
  Measuring a section while the ones below it are still in the column gives
  the wrong answer: flex shrinks them.
- Sections break between `li` and `tr`, so lists, the agenda and the hourly
  table all run on; the notes box has no rows and moves whole or not at all.
  `railFoot()` is what keeps the hourly table's sunrise line with the part
  that carries its last row.
- When nothing overflows, the rebuild is a no-op down to the pixel — which
  is what keeps the default render byte-identical. Check it that way
  (`md5sum` of the sample PNGs) after touching any of this.
- `RenderResult.rail_dropped` is the one thing the sheet cannot show the
  reader an "online" line for, so it is logged at WARNING. `railTally()`
  counts from the finished page, not from the passes, so the report cannot
  drift from the paper.

## Delivery and printing (`deliver/`)

- **pyipp's serializer silently returns empty bytes for attribute names it
  does not know**, and `sides` is one of them. The Print-Job request is
  therefore built with pyipp's `construct_attribute` and explicit tags and
  posted with `requests`; pyipp's client is used only for
  Get-Printer-Attributes. Sessions use `trust_env=False` so a proxy
  variable never applies to a LAN printer.
- **The Brother HL-L2460DW does not accept `application/pdf` over IPP.** It
  lists `application/octet-stream`, `image/urf`, `image/pwg-raster` at 300
  and 600 dpi. `deliver/pwg.py` encodes PWG Raster (1-bit `black_1` by
  default) and `pick_format()` prefers PDF, then PWG. `image/urf` alone is
  not supported. The header offsets are documented in `pwg.py`; the
  encoder has a matching decoder used by the tests. As of this writing it
  has not been verified on the physical printer: check orientation, both
  sides on one sheet, margins and text weight on the first real print.
- A one-page PDF is sent `one-sided` regardless of the duplex setting.
- mDNS printer discovery only works with host networking (or macvlan).
- `diagnose()` runs resolve, connect, IPP, format, state in order and stops
  at the first failure; its summaries are what the status strip shows.

## Sources

- **Substack RSS carries paid posts as previews.** A preview is never
  printed; a paid publication needs the IMAP route (`IMAP_*` variables,
  optional `IMAP_MAILBOX`) or the post is skipped with a warning.
- The queue: window of `article_max_age_days` (optionally widened in steps
  when empty), publications in Sources-tab order, oldest unread first,
  up to `QUEUE_LIMIT` (8) offered to the layout. Posts are marked seen
  only after a real successful run, and a partially printed post counts
  as used. `guid` is stripped from `data.json`; `url` stays.
- **In the queue view, ask "has it printed?" before "is it too old?"**
  A post printed from a widened window is older than the window forever
  after, so a preview that tests the age first buries it under
  `too-old` — the reader sees no record of a story that was in the paper
  that morning, and the row loses its "Mark as unread" button. Having been
  printed is a fact about the post; the window only decides what prints
  next.
- **NYT crossword**: `https://www.nytimes.com/svc/crosswords/v6/puzzle/daily/{YYYY-MM-DD}.json`
  with the `NYT-S` cookie (`NYT_S` variable). Keys: `body[0].cells`
  (`{}` is a black square), `body[0].clues` with `label`, `direction`,
  `text[].plain`, `publicationDate`. Answers are never stored or printed.
  Reference implementation: xword-dl's
  `src/xword_dl/downloader/newyorktimesdownloader.py`.
- Open-Meteo needs no key; the request is built with `forecast_days=2` so
  arrays are selected by ISO date, not index, which is what makes the
  `today=` test hook work.
- **Open-Meteo answers a momentary 503** now and then, and the 6 a.m. run
  gets only one shot at the forecast: a morning's ear box read "Forecast
  unavailable" while the Sources tab's Check button, run by hand an hour
  later, worked fine. `_request` therefore retries a transient failure
  (connection error, timeout, or a code in `RETRY_STATUS`) up to `ATTEMPTS`
  times with `BACKOFF_SECONDS` between tries, inside `BUDGET_SECONDS` (25),
  which stays under `gather.TIMEOUT_SECONDS` (30) — that budget is the
  thing to keep in mind if the attempts or the backoff ever grow. A 4xx is
  the API saying no and is not retried.
- Google Calendar's secret iCal address includes recurrences; declined
  events are detected by matching the attendee against `X-WR-CALNAME`.
- Substack blocks default user agents; the fetcher sends a browser-like
  one.

## Web page

- Auth is one Basic-auth middleware so `/static` and `/fonts` are covered.
  `POST /tasks` and `POST /lists/{slug}` are open (bearer token or the
  list's slug), as is `/healthz`. No `WEB_PASSWORD` means no login, and
  every page says so.
- The real `TASKS_TOKEN` appears in HTML only inside the copy button's
  data attribute and only when the page is behind `WEB_PASSWORD`;
  otherwise the button copies a placeholder. Container variables are
  otherwise shown as set or not set, never their values.
- Copy buttons chain `navigator.clipboard` and a hidden-textarea
  `execCommand` fallback because plain http on a LAN is not a secure
  context.
- The phone's post address is derived from `X-Forwarded-Proto` and
  `X-Forwarded-Host` when present (Tailscale Serve, reverse proxies), then
  the Host header, then the container's LAN IP; `tasks_post_url` overrides.
- The footer prints `Build <sha>` from `APP_BUILD`, set by the publish
  workflow. It is the fastest way to tell whether a container update took.
- **Every colour on the page is a token on `:root`** (`--ink`, `--paper`,
  `--grey`, ...), because `settings.web.theme` turns the whole page over at
  once. A colour written straight into a rule stays light in the dark, and
  that is exactly the bug nobody notices; `test_the_stylesheet_has_no_colour_outside_the_palette`
  fails the build for it.
- The dark palette is written twice on purpose — once under
  `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` and
  once under `:root[data-theme="dark"]` — because plain CSS cannot share a
  block between a media query and a selector. A test asserts the two agree.
  `color-scheme: dark` is what makes the browser's own radios, checkboxes
  and time pickers follow; without it they stay white.
- The theme is server state (`settings.web`), not localStorage, so it is the
  same on the phone and the laptop and `base.html` can put it on `<html>`
  before the page paints — no flash of the wrong theme, and no script. The
  switch is plain submit buttons and a `next` path, sanitised by `_own_path`
  so a posted form cannot turn it into an open redirect.
- **Starlette's `StaticFiles` sends no `Cache-Control`**, only an ETag and a
  Last-Modified, which leaves the browser free to guess how long a file
  stays fresh and reuse it without asking. It guessed wrong the first time
  the theme shipped: a reader's browser served the *previous* image's
  `style.css` with the new page, so `<html data-theme="dark">` was right and
  the page stayed white, because that stylesheet had no dark palette in it.
  Static files are mounted through `Revalidated` (`Cache-Control: no-cache`
  — "ask first", answered by a 304 with no body) and `base.html` hangs
  `?v={{ build }}` on the stylesheet and the script so a container update is
  a new address as well. Any future CSS or JS change depends on both; a
  symptom that "the new page has the old styling" is this, not the cascade.
- **Never set a control's `background` without its `color` in the same
  rule.** The stylesheet had `button { background: #fff }` and no `color`
  from the beginning, which left the text to the user agent. On a device
  whose system is dark the agent paints `buttontext` white — white on an
  explicitly white button — so the theme switch rendered as three empty
  boxes, reported as "the text inside the buttons isn't showing up". It
  needed a dark phone *and* the stale stylesheet above to show up, which is
  why no amount of desktop screenshotting found it.
  `test_no_control_sets_a_background_without_its_ink` fails the build for
  the whole class. Declaring `color-scheme` on `:root` (both branches) is
  the other half: the agent then draws its own widgets to match the page
  instead of guessing from the system.
- **Judge any UI control at `device_scale_factor=1`.** The theme switch was
  first set as grey small caps at 0.85rem, which looked fine in a 3x
  screenshot and was thin and captionish on the actual page — small caps
  shrinks the letterforms again on top of the size. It is a segmented
  control now, every choice in full `--ink` with a real border, the chosen
  one reversed (`background: var(--ink); color: var(--paper)`), which is the
  only marking that reads the same in both themes.
- The theme is tested where it is actually decided: `test_the_page_is_painted_the_way_she_set_it`
  loads the real page and the real stylesheet in Chromium across the four
  (choice, machine) pairs and asserts the painted background. Structural CSS
  assertions did not catch the caching bug and would not catch a cascade one.

## Deployment

- **Docker Hub login rejects a mixed-case username** with the misleading
  error `malformed HTTP Authorization header`. The workflow lowercases
  `DOCKERHUB_USERNAME` and checks both secrets' shape (without printing
  them) before logging in.
- Manual builds: run the `docker` workflow from the Actions tab (or the
  API) with the `tag` input, default `dev`. Pushes to `main` and `v*` tags
  publish automatically.
- **Unraid with host networking ignores port mappings**; the app listens on
  8080 inside the container. With bridge networking, map any host port to
  container 8080. **Per-container Tailscale**: the tailnet node lives in
  the container's own network, so Tailscale Serve's port must be the
  container's internal 8080, not the host mapping.
- The Unraid template can be fetched with `wget` into
  `/boot/config/plugins/dockerMan/templates-user/`; Unraid keeps its own
  copy after the container is created, so image updates never need it
  again.
- State lives in `/data/state.json`: `issue`, `volume`, `issues_total`,
  `first_issue_date`, `last_issue`, `seen_posts`, `last_*`,
  `printer_check`, `crossword_check`. Same-day papers archive as
  `<date>.pdf`, `<date>-2.pdf`, ... and never overwrite.

## Working with agents on this repo

- Split parallel work by file ownership and put the data or settings
  contract in every brief; two agents editing `run.py` at once is how
  things get lost.
- A message sent to a running agent arrives appended to one of its tool
  results, and a careful agent may treat it as untrusted and refuse it. Put
  anything important in the initial brief, or do it yourself afterwards.
- Every render-touching brief should demand the default-identical check
  (md5 of the sample PNGs before and after) and a 150 dpi look at the
  result; several layout bugs were only visible in the picture.
- The stop hook complains about uncommitted files while agents are
  mid-edit. Do not commit half-finished agent work; commit when the agent
  reports and the suite is green.
