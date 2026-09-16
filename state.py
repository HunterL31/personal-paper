"""
`<DATA_DIR>/state.json`: the few facts that survive between runs.

Keys
----
issue         int   number of issues printed so far; the next paper is issue + 1
seen_posts    list  Substack post GUIDs already printed (gather/substack.py)
last_run      str   ISO timestamp of the last attempt
last_success  str   ISO timestamp of the last successful run
last_error    str   message of the last failure ("" once a run succeeds)
last_pages    int   page count of the last successful paper
last_pdf      str   path of the last archived PDF

Everything is written atomically; a corrupt or missing file reads as defaults,
because a bad state file must never stop the paper.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
_lock = threading.Lock()

DEFAULT_STATE: dict[str, Any] = {
    "issue": 0,
    "seen_posts": [],
    "last_run": "",
    "last_success": "",
    "last_error": "",
    "last_pages": 0,
    "last_pdf": "",
}


def data_dir() -> Path:
    """DATA_DIR is read on every call so tests (and the web app) can move it."""
    return Path(os.environ.get("DATA_DIR", "/data"))


def state_path() -> Path:
    return data_dir() / "state.json"


def load_state() -> dict[str, Any]:
    p = state_path()
    state = json.loads(json.dumps(DEFAULT_STATE))
    try:
        if p.exists():
            stored = json.loads(p.read_text())
            if isinstance(stored, dict):
                state.update(stored)
    except Exception as exc:  # corrupt file: start from defaults, keep going
        log.warning("state.json unreadable (%s); using defaults", exc)
    return state


def save_state(state: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


def update_state(**values: Any) -> dict[str, Any]:
    """Merge `values` into the stored state and write it back."""
    state = load_state()
    state.update(values)
    save_state(state)
    return state


def next_issue() -> int:
    """The number the paper being made now will carry."""
    return int(load_state().get("issue", 0)) + 1


def bump_issue() -> int:
    """Count one printed issue and return the new number."""
    state = load_state()
    state["issue"] = int(state.get("issue", 0)) + 1
    save_state(state)
    return state["issue"]
