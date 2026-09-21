#!/usr/bin/env python3
"""
One issue of the paper: gather -> data.json -> render -> archive -> deliver.

    python run.py                      # today, real data, every enabled route
    python run.py --dry-run            # real data, render and archive only
    python run.py --sample             # render/sample_data.json instead of gathering
    python run.py --date 2026-09-16    # re-render an archived day, no delivery

Called by the scheduler and by the web page's buttons as
`run(settings, dry_run=..., date=...) -> RunResult`.  Exits non-zero on
failure.

`reprint(settings) -> ReprintResult` is the other half of the page's
"Right now": it hands the latest archived issue to the routes again, with
nothing gathered, nothing rendered and nothing counted.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # so `python run.py` finds app/, gather/, render/
    sys.path.insert(0, str(HERE))

from app.settings import DATA_DIR as _DEFAULT_DATA_DIR  # noqa: E402
from app.settings import Env, Settings  # noqa: E402
from render.render import render as render_paper  # noqa: E402
from state import (  # noqa: E402
    bump_issue,
    load_state,
    next_issue,
    roman,
    update_state,
    volume_number,
)

log = logging.getLogger("run")

SAMPLE_DATA = HERE / "render" / "sample_data.json"

#: The crossword gets the same wall-clock budget as a gatherer: the puzzle
#: is a nice-to-have and the paper is not waiting on it (house rule 3).
CROSSWORD_TIMEOUT = 30.0


@dataclass
class RunResult:
    date: str
    pdf: Optional[Path] = None
    #: what the render laid out: 2, or 1 on a morning with no articles
    pages: int = 0
    gather_errors: dict[str, str] = field(default_factory=dict)
    #: True when today's puzzle was fetched and typeset on page 2
    crossword: bool = False
    #: indices into the gathered articles that reached the sheet, whole or in
    #: part; the rest did not fit and stay unseen so they print another day
    printed: list[int] = field(default_factory=list)
    #: article index -> how many of its leading paragraphs were printed, for
    #: the one story that was carried only in part. Empty when none was.
    partial: dict[int, int] = field(default_factory=dict)
    #: route name -> error message, or None when that route succeeded
    delivery: dict[str, Optional[str]] = field(default_factory=dict)
    #: this run's own log file, when enhanced logging is on (settings.logs)
    log: Optional[Path] = None
    ok: bool = False
    error: Optional[str] = None


@dataclass
class ReprintResult:
    """One archived issue sent to the routes again. No issue was made."""

    pdf: Optional[Path] = None
    #: pages counted in the PDF itself, or None when it could not be counted
    pages: Optional[int] = None
    delivery: dict[str, Optional[str]] = field(default_factory=dict)
    ok: bool = False
    error: Optional[str] = None


#: What the page says when the archive is empty: there is nothing to send.
NOTHING_TO_REPRINT = "No paper has been made yet."

#: `<date>.pdf`, and `<date>-2.pdf` for a second issue made the same day.
ARCHIVE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?\.pdf$")

#: One enhanced-logging file per run: `<date>-<HHMMSS>.log`, which sorts
#: by name in the order the runs happened.
RUN_LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}\.log$")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: Libraries that stay at INFO even in an enhanced run. urllib3 writes the
#: whole of every URL it fetches at debug level, and a calendar address is
#: a credential: the run's log file is downloadable, so it keeps none.
QUIET_LOGGERS = (
    "urllib3", "requests", "httpx", "httpcore", "hpack", "charset_normalizer",
    "asyncio", "apscheduler", "PIL", "fontTools", "markdown_it",
)


# ------------------------------------------------------------------ helpers
def data_dir() -> Path:
    """DATA_DIR, read fresh (tests and the web app move it)."""
    return Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA_DIR)))


def archive_dir() -> Path:
    """`<DATA_DIR>/archive`: every issue that was ever made, by date."""
    return data_dir() / "archive"


def archive_target(day: str) -> Path:
    """Where today's issue is filed, never on top of one already there.

    The first paper of the day is `<date>.pdf`; a second run the same day
    is `<date>-2.pdf`, a third `-3.pdf`, and so on. An issue that was made
    is an issue that was made: the morning's sheet is still there to open
    after a new paper is made at noon.
    """
    folder = archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day}.pdf"
    nth = 2
    while path.exists():
        path = folder / f"{day}-{nth}.pdf"
        nth += 1
    return path


def latest_archive() -> Optional[Path]:
    """The newest issue on file, or None when none has been made yet.

    `state.last_pdf` is the run's own word for it; when that file is gone
    (or state was lost) the archive folder itself is asked, newest first.
    """
    stored = str(load_state().get("last_pdf") or "").strip()
    if stored:
        path = Path(stored)
        if path.is_file():
            return path
    try:
        pdfs = [p for p in archive_dir().iterdir() if p.is_file() and ARCHIVE_NAME.match(p.name)]
    except OSError:
        return None
    return max(pdfs, key=lambda p: (p.stat().st_mtime, p.name), default=None)


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(Env.tz()))
    except Exception:
        return datetime.now()


def long_date(d: datetime, fmt: str | None = None) -> str:
    """The folio date in the reader's chosen style (Look tab)."""
    from app.dates import format_date

    return format_date(d, fmt)


