# Personal Paper

A one-reader morning newspaper, generated daily in a Docker container and
printed or emailed. The project, the image and the container are generic;
the paper's name is whatever its one reader types on the Look tab
(`settings.look.paper_name`). Read `PLAN.md` first; it is the design.

## House rules (never break these)

1. Articles are printed verbatim. Never summarize, paraphrase, shorten, or
   reword an article, title, deck, or byline. The only allowed operation is
   the template's fitting script breaking text after the last word that fits
   and continuing the rest, unchanged, on an inside page.
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
- `app/settings.py` is the settings contract. Extend it deliberately.
- `DATA_DIR` env (default `/data`) is where state, settings, archive, logs live.
- Each gatherer: `fetch(settings) -> <its part>` plus a `__main__` printing JSON.

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

## Style

Python 3.11, type hints, stdlib logging (`logging.getLogger(__name__)`),
no print in library code. Keep modules small. No new dependencies without
adding them pinned to `requirements.txt`.
