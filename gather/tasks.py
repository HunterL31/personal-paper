"""
Tasks gatherer.

There is no API to call: the phone pushes. An iPhone Shortcut posts today's
list to the app's `POST /tasks`, which calls `write_tasks()`; this module
reads that file back at run time. A file that is missing or stale means the
sync did not happen, which is a log line and an empty section — never a
stale list printed as if it were today's.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_TASKS = 40
FILENAME = "tasks.json"


def _data_dir() -> Path:
    import app.settings as app_settings

    return Path(os.environ.get("DATA_DIR") or app_settings.DATA_DIR)


def path() -> Path:
    return _data_dir() / FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean(tasks: Any) -> list[str]:
    """Strip whitespace, drop empties, keep her order, cap the list."""
    out: list[str] = []
    for task in tasks or []:
        if not isinstance(task, str):
            task = str(task)
        task = task.strip()
        if task:
            out.append(task)
    if len(out) > MAX_TASKS:
        logger.info("tasks: %d posted, printing the first %d", len(out), MAX_TASKS)
    return out[:MAX_TASKS]


def _read() -> Optional[dict]:
    p = path()
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text())
    except Exception:
        logger.warning("tasks: %s is not readable JSON", p, exc_info=True)
        return None
    return payload if isinstance(payload, dict) else None


def _updated(payload: dict) -> Optional[datetime]:
    raw = payload.get("updated")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("tasks: unparseable timestamp %r", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(payload: dict) -> Optional[float]:
    dt = _updated(payload)
    if dt is None:
        return None
    return (_now() - dt).total_seconds() / 3600.0


def fetch(settings) -> list[str]:
    """Today's tasks, or an empty list if the phone did not sync."""
    payload = _read()
    if payload is None:
        logger.warning("tasks not synced: no %s", path())
        return []
    age = _age_hours(payload)
    max_age = float(getattr(settings.sources, "tasks_max_age_hours", 24))
    if age is None:
        logger.warning("tasks not synced: no usable timestamp in %s", path())
        return []
    if age > max_age:
        logger.warning(
            "tasks not synced: last sync was %.1f h ago (limit %.0f h)", age, max_age
        )
        return []
    return clean(payload.get("tasks"))


def write_tasks(tasks: list[str]) -> list[str]:
    """
    Store what the Shortcut posted, atomically. Called by `POST /tasks`.
    Returns the cleaned list that was written.
    """
    cleaned = clean(tasks)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tasks": cleaned, "updated": _now().isoformat()}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, p)
    logger.info("tasks: stored %d", len(cleaned))
    return cleaned


def status() -> dict:
    """What the Sources tab shows: how old the sync is, and how many tasks."""
    payload = _read()
    if payload is None:
        return {"age_hours": None, "count": 0, "updated": None}
    return {
        "age_hours": _age_hours(payload),
        "count": len(clean(payload.get("tasks"))),
        "updated": payload.get("updated"),
    }


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.settings import Settings

    print(json.dumps(fetch(Settings.load()), indent=2, ensure_ascii=False))
