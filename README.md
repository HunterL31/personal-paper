# Personal Paper

The paper's name, its fonts and its whole look are set per deployment on the
Look tab; the sample issue calls itself "Personal Paper" only until you type
your own name for it there.

A one-reader morning newspaper: the reader's calendar, to-do list and weather
in the rail, and the newest posts from the Substacks they read on the front
page, printed exactly as their authors wrote them. Rendered every morning in a
Docker container on the Unraid box, then printed on the Brother and/or
emailed as a PDF.

Design and decisions: [`PLAN.md`](PLAN.md). Rules for anyone changing the
code: [`CLAUDE.md`](CLAUDE.md).

## How it runs

One container, one process. A FastAPI app serves a password-protected
settings page on port 8080, accepts the phone's task sync, and runs an
in-process scheduler that produces the paper at the time set on the page.

```
scheduler ─▶ gather (calendar, weather, substack, tasks) ─▶ render (Chromium) ─▶ archive ─▶ print / email
```

Everything persistent lives under `/data`: `settings.json` (the page's
settings), `state.json` (issue counter, seen posts), `tasks.json`,
`archive/<date>.pdf`, `out/<date>/` (that day's data and HTML) and
`logs/run.log`.

## Install on Unraid

1. Docker tab → Add Container → Template: paste the contents of
   `unraid-template.xml`, or add the repository `hunterl31/personal-paper:latest`
   by hand with host networking, port 8080, and `/data` mapped to
   `/mnt/user/appdata/personal-paper`.
2. Fill in the variables. None is required to start. `WEB_PASSWORD` puts a
   login on the page; leave it empty and the page is open to anyone on
   your network, which the page itself points out.
   `TASKS_TOKEN` is needed for the phone sync, the `SMTP_*` set for
   emailing the PDF, the `IMAP_*` set only if a paid Substack needs the
   email route. See `.env.example` for each one.
3. Open `http://<unraid-ip>:8080/`. With `WEB_PASSWORD` set the browser
   asks for a username and password: the username is ignored (type
   anything), the password is `WEB_PASSWORD`. Then work through the tabs:
   - **Output**: pick the printer (Discover, or type its IP, then Test and
     Print test page), and/or enable email with the recipients; set the
     time and days.
   - **Sources**: paste the calendar's secret iCal address, list the
     Substacks in priority order, set the weather location, and follow the
     Shortcut instructions for tasks.
   - **Look**: fonts, size, name, ear text. Check it on **Preview**.

With `docker compose` instead: copy `.env.example` to `.env`, fill it in,
`docker compose up -d`.

Host networking is what lets the page discover the printer over mDNS. On
bridge networking discovery finds nothing and the printer is entered by IP.

## The phone: tasks

There is no API for the tasks app, so the phone pushes. An iPhone Shortcut
personal automation, a few minutes before print time, collects today's
tasks and sends them:

```
POST http://<unraid-ip>:8080/tasks
Authorization: Bearer <TASKS_TOKEN>
Content-Type: application/json

{"tasks": ["Return library books", "Water the fig tree"]}
```

A plain-text body with one task per line also works. If the file is older
than the age set on the Sources tab, the paper prints an empty to-do list
and logs "tasks not synced" rather than yesterday's list.

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
4. Credentials live in container variables; everything the reader changes
   lives on the settings page.

## Setting up each source

All of this happens on the **Sources** tab except the two credentials
that go in container variables. Every source has a Check button that
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
  arrive in RSS as previews, and a preview is never printed. With Paid
  ticked the gatherer fetches the full post from the email Substack sent
  you instead, which needs the IMAP setup below.

Only posts from the last seven days are considered, and each post prints
once. The first morning prints whatever is new that week, not the archive.

**Email route for paid posts (Gmail):**

1. In Gmail, create a label, e.g. `Paper`, and a filter: From contains
   `substack.com`, apply label `Paper`. Optionally "Skip the Inbox".
2. In your Google account, Security, enable 2-Step Verification if it is
   not on, then App passwords: create one named `Personal Paper`. Google
   shows a 16-character password once.
3. Set the container variables `IMAP_HOST=imap.gmail.com`,
   `IMAP_USER=<your gmail address>`, `IMAP_PASSWORD=<the app password>`,
   `IMAP_MAILBOX=Paper` (the label name; `INBOX` if you skipped step 1).
   Apply, which restarts the container.
4. The Sources tab shows each IMAP variable as "set in container".

The gatherer matches the email by the post's title within the last 24
hours and strips only email chrome (headers, footers, unsubscribe
links); the author's text is untouched.

### Tasks (from the phone)

There is no API for the tasks app, so an iPhone Shortcut pushes today's
list every morning before print time.

1. Set the container variable `TASKS_TOKEN` to a long random string
   (anything; it only has to match what the Shortcut sends). Apply.
2. The Sources tab shows the exact URL and header to use. It is:

   ```
   POST http://<unraid-ip>:8080/tasks
   Authorization: Bearer <TASKS_TOKEN>
   Content-Type: application/json
   {"tasks": ["first task", "second task"]}
   ```

   A `text/plain` body with one task per line also works.
3. On the iPhone, open Shortcuts and create a shortcut:
   - An action that produces today's tasks as text. With Apple Reminders:
     **Find Reminders** where *Is Completed* is false and *Due Date* is
     today, then **Combine Text** with a new line. If the tasks app has
     its own Shortcuts actions, use those instead; if it has none,
     Reminders is the fallback.
   - **Get Contents of URL**: URL as above, Method POST, Headers
     `Authorization` = `Bearer <TASKS_TOKEN>` and `Content-Type` =
     `text/plain`, Request Body = File, pick the combined text.
4. Run it once by hand and look at the Sources tab: it shows how many
   tasks were received and when.
5. Automations tab, New, Time of Day, a few minutes before print time,
   Run Immediately (turn off "Ask Before Running"), and pick the
   shortcut.

If the file is older than the "max age" on the Sources tab when the paper
runs, the to-do list prints empty rather than stale.
