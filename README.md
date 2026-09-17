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
2. Fill in the variables. Only `WEB_PASSWORD` is required to start.
   `TASKS_TOKEN` is needed for the phone sync, the `SMTP_*` set for
   emailing the PDF, the `IMAP_*` set only if a paid Substack needs the
   email route. See `.env.example` for each one.
3. Open `http://<unraid-ip>:8080/`. The browser shows a username and
   password prompt: the username is ignored (type anything), the password
   is `WEB_PASSWORD`. Then work through the tabs:
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
