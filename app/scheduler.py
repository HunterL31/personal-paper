"""The daily run, scheduled in-process.

One APScheduler cron job built from `settings.output.schedule`. Saving the
Output tab calls `reschedule()`, so a new print time takes effect without a
restart. The job takes `jobs.RUN_LOCK`, the same lock the web page's
buttons take, so "Run now" during the scheduled run waits rather than
double-printing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import jobs
from app.settings import Env, Schedule, Settings

log = logging.getLogger(__name__)

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
def run_daily() -> None:
    """What fires every morning. Catches everything; the app must survive it."""
    from run import run as run_paper

    log.info("scheduled run starting")
    try:
        with jobs.RUN_LOCK:
            result = run_paper(Settings.load())
        log.info("scheduled run: ok=%s pages=%s error=%s", result.ok, result.pages, result.error)
    except Exception:  # noqa: BLE001 - a failed run must not kill the scheduler
        log.exception("scheduled run failed")


# --------------------------------------------------------------- scheduler
def disabled() -> bool:
    return os.environ.get("PAPER_NO_SCHEDULER") == "1"


def start(settings: Settings) -> Optional[BackgroundScheduler]:
    """Start the scheduler and install the job. No-op under PAPER_NO_SCHEDULER."""
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


def reschedule(settings: Settings) -> Optional[datetime]:
    """Install (or remove) the daily job for these settings; return next run."""
    if _scheduler is None or not _scheduler.running:
        return None
    trigger = build_trigger(settings.output.schedule)
    if trigger is None:
        if _scheduler.get_job(JOB_ID):
            _scheduler.remove_job(JOB_ID)
        log.info("no days selected; daily run is off")
        return None
    _scheduler.add_job(
        run_daily,
        trigger=trigger,
        id=JOB_ID,
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE,
        coalesce=True,
        max_instances=1,
    )
    when = next_run_time()
    log.info("daily run scheduled; next fire %s", when)
    return when


def next_run_time() -> Optional[datetime]:
    if _scheduler is None or not _scheduler.running:
        return None
    job = _scheduler.get_job(JOB_ID)
    return getattr(job, "next_run_time", None)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
