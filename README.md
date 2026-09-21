# Personal Paper

The paper's name, its fonts and its whole look are set per deployment on the
Look tab; the sample issue calls itself "Personal Paper" only until you type
your own name for it there.

A one-reader morning newspaper: the reader's calendar, their own lists and
the weather in the rail, and the newest posts from the Substacks they read on the front
page, printed exactly as their authors wrote them. Rendered every morning in a
Docker container on the Unraid box, then printed on the Brother and/or
emailed as a PDF.

Design and decisions: [`PLAN.md`](PLAN.md). Rules for anyone changing the
code: [`CLAUDE.md`](CLAUDE.md).

## How it runs

One container, one process. A FastAPI app serves a password-protected
settings page on port 8080, accepts the phone's list syncs, and runs an
in-process scheduler that produces the paper at the time set on the page.

```
scheduler ─▶ gather (calendar, weather, substack, lists) ─▶ render (Chromium) ─▶ archive ─▶ print / email
```

Everything persistent lives under `/data`: `settings.json` (the page's
settings), `state.json` (issue counter, seen posts), `lists/<name>.json`,
`archive/<date>.pdf` (`-2`, `-3` for another paper made the same day),
`out/<date>/` (that day's data and HTML) and
`logs/run.log`.

## Install on Unraid

1. Docker tab → Add Container → Template: paste the contents of
   `unraid-template.xml`, or add the repository `hunterl31/personal-paper:latest`
   by hand with host networking, port 8080, and `/data` mapped to
   `/mnt/user/appdata/personal-paper`.
2. Fill in the variables. None is required to start. `WEB_PASSWORD` puts a
   login on the page; leave it empty and the page is open to anyone on
   your network, which the page itself points out.
   `TASKS_TOKEN` guards the phone sync (optional: each list also accepts
   its own address name as the token), the `SMTP_*` set for
   emailing the PDF, `NYT_S` for the crossword, and the `IMAP_*` set only
   if a paid Substack needs the email route (IMAP). See `.env.example` for
   each one.
3. Open `http://<unraid-ip>:8080/`. With `WEB_PASSWORD` set the browser
   asks for a username and password: the username is ignored (type
   anything), the password is `WEB_PASSWORD`. Then work through the tabs:
   - **Output**: pick the printer (**Find printers**, or type its address,
     then **Check the connection** and **Print a test page**), and/or tick
     **Email the paper every morning** with the addresses; set the time and
     days under **When to make the paper**. **Right now**, at the foot of the
     tab, has three buttons: the first sends the latest issue to those routes
     again exactly as it was made, the second makes a new paper — the next
     stories from the queue, a new issue number, and those stories marked as
     printed — and the third makes that same new paper without sending it
     anywhere, to look at on the Preview tab. Sending an issue again gathers
     nothing, counts no issue and marks no story as printed, so it costs
     nothing to press twice.
   - **Sources**: paste the calendar's secret address, list the Substack
     publications in priority order, set the weather location, switch the
     crossword on if you want one, and name your lists and follow the
     **How the phone sends this list** box beside each of them.
   - **Look**: paper name, imprint, what each ear shows and the words it
     shows, where the date is printed and in what face, the three fonts,
     body text size and lead story depth.
   - **Layout**: the **Sections** table — which page each section goes on,
     and in what order — plus **Rail side**, the rail's width, how many
     stories the front page may hold, and the crossword's place and size.
     Check it on **Preview**.

   **Theme** at the end of the tab row sets this page light or dark, or
   leaves it to follow your phone or laptop. It is only this page: the
   paper is printed black on white whatever you choose.

With `docker compose` instead: copy `.env.example` to `.env`, fill it in,
`docker compose up -d`.

Host networking is what lets the page discover the printer over mDNS. On
bridge networking discovery finds nothing and the printer is entered by IP.

## Printing

The paper goes straight to the printer over IPP: no CUPS, no driver, nothing
to install on the Unraid box. **Check the connection** asks the printer what
it takes and the **Format** line says what will be sent:

- **PDF**, when the printer lists `application/pdf` — the sheet exactly as it
  was laid out.
- **PWG Raster**, when it does not. Plenty of AirPrint printers (the Brother
  HL-L2460DW among them) accept only raster pages, so the paper is rendered
  to pixels here, at the finest resolution the printer lists up to 600 dpi
  and in black and white: a two-page issue is about 600 KB at 300 dpi and
  2 MB at 600 dpi. The printed sheet looks the same; only what goes over the
  wire differs.

A printer that lists neither fails the check and says which formats it did
list. `image/urf`, Apple's own raster, is not written yet; if a printer offers
nothing else, that is what to add.

## The phone: lists

There is no API for the list apps, so the phone pushes. An iPhone Shortcut
personal automation, a few minutes before print time, collects a list and
sends it to that list's own address:

```
POST http://<unraid-ip>:8080/lists/tasks
Authorization: Bearer tasks
Content-Type: application/json

{"items": ["Return library books", "Water the fig tree"]}
```

The bearer token is either the list's address name (as above — it is
already in the URL, so it is no secret) or the container's `TASKS_TOKEN` if
you set one; with no `TASKS_TOKEN` set, no header at all is accepted too. A
plain body with one item per line works, and so does `{"tasks": [...]}`. If
a list's file is older than the age set on the Sources tab, that section
prints empty and the log says it was not synced, rather than printing
yesterday's list.

