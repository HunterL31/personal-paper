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
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import traceback
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # so `python run.py` finds app/, gather/, render/
    sys.path.insert(0, str(HERE))

from app.settings import DATA_DIR as _DEFAULT_DATA_DIR  # noqa: E402
from app.settings import Env, Settings  # noqa: E402
from render.render import render as render_paper  # noqa: E402
from state import bump_issue, next_issue, update_state  # noqa: E402

log = logging.getLogger("run")

SAMPLE_DATA = HERE / "render" / "sample_data.json"

#: The crossword gets the same wall-clock budget as a gatherer: the puzzle
#: is a nice-to-have and the paper is not waiting on it (house rule 3).
CROSSWORD_TIMEOUT = 30.0


@dataclass
class RunResult:
    date: str
    pdf: Optional[Path] = None
    pages: int = 0
    gather_errors: dict[str, str] = field(default_factory=dict)
    #: True when today's puzzle was fetched and typeset on page 2
    crossword: bool = False
    #: indices into the gathered articles that were printed in full; the
    #: rest did not fit the sheet and stay unseen so they print another day
    printed: list[int] = field(default_factory=list)
    #: route name -> error message, or None when that route succeeded
    delivery: dict[str, Optional[str]] = field(default_factory=dict)
    ok: bool = False
    error: Optional[str] = None


# ------------------------------------------------------------------ helpers
def data_dir() -> Path:
    """DATA_DIR, read fresh (tests and the web app move it)."""
    return Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA_DIR)))


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(Env.tz()))
    except Exception:
        return datetime.now()


def long_date(d: datetime) -> str:
    """"Wednesday, September 16, 2026" — no %-d, which is glibc-only."""
    return f"{d:%A, %B} {d.day}, {d.year}"


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
        "tasks": [],
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


def _deliver(pdf: Path, settings: Settings) -> dict[str, Optional[str]]:
    try:
        from deliver import deliver
    except Exception as exc:
        log.error("deliver unavailable: %s", exc)
        return {"deliver": f"{type(exc).__name__}: {exc}"}
    try:
        return dict(deliver(pdf, settings) or {})
    except Exception as exc:
        log.error("deliver failed: %s", traceback.format_exc())
        return {"deliver": f"{type(exc).__name__}: {exc}"}


def _mark_seen(guids: list[str]) -> None:
    """Remember delivered Substack posts so they are not printed again.

    Only called after a successful real run: a failed or dry run leaves the
    posts unseen so they print tomorrow instead of being lost.
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
    """
    _setup_file_logging()
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
                "volume": f"Vol. I, No. {next_issue()}",
                "date": long_date(now),
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
        # of the render contract, so keep it out of data.json.
        # One guid per article, positionally, so the printed indices reported
        # by the layout can be mapped back to the posts that actually appeared.
        articles = data.get("articles") or []
        guids = [a.pop("guid", None) if isinstance(a, dict) else None for a in articles]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

        # 4. render
        rendered = render_paper(data, look, out_dir)
        result.pdf, result.pages = rendered.pdf, rendered.pages
        result.printed = list(getattr(rendered, "printed", []) or [])
        dropped = [i for i in range(len(articles)) if i not in result.printed]
        if dropped:
            log.info(
                "did not fit the sheet, held for another day: %s",
                "; ".join(str((articles[i] or {}).get("title", i)) for i in dropped),
            )
        log.info("rendered %s page(s) -> %s", rendered.pages, rendered.pdf)

        # 5. archive (always, before any route runs)
        archive = data_dir() / "archive" / f"{day}.pdf"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rendered.pdf, archive)
        result.pdf = archive

        # 6. deliver
        if dry_run or replay:
            log.info("dry run: not delivering")
        else:
            result.delivery = _deliver(archive, settings)
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
            issue = bump_issue()
            fields["last_success"] = now.isoformat(timespec="seconds")
            log.info("issue %s printed", issue)
            _mark_seen([g for i, g in enumerate(guids) if g and i in result.printed])
        update_state(**fields)
    else:
        update_state(last_error=result.error or "unknown error")
        paper_name = settings.look.paper_name or "Personal Paper"
        _notify_failure(settings, f"{paper_name} failed on {day}: {result.error}")
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
