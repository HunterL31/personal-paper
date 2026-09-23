"""
The papers one container makes: one reader each, printed one after another.

The first paper is the one the container has always made, and it lives
where it always has, at the top of `DATA_DIR`. Every other paper has a
folder of its own, `<DATA_DIR>/papers/<id>/`, laid out exactly the same way
(settings.json, state.json, lists/, out/, archive/, logs/), so nothing one
reader changes, syncs or prints can reach another's paper.

`<DATA_DIR>/papers.json` holds the order they print in: `{"order": ["main",
"sam"]}`. With no file there is one paper, `main`, which is the container
as it was before there could be more.

Which paper a piece of code is working for is a context variable, not an
argument: `data_dir()` answers for the current paper, and everything that
reads or writes state already goes through a `data_dir()`. The scheduler,
the CLI and the web page's `/p/<id>/` prefix set it with `using(id)`; a
thread started for a paper carries it along with `contextvars.copy_context`.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

#: The paper that lives at the top of DATA_DIR.
MAIN = "main"
#: Where the other papers live, under DATA_DIR.
PAPERS_DIRNAME = "papers"
REGISTRY_NAME = "papers.json"
#: A paper's id is in its web address (`/p/<id>/`) and its folder name.
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
ID_MAX = 40
#: The web page's prefix for a paper that is not the main one.
URL_PREFIX = "/p/"

_current: contextvars.ContextVar[str] = contextvars.ContextVar("paper", default=MAIN)
_lock = threading.Lock()


# ------------------------------------------------------------- the context
def root() -> Path:
    """DATA_DIR itself, read fresh (tests and the web app move it)."""
    return Path(os.environ.get("DATA_DIR", "/data"))


def current() -> str:
    """The id of the paper this code is working for."""
    return _current.get()


def data_dir(paper: str | None = None) -> Path:
    """Where a paper keeps everything: DATA_DIR for the main paper."""
    pid = paper or current()
    if pid == MAIN:
        return root()
    return root() / PAPERS_DIRNAME / pid


@contextmanager
def using(paper: str) -> Iterator[str]:
    """Work for `paper` inside the block, and for whoever it was after it."""
    if not valid_id(paper):
        raise ValueError(f"not a paper id: {paper!r}")
    token = _current.set(paper)
    try:
        yield paper
    finally:
        _current.reset(token)


def url_prefix(paper: str | None = None) -> str:
    """"" for the main paper, "/p/<id>" for the others: the web page's base."""
    pid = paper or current()
    return "" if pid == MAIN else f"{URL_PREFIX}{pid}"


def start_thread(target, *, name: str, daemon: bool = True) -> threading.Thread:
    """A thread that works for the same paper as the code that starts it.

    A plain `threading.Thread` starts in an empty context, which would be
    the main paper's: a gatherer started for another paper would read the
    main paper's lists and state.
    """
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(target,), name=name, daemon=daemon)
    thread.start()
    return thread


class PaperFilter(logging.Filter):
    """Passes only the lines logged while working for one paper.

    A run's log files hang off the root logger, so without this every paper's
    run.log would carry every other paper's lines too.
    """

    def __init__(self, paper: str):
        super().__init__()
        self.paper = paper

    def filter(self, record: logging.LogRecord) -> bool:
        return current() == self.paper


# ------------------------------------------------------------ the registry
def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def registry_path() -> Path:
    return root() / REGISTRY_NAME


def ids() -> list[str]:
    """Every paper, in the order they print. Never raises; `main` is always one."""
    order: list[str] = []
    try:
        stored = json.loads(registry_path().read_text())
        raw = stored.get("order") if isinstance(stored, dict) else None
        for pid in raw or []:
            if valid_id(pid) and pid not in order:
                order.append(pid)
    except FileNotFoundError:
        pass
    except Exception as exc:  # a bad registry must never stop the main paper
        log.warning("%s unreadable (%s); printing the main paper only", REGISTRY_NAME, exc)
    if MAIN not in order:
        order.insert(0, MAIN)
    return order


def exists(paper: str) -> bool:
    return paper in ids()


def _save(order: list[str]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"order": order}, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def new_id(name: str) -> str:
    """An id for a paper called `name`, unused and not `main`."""
    base = "-".join(re.findall(r"[a-z0-9]+", (name or "").lower()))[:ID_MAX - 3].strip("-")
    base = base or "paper"
    taken = set(ids())
    pid, n = base, 1
    while pid in taken or pid == MAIN or data_dir(pid).exists():
        n += 1
        pid = f"{base}-{n}"
    return pid


def add(name: str) -> str:
    """Register a new paper after the others, make its folder, return its id.

    The folder starts empty: the caller writes its settings.
    """
    with _lock:
        pid = new_id(name)
        data_dir(pid).mkdir(parents=True, exist_ok=True)
        _save([*ids(), pid])
    log.info("paper %s added", pid)
    return pid


def remove(paper: str) -> None:
    """Stop making a paper. Its folder is kept, renamed, never deleted:
    an archive of issues is not something a button should destroy."""
    if paper == MAIN:
        raise ValueError("the main paper cannot be removed")
    with _lock:
        order = ids()
        if paper not in order:
            return
        _save([p for p in order if p != paper])
        folder = data_dir(paper)
        if folder.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            folder.rename(folder.with_name(f".removed-{paper}-{stamp}"))
    log.info("paper %s removed", paper)


def move(paper: str, step: int) -> None:
    """Move a paper earlier (step < 0) or later in the print order."""
    with _lock:
        order = ids()
        if paper not in order:
            return
        i = order.index(paper)
        j = max(0, min(len(order) - 1, i + step))
        order.insert(j, order.pop(i))
        _save(order)
