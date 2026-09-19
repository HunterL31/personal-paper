# Personal Paper

A one-reader morning newspaper, generated daily in a Docker container and
printed or emailed. The project, the image and the container are generic;
the paper's name is whatever its one reader types on the Look tab
(`settings.look.paper_name`). Read `PLAN.md` first; it is the design.

## House rules (never break these)

1. Articles are printed verbatim. Never summarize, paraphrase, shorten, or
   reword an article, title, deck, or byline. A paragraph is never altered
   and never split; the one exception is the front-page jump, where the
   fitting script breaks the text after the last word that fits the slot and
   continues the rest, unchanged, on page 2. The paper is exactly one
   double-sided sheet, printed one-sided on a morning with no articles (one
   page, the crossword on the front), and the sheet is filled: the articles
   that fit whole are printed whole, and the last article on the sheet may
   stop at a paragraph boundary with a line pointing to the rest online
   (`RenderResult.partial` says how many of its paragraphs were printed).
   The paragraphs left behind are held for another day, never trimmed, and
   nothing anywhere is ever summarized or reworded.
2. No LLM calls anywhere in the pipeline.
3. The paper is produced every morning even when a source fails: a failing
   gatherer yields an empty section and a log line, never an exception out
   of `run_all`.
4. Black only, US Letter, duplex. Look changes go through `app/settings.py`
   `Look`, never by editing article text.
5. Credentials live in container env vars (`app.settings.Env`); everything
   the reader changes lives in `/data/settings.json` (`app.settings.Settings`).

## Contracts

- `render/sample_data.json` is the shape `gather.run_all(settings)` returns.
  Each article carries `url` (the post's own page): the template prints it
  under a story that only partly fit, so it is part of the contract, not a
  secret, and it stays in `data.json`. `guid` does not: it is the run's own
  bookkeeping and is stripped before the file is written.
  `crossword` is nullable: the puzzle object `gather/crossword.py` returns, or
  `null` on any morning without one. It never carries the answers.
  `lists` replaces the old `tasks`: one entry per list configured on the
  Sources tab, in that order, `{"name", "slug", "style", "items"}`, with
  `items: []` when the phone did not sync. Where each one goes is
  `look.layout.sections`, not the data.
- `app/settings.py` is the settings contract. Extend it deliberately.
- `DATA_DIR` env (default `/data`) is where state, settings, archive, logs live.
- Each gatherer: `fetch(settings) -> <its part>` plus a `__main__` printing JSON.
- Credentials for a source are container variables (`NYT_S` for the
  crossword); what the reader switches on and off is `settings.sources`.

## Running

```
pip install -r requirements-dev.txt
DATA_DIR=/tmp/ml-data python -m pytest -q          # all tests, offline
DATA_DIR=/tmp/ml-data python run.py --dry-run       # render today, no delivery
python render/render.py render/sample_data.json --png   # sample issue PNGs in render/out/
DATA_DIR=/tmp/ml-data WEB_PASSWORD=x uvicorn app.main:app --port 8080
```

In this dev container Playwright is pinned to 1.56 to match the
pre-installed Chromium under `/opt/pw-browsers`; never run
`playwright install`. Tests must run with the network off: use fixtures
under `tests/fixtures/`.

## Before you dig in

`docs/AGENT-NOTES.md` holds what earlier sessions learned the hard way:
tooling pins, test traps, the fitting script's pitfalls, what the printer
really accepts, Docker Hub and Unraid gotchas, and how to split work
between agents. Read it before changing the template, delivery or the
workflow, and add to it when you hit something new.

## Style

Python 3.11, type hints, stdlib logging (`logging.getLogger(__name__)`),
no print in library code. Keep modules small. No new dependencies without
adding them pinned to `requirements.txt`.