def empty_data() -> dict[str, Any]:
    """What a completely failed gather looks like. The paper still prints."""
    return {
        "paper": {},
        "weather": {
            "summary": "Forecast unavailable",
            "high": "—", "low": "—", "wind": "—",
            "sunrise": "—", "sunset": "—",
            "hourly": [],
        },
        "events": [],
        "lists": [],
        "articles": [],
        "crossword": None,
    }


def _setup_file_logging() -> None:
    """Append to <DATA_DIR>/logs/run.log, once per process per path."""
    path = data_dir() / "logs" / "run.log"
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path:
            return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except Exception as exc:  # a read-only /data must not stop the paper
        log.warning("cannot write run.log (%s)", exc)


# ------------------------------------------------- enhanced logging (Logs)
def run_log_dir() -> Path:
    """`<DATA_DIR>/logs/runs`: the file enhanced logging keeps per run."""
    return data_dir() / "logs" / "runs"


def run_logs() -> list[Path]:
    """Every run log on disk, newest first. Never raises."""
    try:
        files = [p for p in run_log_dir().iterdir()
                 if p.is_file() and RUN_LOG_NAME.match(p.name)]
    except OSError:
        return []
    # The name is the run's own date and time, so it sorts chronologically
    # however the files were copied about.
    return sorted(files, key=lambda p: p.name, reverse=True)


def _prune_run_logs(keep: int) -> None:
    """Keep the newest `keep` run logs and delete the rest."""
    for path in run_logs()[max(1, int(keep)):]:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("could not delete %s (%s)", path, exc)


