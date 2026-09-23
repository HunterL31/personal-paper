"""Background jobs for the web page: previews and runs.

A preview render takes a few seconds in Chromium, so the buttons start a
job in a daemon thread and the page polls `GET /jobs/<id>`. Everything that
renders or prints takes `RUN_LOCK`, so a preview can never overlap the
scheduled run.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import papers

log = logging.getLogger(__name__)

#: Every render, run or test print holds this. The scheduler holds it too.
RUN_LOCK = threading.Lock()

#: How many preview directories to keep on disk.
KEEP_PREVIEWS = 3

_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


def preview_root() -> Path:
    """One for the container, not one per paper: a job's id is unique, so a
    preview is found by it alone whichever paper's page asked for it."""
    return papers.root() / "preview"


def preview_dir(job_id: str) -> Path:
    return preview_root() / job_id


def get(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return _jobs.get(job_id)


def latest(kind: str) -> Optional[Job]:
    with _jobs_lock:
        jobs = [j for j in _jobs.values() if j.kind == kind]
    return max(jobs, key=lambda j: j.started, default=None)


def start(kind: str, work: Callable[["Job"], dict[str, Any]]) -> Job:
    """Run `work(job)` in a daemon thread while holding `RUN_LOCK`."""
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with _jobs_lock:
        _jobs[job.id] = job

    def target() -> None:
        try:
            with RUN_LOCK:
                job.result = work(job) or {}
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - a job never takes the app down
            log.error("job %s (%s) failed: %s", job.id, kind, traceback.format_exc())
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "error"
        finally:
            job.finished = time.time()

    # The job works for the paper whose page started it.
    papers.start_thread(target, name=f"job-{kind}-{job.id}")
    return job


def prune_previews(keep: int = KEEP_PREVIEWS) -> None:
    """Keep only the newest `keep` preview directories."""
    root = preview_root()
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def reset() -> None:  # pragma: no cover - used by tests
    with _jobs_lock:
        _jobs.clear()
