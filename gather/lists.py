"""
Lists gatherer: the reader's own lists, pushed from the phone.

There is no API to call. An iPhone Shortcut posts a list to
`POST /lists/<slug>`, which calls `write_list()`; this module reads those
files back at run time. One file per list, `<DATA_DIR>/lists/<slug>.json`,
holding `{"items": [...], "updated": <ISO>}`.

A file that is missing or stale means the sync did not happen, which is a
log line and an empty section — never a stale list printed as if it were
today's.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Nobody prints more than this; the rail runs out long before.
MAX_ITEMS = 60
DIRNAME = "lists"
#: What the single to-do list was called before there were named lists.
LEGACY_FILENAME = "tasks.json"
LEGACY_SLUG = "tasks"


def _data_dir() -> Path:
    import app.settings as app_settings

    return Path(os.environ.get("DATA_DIR") or app_settings.DATA_DIR)


def dir_path() -> Path:
    return _data_dir() / DIRNAME


def path(slug: str) -> Path:
    return dir_path() / f"{slug}.json"


def legacy_path() -> Path:
    return _data_dir() / LEGACY_FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean(items: Any) -> list[str]:
    """Strip whitespace, drop empties, keep her order, cap the list."""
    out: list[str] = []
    for item in items or []:
        if not isinstance(item, str):
            item = str(item)
        item = item.strip()
        if item:
            out.append(item)
    if len(out) > MAX_ITEMS:
        logger.info("list: %d posted, printing the first %d", len(out), MAX_ITEMS)
    return out[:MAX_ITEMS]


def _load(p: Path) -> Optional[dict]:
    """Read one file as `{"items": [...], "updated": ...}`, or None.

    `tasks` is read as well as `items`, which is what the pre-lists
    `tasks.json` called them.
    """
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text())
    except Exception:
        logger.warning("list: %s is not readable JSON", p, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    stored = payload.get("items")
    if stored is None:
        stored = payload.get("tasks")
    return {"items": stored, "updated": payload.get("updated")}


def _read(slug: str) -> Optional[dict]:
    """This list's file, falling back to the pre-lists `tasks.json`.

    The migration is a read, not a rewrite: a `/data` mounted read-only
    still prints the list, and the first sync from the phone writes the
    file in its new place.
    """
    payload = _load(path(slug))
    if payload is None and slug == LEGACY_SLUG:
        payload = _load(legacy_path())
        if payload is not None:
            logger.info("list tasks: read from %s (pre-lists file)", legacy_path())
    return payload


def _updated(payload: dict) -> Optional[datetime]:
    raw = payload.get("updated")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("list: unparseable timestamp %r", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(payload: dict) -> Optional[float]:
    dt = _updated(payload)
    if dt is None:
        return None
    return (_now() - dt).total_seconds() / 3600.0


def items(slug: str, max_age_hours: float = 24.0) -> list[str]:
    """Today's items for one list, or [] when the phone did not sync."""
    payload = _read(slug)
    if payload is None:
        logger.warning("list %s not synced: no %s", slug, path(slug))
        return []
    age = _age_hours(payload)
    if age is None:
        logger.warning("list %s not synced: no usable timestamp in %s", slug, path(slug))
        return []
    if age > float(max_age_hours):
        logger.warning(
            "list %s not synced: last sync was %.1f h ago (limit %.0f h)",
            slug, age, float(max_age_hours),
        )
        return []
    return clean(payload.get("items"))


def fetch(settings) -> list[dict]:
    """
    Every configured list, in the order of the Sources tab:
    `[{"name", "slug", "style", "items"}, ...]`.

    A list whose file is missing or stale is present with `items: []`: the
    section is part of the paper's furniture even on a morning the phone
    never reached it.
    """
    configured = list(getattr(getattr(settings, "sources", None), "lists", None) or [])
    out: list[dict] = []
    for source in configured:
        out.append({
            "name": source.name,
            "slug": source.slug,
            "style": source.style,
            "items": items(source.slug, source.max_age_hours),
        })
    return out


def write_list(slug: str, posted: Any) -> list[str]:
    """
    Store what the Shortcut posted, atomically. Called by `POST /lists/<slug>`.
    Returns the cleaned list that was written.
    """
    cleaned = clean(posted)
    p = path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": cleaned, "updated": _now().isoformat()}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, p)
    logger.info("list %s: stored %d", slug, len(cleaned))
    return cleaned


def status(slug: str) -> dict:
    """What the Sources tab shows: how old the sync is, and how many items."""
    payload = _read(slug)
    if payload is None:
        return {"age_hours": None, "count": 0, "updated": None}
    return {
        "age_hours": _age_hours(payload),
        "count": len(clean(payload.get("items"))),
        "updated": payload.get("updated"),
    }


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.settings import Settings

    print(json.dumps(fetch(Settings.load()), indent=2, ensure_ascii=False))