@contextmanager
def _enhanced_log(settings: Settings) -> Iterator[Optional[Path]]:
    """This run's own debug log, when the reader asked for one.

    Yields the file's path, or None when enhanced logging is off (or the
    file cannot be written, which is never a reason not to print a paper).
    Inside, the root logger runs at DEBUG so everything the run and the
    gatherers say reaches this file; the handlers that were already there
    (run.log, the container's stdout) are pinned at the level they were
    running at, so neither is flooded, and `QUIET_LOGGERS` keeps the
    libraries that write credentials into their debug lines at INFO.
    """
    logs = getattr(settings, "logs", None)
    if logs is None or not logs.enhanced:
        yield None
        return

    path = run_log_dir() / f"{_now():%Y-%m-%d-%H%M%S}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:            # a read-only /data must not stop the paper
        log.warning("cannot write %s (%s)", path, exc)
        yield None
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    was = root.level
    floor = was if was > logging.NOTSET else logging.INFO
    pinned = [h for h in root.handlers if h.level == logging.NOTSET]
    quiet = [(logging.getLogger(name), logging.getLogger(name).level)
             for name in QUIET_LOGGERS]
    for h in pinned:
        h.setLevel(floor)
    for logger, _ in quiet:
        logger.setLevel(logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield path
    finally:
        root.setLevel(was)
        root.removeHandler(handler)
        handler.close()
        for h in pinned:
            h.setLevel(logging.NOTSET)
        for logger, level in quiet:
            logger.setLevel(level)
        _prune_run_logs(logs.keep_runs)


def _log_settings(settings: Settings, *, dry_run: bool, date: Optional[str],
                  sample: bool) -> None:
    """What this run was asked for and what the paper is set to.

    Never a credential: a calendar address is a secret, so the calendars
    are counted, not named, and the container's variables are reported set
    or not set, exactly as the web page reports them.
    """
    mode = "replaying " + date if date else ("sample data" if sample else "live")
    log.info("enhanced logging on")
    log.debug("run: %s%s", mode, "; dry run, nothing delivered" if dry_run else "")
    log.debug("data dir: %s; timezone: %s; build: %s; python %s",
              data_dir(), Env.tz(), os.environ.get("APP_BUILD") or "dev",
              sys.version.split()[0])
    src = settings.sources
    log.debug("sources: %d calendar(s), %d substack(s), %d list(s); "
              "weather at %s,%s; crossword %s; articles within %d day(s)%s",
              len(src.calendars), len(src.substacks), len(src.lists),
              src.weather.lat, src.weather.lon,
              "on" if src.crossword.enabled else "off",
              src.article_max_age_days,
              "; window widened when empty" if src.extend_window_when_empty else "")
    out = settings.output
    log.debug("output: print %s (%s, %s); email %s (%d address(es)); "
              "schedule %s on days %s; notify %s",
              "on" if out.print.enabled else "off",
              out.print.printer_host or "no printer",
              "duplex" if out.print.duplex else "one-sided",
              "on" if out.email.enabled else "off", len(out.email.to),
              out.schedule.time,
              ",".join(str(d) for d in out.schedule.days) or "none",
              out.notify)
    look = settings.look
    log.debug("look: %r; body %s at %.1fpt; up to %d front stor%s; rail on the %s",
              look.paper_name, look.body_font, look.body_size_pt,
              look.layout.front_stories,
              "y" if look.layout.front_stories == 1 else "ies",
              look.layout.rail_side)
    st = load_state()
    log.debug("state: issue %s; last run %s; last success %s; last error %s",
              st.get("issue"), st.get("last_run") or "never",
              st.get("last_success") or "never", st.get("last_error") or "none")
    log.debug("container variables set: %s",
              ", ".join(n for n, on in Env.status().items() if on) or "none")


def _log_result(result: RunResult, seconds: float) -> None:
    """The last lines of a run log: what came of all that."""
    for name, err in result.gather_errors.items():
        log.debug("gatherer %s reported nothing: %s", name, err)
    log.debug("delivery: %s",
              "; ".join(f"{route}: {err or 'ok'}"
                        for route, err in result.delivery.items()) or "nothing sent")
    log.info(
        "run finished in %.1fs: ok=%s pages=%s crossword=%s printed=%s "
        "partial=%s error=%s",
        seconds, result.ok, result.pages, result.crossword, result.printed,
        result.partial or "none", result.error or "none",
    )


def _gather(settings: Settings) -> tuple[dict[str, Any], dict[str, str]]:
    """gather.run_all(settings), with every failure turned into empty data."""
    try:
        from gather import run_all
    except Exception as exc:
        log.error("gather unavailable: %s", exc)
        return empty_data(), {"gather": f"{type(exc).__name__}: {exc}"}
    try:
        data = run_all(settings)
    except Exception as exc:  # run_all should never raise; belt and braces
        log.error("gather failed: %s", traceback.format_exc())
        return empty_data(), {"gather": f"{type(exc).__name__}: {exc}"}
    errors = dict(data.pop("errors", {}) or {})
    merged = empty_data()
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged, errors


def _crossword(settings: Settings) -> Optional[dict[str, Any]]:
    """Today's puzzle for the render contract, or None.

    Never raises, and never hangs the run.

    `gather.crossword.fetch` already swallows its own failures, so this is
    about the two things it cannot: an import that fails, and a request that
    never comes back. It runs on the same abandoned-on-timeout daemon thread
    as the gatherers.
    """
    source = getattr(getattr(settings, "sources", None), "crossword", None)
    if source is None or not source.enabled:
        return None
    try:
        from gather import submit
        from gather import crossword as crossword_gather
    except Exception as exc:
        log.warning("crossword unavailable: %s", exc)
        return None

    future = submit("crossword", crossword_gather.fetch, settings)
    try:
        puzzle = future.result(timeout=CROSSWORD_TIMEOUT)
    except FutureTimeout:
        log.error("crossword: timed out after %.0fs", CROSSWORD_TIMEOUT)
        return None
    except Exception as exc:
        log.error("crossword failed: %s\n%s", exc, traceback.format_exc())
        return None
    return puzzle or None


def _deliver(pdf: Path, settings: Settings, pages: Optional[int]) -> dict[str, Optional[str]]:
    """Hand the issue to the routes. `pages` is what the render laid out:
    a one-page paper is not a duplex job, and the print route says so."""
    try:
        from deliver import deliver
    except Exception as exc:
        log.error("deliver unavailable: %s", exc)
        return {"deliver": f"{type(exc).__name__}: {exc}"}
    try:
        return dict(deliver(pdf, settings, pages=pages) or {})
    except Exception as exc:
        log.error("deliver failed: %s", traceback.format_exc())
        return {"deliver": f"{type(exc).__name__}: {exc}"}


def _mark_seen(guids: list[str]) -> None:
    """Remember delivered Substack posts so they are not printed again.

    Only called after a successful real run: a failed or dry run leaves the
    posts unseen so they print tomorrow instead of being lost.

    A post that was carried only in part counts as used: the reader has the
    beginning on paper and the address of the rest, so reprinting it from the
    top the next morning would be the wrong thing.
    """
    if not guids:
        return
    try:
        from gather.substack import mark_seen
        mark_seen(guids)
    except Exception as exc:
        log.warning("could not mark posts seen: %s", exc)


def _notify_failure(settings: Settings, message: str) -> None:
    try:
        from deliver.notify import notify_failure
    except Exception:
        log.debug("no failure notifier available")
        return
    try:
        notify_failure(settings, message)
    except Exception as exc:
        log.warning("failure notification failed: %s", exc)


# ---------------------------------------------------------------------- run
def run(
    settings: Settings,
    *,
    dry_run: bool = False,
    date: Optional[str] = None,
    sample: bool = False,
) -> RunResult:
    """Make one issue.

    `dry_run` renders and archives but delivers nothing; `date` replays the
    archived `out/<date>/data.json` (also without delivering); `sample`
    renders `render/sample_data.json` instead of gathering.  Only a real,
    delivered run bumps the issue counter.

    The issue itself is `_run` below; this sets up the logging around it,
    which with enhanced logging on is a file of this run's own.
    """
    _setup_file_logging()
    started = time.monotonic()
    with _enhanced_log(settings) as path:
        if path is not None:
            _log_settings(settings, dry_run=dry_run, date=date, sample=sample)
        result = _run(settings, dry_run=dry_run, date=date, sample=sample)
        result.log = path
        if path is not None:
            _log_result(result, time.monotonic() - started)
    return result


def _run(
    settings: Settings,
    *,
    dry_run: bool,
    date: Optional[str],
    sample: bool,
) -> RunResult:
    """One issue, inside whatever logging `run` set up around it."""
    now = _now()
    replay = date is not None
    day = date or now.strftime("%Y-%m-%d")
    guids: list[str] = []
    result = RunResult(date=day)
    out_dir = data_dir() / "out" / day
    update_state(last_run=now.isoformat(timespec="seconds"))

    try:
        # 1-2. the data
        if replay:
            src = out_dir / "data.json"
            if not src.exists():
                raise FileNotFoundError(f"no archived data for {day}: {src}")
            data = json.loads(src.read_text())
            log.info("replaying %s", src)
        elif sample:
            data = json.loads(SAMPLE_DATA.read_text())
            log.info("rendering sample data")
        else:
            data, result.gather_errors = _gather(settings)
            for name, err in result.gather_errors.items():
                log.warning("gatherer %s: %s", name, err)

        # 3. the masthead facts and the archived copy of the data
        look = settings.look
        paper = dict(data.get("paper") or {})
        if not replay:
            paper.update({
                "name": look.paper_name,
                "volume": f"Vol. {roman(volume_number(now.date()))}, No. {next_issue(now.date())}",
                "date": long_date(now, getattr(look, "date_format", None)),
                "imprint": look.imprint,
                "price": look.price,
            })
        data["paper"] = paper
        # The puzzle is part of the day's data, not a separate document: the
        # template typesets it, so it is written to data.json with everything
        # else and a replay re-renders exactly the same paper.
        if not (replay or sample):
            data["crossword"] = _crossword(settings)
        data.setdefault("crossword", None)   # the key is always there, nullable
        result.crossword = bool(data.get("crossword"))
        # Substack hands back a `guid` per article so the run can mark the
        # posts seen once the issue is actually delivered; it is not part
        # of the render contract, so keep it out of data.json. `url` is
        # part of the contract (the template prints it under a story that
        # only partly fit) and is not a secret, so it stays.
        # One guid per article, positionally, so the printed indices reported
        # by the layout can be mapped back to the posts that actually appeared.
        articles = data.get("articles") or []
        guids = [a.pop("guid", None) if isinstance(a, dict) else None for a in articles]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

        # 4. render
        rendered = render_paper(data, look, out_dir)
        result.pdf, result.pages = rendered.pdf, rendered.pages
        # `printed` is every article that reached the sheet, whole or in part;
        # `partial` says how much of the one that was cut short was carried.
        result.partial = {int(k): int(v) for k, v in (getattr(rendered, "partial", {}) or {}).items()}
        printed = list(getattr(rendered, "printed", []) or [])
        printed += [i for i in result.partial if i not in printed]
        result.printed = printed
        dropped = [i for i in range(len(articles)) if i not in result.printed]
        if dropped:
            log.info(
                "did not fit the sheet, held for another day: %s",
                "; ".join(str((articles[i] or {}).get("title", i)) for i in dropped),
            )
        for index, paragraphs in sorted(result.partial.items()):
            title = str((articles[index] or {}).get("title", index)) if index < len(articles) else index
            # House rule 1: nothing was shortened. The sheet simply ran out,
            # and the post counts as used — it is not printed again tomorrow.
            log.info(
                "printed the first %d paragraph%s of %s; the rest is online",
                paragraphs, "" if paragraphs == 1 else "s", title,
            )
        log.info("rendered %s page(s) -> %s", rendered.pages, rendered.pdf)

        # 5. archive (always, before any route runs). A second paper made
        # the same day is filed beside the first, never over it.
        archive = archive_target(day)
        shutil.copyfile(rendered.pdf, archive)
        result.pdf = archive

        # 6. deliver
        if dry_run or replay:
            log.info("dry run: not delivering")
        else:
            result.delivery = _deliver(archive, settings, result.pages)
            for route, err in result.delivery.items():
                log.info("route %s: %s", route, err or "ok")

        # A run is a success if the paper exists and no enabled route failed
        # outright; with no routes configured the archive copy is the paper.
        failed = [r for r, e in result.delivery.items() if e]
        result.ok = not (failed and len(failed) == len(result.delivery))
        if not result.ok:
            result.error = "; ".join(f"{r}: {result.delivery[r]}" for r in failed)

    except Exception as exc:
        log.error("run failed: %s", traceback.format_exc())
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"

    # 7. state
    if result.ok:
        fields = {"last_error": "", "last_pages": result.pages, "last_pdf": str(result.pdf or "")}
        if not (dry_run or replay):
            issue = bump_issue(day)
            # The number this archived sheet carries, so a reprint can say
            # which issue it is sending.
            fields["last_issue"] = issue
            fields["last_success"] = now.isoformat(timespec="seconds")
            log.info("issue %s printed", issue)
            _mark_seen([g for i, g in enumerate(guids) if g and i in result.printed])
        else:
            # A test run or a replay files a sheet that was never counted as
            # an issue, so the paper on file carries no number.
            fields["last_issue"] = 0
        update_state(**fields)
    else:
        update_state(last_error=result.error or "unknown error")
        paper_name = settings.look.paper_name or "Personal Paper"
        _notify_failure(settings, f"{paper_name} failed on {day}: {result.error}")
    return result


# ------------------------------------------------------------------ reprint
def _page_count(pdf: Path) -> Optional[int]:
    """How many pages the archived PDF has, or None. Never raises."""
    try:
        from deliver.printer import page_count
    except Exception as exc:
        log.warning("cannot count pages: %s", exc)
        return None
    return page_count(pdf)


def reprint(settings: Settings) -> ReprintResult:
    """Send the latest archived issue to every enabled route again.

    Nothing is gathered, nothing is rendered, no issue is counted and no
    post is marked seen: this is the sheet that was already made, going out
    a second time. `state.last_run` and `last_error` are the record of the
    runs that make papers, so they are left exactly as they were; the only
    trace is a line in the log.
    """
    _setup_file_logging()
    result = ReprintResult()
    pdf = latest_archive()
    if pdf is None:
        result.error = NOTHING_TO_REPRINT
        log.warning("reprint: %s", result.error)
        return result

    result.pdf = pdf
    result.pages = _page_count(pdf)
    result.delivery = _deliver(pdf, settings, result.pages)
    routes = "; ".join(f"{r}: {e or 'ok'}" for r, e in result.delivery.items())
    log.info("reprinted %s: %s", pdf.name, routes or "no routes enabled")

    # The same rule as a run: it failed only when every enabled route did.
    failed = [r for r, e in result.delivery.items() if e]
    result.ok = not (failed and len(failed) == len(result.delivery))
    if failed:
        result.error = "; ".join(f"{r}: {result.delivery[r]}" for r in failed)
    return result


# ---------------------------------------------------------------------- CLI
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="render and archive, deliver nothing")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="re-render an archived day")
    ap.add_argument("--sample", action="store_true", help="use render/sample_data.json instead of gathering")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    settings = Settings.load()
    result = run(settings, dry_run=args.dry_run, date=args.date, sample=args.sample)
    if result.ok:
        print(f"{result.date}: {result.pages} page(s) -> {result.pdf}")
        return 0
    print(f"{result.date}: FAILED — {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