`POST /tasks` is still the `tasks` list, so a Shortcut made before lists had
names keeps working.

## Publishing the image

`.github/workflows/docker.yml` builds the image on every push to `main`
and on `v*` tags and pushes it to Docker Hub. Add two repository secrets on
GitHub (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | the Docker Hub account; the image is `<this>/personal-paper` |
| `DOCKERHUB_TOKEN` | a Docker Hub access token with read/write scope |

If the Docker Hub account is not `hunterl31`, change the image name in
`docker-compose.yml` and `unraid-template.xml` to match.

## Development

```
pip install -r requirements-dev.txt          # see CLAUDE.md if sgmllib3k fails to build
DATA_DIR=/tmp/ml-data python -m pytest -q    # offline, ~40 s, needs Chromium
DATA_DIR=/tmp/ml-data python run.py --dry-run --sample     # render the sample issue
python render/render.py render/sample_data.json --png      # PNGs in render/out/
DATA_DIR=/tmp/ml-data WEB_PASSWORD=x uvicorn app.main:app --port 8080
```

Each gatherer can be run alone: `python -m gather.weather`.

## House rules

1. Articles are printed verbatim. The layout may break an article after
   the last word that fits the front page and continue it inside; nothing
   may summarize, shorten or reword it. `tests/test_render.py` and
   `tests/test_look.py` check this on every render.
2. No LLM calls anywhere.
3. The paper prints every morning even when a source fails.
4. Credentials are set on the container; everything the reader changes
   lives on the settings page.

## Setting up each source

All of this happens on the **Sources** tab except the two credentials
that are set on the container. Every source has a Check button that
fetches live and shows what it found, so you can confirm each one before
the first morning.

### Calendar (Google Calendar)

1. In Google Calendar on the web, open Settings, then the calendar in the
   left list, then "Integrate calendar".
2. Copy the **Secret address in iCal format** (not the public one; it
   starts with `https://calendar.google.com/calendar/ical/…/private-…`).
   Treat it like a password: anyone with it can read the calendar.
3. Paste it into a calendar row on the Sources tab and press Check. It
   shows the calendar's name and today's event count. Save.
4. Repeat for each calendar that should appear in the rail. Events are
   merged and sorted, all-day first.

Declined invitations and cancelled events are left out. Times are shown
without a.m./p.m. because the order makes it obvious. If a recurring
event is missing, the feed itself is probably missing it: Google's secret
address includes recurrences, but a shared calendar you were only invited
to sometimes does not.

### Weather (Open-Meteo)

No account or key. Enter latitude and longitude for the house (right
click the spot in Google Maps and copy the pair, e.g. `37.7749,
-122.4194`) and press Check. It shows the summary, high and low for
today. The container's `TZ` variable is the zone the hours are in.

### Substack

Posts come from each publication's RSS feed. List the publications in
the order you want them on the front page (the first with a new post is
the lead) and press Check on each to see its latest title.

- **Name** is the part before `.substack.com`, e.g. `platformer` for
  `platformer.substack.com`. A custom domain works too: paste the full
  feed URL, e.g. `https://www.example.com/feed`.
- **Paid** should be ticked for any subscription you pay for. Paid posts
  arrive in the feed as previews, and a preview is never printed. With Paid
  ticked the gatherer fetches the full post from the email Substack sent
  you instead, over the email route (IMAP) set up below.

Posts are a queue rather than a news feed: the oldest post you have not
been given yet goes first, publication by publication in the order above,
so nothing is skipped while newer posts jump ahead of it. **Skip posts
older than … days** (7 by default, 1–60) is how far back that queue looks —
a post still waiting when it passes that age is never printed, which is
what keeps the first morning from printing the archive. **Show what's
waiting** fetches every publication and lists what would print, without
marking anything.

Each post prints once. Four stories fit a sheet at most, and a story is
printed whole or held for a morning with room; when the last slot holds
only the beginning of one, the paper prints the address of the rest.

**The email route (IMAP), for paid posts (Gmail):**

1. In Gmail, create a label, e.g. `Paper`, and a filter: From contains
   `substack.com`, apply label `Paper`. Optionally "Skip the Inbox".
2. In your Google account, Security, enable 2-Step Verification if it is
   not on, then App passwords: create one named `Personal Paper`. Google
   shows a 16-character password once.
3. Set the container variables `IMAP_HOST=imap.gmail.com`,
   `IMAP_USER=<your gmail address>`, `IMAP_PASSWORD=<the app password>`,
   `IMAP_MAILBOX=Paper` (the label name; `INBOX` if you skipped step 1).
   Apply, which restarts the container.
4. The Sources tab shows each `IMAP_*` setting as "set on the container".

The gatherer matches the email by the post's title within the last 24
hours and strips only email chrome (headers, footers, unsubscribe
links); the author's text is untouched.

### Crossword (NYT)

The New York Times daily puzzle, typeset into the paper from the Times'
own puzzle data: the grid and the clues, never the answers. It needs a
Games subscription and the session cookie from a browser you are already
logged in with.

1. In Chrome or Edge on a computer, sign in at `nytimes.com` with the
   account that has the Games subscription, and open today's crossword
   once so the session is live.
2. Open Developer tools (F12, or ⌥⌘I on a Mac), go to **Application**,
   then **Storage → Cookies → https://www.nytimes.com**.
3. Find the cookie named **`NYT-S`** and copy its **Value** — the whole
   string, which is long and looks like nonsense. In Safari the same
   thing is under Develop → Show Web Inspector → Storage → Cookies.
4. Set the container variable **`NYT_S`** to that value and apply, which
   restarts the container. The Sources tab then shows `NYT_S` as "set on
   the container". The cookie is good for about a year; when it expires the
   Check button says so and you repeat steps 1–3.
5. On the **Sources** tab, tick **Print the crossword**, untick any day
   you would rather not have one (Saturday's is the hard one), and Save.
6. Press **Check**. It fetches today's puzzle and shows the title, the
   constructor, the date, the grid size and the clue count — for example
   "Cross Purposes — Robyn Weintraub, 2026-09-17 (15x15, 72 clues)". If
   something is wrong it says which: `NYT_S is not set in the container`,
   `NYT-S cookie rejected (got a login page)` (the cookie was copied
   wrong, or it is not the subscriber's), `NYT-S cookie expired or
   invalid` (copy a fresh one), or `no puzzle for <date> at <url>`.

On a morning with no stories to print, the paper is a single page and the
puzzle takes the front, set larger where the lead story would have been.

Treat the cookie like a password: anyone with it is signed in as you.
It lives only in the container's variables, never in `settings.json` and
never in the browser.

**On the Times' terms.** This is automated access to a subscription, which
their terms discourage; it is one household's own copy of a puzzle it pays
for, and the owner of this paper has accepted that trade. No password is
stored and no login is scripted — the cookie is copied by hand from a
browser that is already signed in. If the fetch fails for any reason the
paper prints without a puzzle; nothing here can stop the morning's paper.

### Lists (from the phone)

Each list is a row in the **Lists from your phone** table on the Sources
tab: a name, the style it is set in (checkboxes, plain lines or numbers),
and how old a sync may be before the section prints empty instead of stale.
Saving the row works out its **address name** — "Weekend shopping" becomes
`weekend-shopping` — and that never changes afterwards, because it is half
of the address the phone is set up with. Under the table, each list has a
**How the phone sends this list** box with its own address and the exact
header, with copy buttons.

A new list goes on page 1 by itself; move it to page 2, or leave it out, in
the **Sections** table on the Layout tab.

1. Optionally set the container variable `TASKS_TOKEN` to a long random
   string and apply. It is not required: a list also accepts its own
   address name as the token, and with no `TASKS_TOKEN` set a post with no
   Authorization header at all is accepted.
2. The Sources tab shows the exact address and header for each list. They are:

   ```
   POST http://<unraid-ip>:8080/lists/<address-name>
   Authorization: Bearer <address-name>
   Content-Type: application/json
   {"items": ["first item", "second item"]}
   ```

   A plain body with one item per line also works, and `{"tasks":
   [...]}` is still read. The address is built from the address your
   browser is using, so if the box is behind a reverse proxy or answers on
   another port from the phone's side, type the right address into
   **Address the phone posts to** on the same tab and every box follows it.
   With a `TASKS_TOKEN` set, the box also offers `Bearer <TASKS_TOKEN>` and
   shows the first four characters of the token the container is holding,
   so you can tell at a glance whether the Shortcut is sending the same one.
3. On the iPhone, open Shortcuts and create one shortcut per list:
   - An action that produces that list as text. With Apple Reminders:
     **Find Reminders** where *Is Completed* is false and *List* is the one
     you want (and, for the to-do list, *Due Date* is today), then
     **Combine Text** with a new line. If the list app has its own
     Shortcuts actions, use those instead.
   - **Get Contents of URL**: URL as above, Method POST, one header
     `Authorization` whose value is the word `Bearer`, a space, and the
     list's address name or the token itself (no angle brackets: for the
     list `groceries` the value is `Bearer groceries`; the Sources tab has
     a button that copies the exact value), Request Body = File, pick the
     combined text. A Content-Type header is not needed; either text lines
     or JSON is accepted.
4. Run it once by hand and look at the Sources tab: each box shows how many
   items were received and when.
5. Automations tab, New, Time of Day, a few minutes before print time,
   Run Immediately (turn off "Ask Before Running"), and pick the shortcut.

### Layout

The **Sections** table is where each section of furniture goes: Today's
agenda, the hourly forecast, the notes box and one row per list. Each row
has an order number and a **Page**: **Page 1** puts it in your own column
on the front of the sheet, **Page 2** in the same column on the back beside
the continued stories, and **Not shown** leaves it out entirely. Under each
name, in italics, is the heading the paper itself prints over that section —
the hourly forecast prints as "Hour by hour", today's agenda as "Today" —
because the paper keeps its own newspaper voice.

**Rail side** runs your column down the right edge of both pages (the
default) or the left. Below it are the rail's width, how many stories the
front page may hold (1–4), and where the crossword sits on page 2 with its
square size and the most of the page it may fill.

Changing any of it changes how much fits, never a word of an article. Judge
it on the **Preview** tab before it hits paper.

#### A list longer than the front page

Your column is one flow. A long to-do list is not cut off at the foot of
page 1: it runs on into a column of the same width on the back of the
sheet, under its own heading again, with *Continued on Page 2* where it
broke. It always breaks between two whole items, never inside one.

Making that column costs the continued stories some width, so a very long
list can mean one story fewer on the sheet — they carry the line saying
where the rest of them is online, and your list does not.

If even both columns cannot hold everything, the sheet says so rather than
losing it quietly: the list ends with *"12 more, not printed"*, and any
section that was pushed off entirely is named at the foot of the column.
Sections fill in the order you set on this tab, so what you want most
should be first.
