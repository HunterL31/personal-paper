"""The daily runs, scheduled in-process.

One APScheduler cron job per print time among the container's papers
(`papers.py`), each paper's time and days from its own
`settings.output.schedule`. Papers set to the same time share one job,
which makes them one after the other in the order the Papers page lists
them, so two readers' sheets come off the printer in a known order.
Saving any paper's Output tab calls `reschedule()`, so a new print time
takes effect without a restart. Every paper's run takes `jobs.RUN_LOCK`,
the same lock the web page's buttons take, so a run started by hand
during the scheduled one waits rather than double-printing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import papers
from app import jobs
from app.settings import Env, Schedule, Settings

log = logging.getLogger(__name__)

#: Every daily job's id starts with this; the rest is the time, "0600".
JOB_ID = "daily-paper"
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
#: A container that was restarting at 06:00 still prints at 06:10.
MISFIRE_GRACE = 1800

_scheduler: Optional[BackgroundScheduler] = None


# ------------------------------------------------------------------ trigger
def parse_time(value: str) -> tuple[int, int]:
    """"07:15" -> (7, 15). Anything unparseable falls back to 06:00."""
    try:
        hour, _, minute = (value or "").partition(":")
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    log.warning("unreadable schedule time %r; using 06:00", value)
    return 6, 0


def build_trigger(schedule: Schedule) -> Optional[CronTrigger]:
    """The cron trigger for a schedule, or None when no day is selected."""
    days = sorted({int(d) for d in schedule.days if 0 <= int(d) <= 6})
    if not days:
        return None
    hour, minute = parse_time(schedule.time)
    return CronTrigger(
        hour=hour,
        minute=minute,
        day_of_week=",".join(DAY_NAMES[d] for d in days),
        timezone=Env.tz(),
    )


# ------------------------------------------------------------------ the job
def _days(schedule: Schedule) -> set[int]:
    return {int(d) for d in schedule.days if 0 <= int(d) <= 6}


def schedules() -> list[tuple[str, int, int, set[int]]]:
    """(paper, hour, minute, days) for every paper, in print order."""
    out = []
    for pid in papers.ids():
        with papers.using(pid):
            schedule = Settings.load().output.schedule
        hour, minute = parse_time(schedule.time)
        out.append((pid, hour, minute, _days(schedule)))
    return out


def _fired_on(hour: int, minute: int, now: datetime) -> int:
    """The weekday of the fire that is running now: the latest hour:minute
    at or before `now`, so a run late past midnight counts for its own day."""
    fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if fire > now:
        fire -= timedelta(days=1)
    return fire.weekday()


def run_paper(pid: str) -> None:
    """Make one paper. Catches everything; the app and the next paper must
    survive it."""
    from run import run as run_issue

    try:
        with papers.using(pid):
            log.info("scheduled run starting (paper %s)", pid)
            with jobs.RUN_LOCK:
                result = run_issue(Settings.load())
            log.info("scheduled run (paper %s): ok=%s pages=%s error=%s",
                     pid, result.ok, result.pages, result.error)
    except Exception:  # noqa: BLE001 - a failed run must not kill the scheduler
        log.exception("scheduled run failed (paper %s)", pid)


def run_due(hour: int, minute: int) -> None:
    """What fires at hour:minute: every paper set to that time and today,
    one after the other, in print order."""
    try:
        today = _fired_on(hour, minute, datetime.now(ZoneInfo(Env.tz())))
        due = [pid for pid, h, m, days in schedules()
               if (h, m) == (hour, minute) and today in days]
    except Exception:  # noqa: BLE001
        log.exception("could not work out which papers are due")
        return
    for pid in due:
        run_paper(pid)


# --------------------------------------------------------------- scheduler
def disabled() -> bool:
    return os.environ.get("PAPER_NO_SCHEDULER") == "1"


def start(settings: Settings | None = None) -> Optional[BackgroundScheduler]:
    """Start the scheduler and install the jobs. No-op under PAPER_NO_SCHEDULER."""
    global _scheduler
    if disabled():
        log.info("scheduler disabled (PAPER_NO_SCHEDULER=1)")
        return None
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=Env.tz())
    if not _scheduler.running:
        _scheduler.start()
    reschedule(settings)
    return _scheduler


def reschedule(settings: Settings | None = None) -> Optional[datetime]:
    """Install the daily jobs for every paper's schedule, drop the ones no
    paper uses any more, and return the current paper's next run.

    `settings` is accepted for the callers that save one paper's; every
    paper's schedule is read fresh either way.
    """
    if _scheduler is None or not _scheduler.running:
        return None
    wanted: dict[tuple[int, int], set[int]] = {}
    for _pid, hour, minute, days in schedules():
        if days:
            wanted.setdefault((hour, minute), set()).update(days)
    ids = set()
    for (hour, minute), days in wanted.items():
        job_id = f"{JOB_ID}-{hour:02d}{minute:02d}"
        ids.add(job_id)
        _scheduler.add_job(
            run_due,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                day_of_week=",".join(DAY_NAMES[d] for d in sorted(days)),
                timezone=Env.tz(),
            ),
            args=(hour, minute),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE,
            coalesce=True,
            max_instances=1,
        )
    for job in _scheduler.get_jobs():
        if job.id.startswith(JOB_ID) and job.id not in ids:
            _scheduler.remove_job(job.id)
    if not ids:
        log.info("no days selected on any paper; the daily run is off")
        return None
    when = next_run_time()
    log.info("daily runs scheduled at %s; this paper's next %s",
             ", ".join(sorted(i[len(JOB_ID) + 1:] for i in ids)), when)
    return when


def next_run_time() -> Optional[datetime]:
    """When the current paper is next made, or None when it is not scheduled."""
    if _scheduler is None or not _scheduler.running:
        return None
    trigger = build_trigger(Settings.load().output.schedule)
    if trigger is None:
        return None
    return trigger.get_next_fire_time(None, datetime.now(ZoneInfo(Env.tz())))


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
