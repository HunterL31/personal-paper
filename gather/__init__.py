"""
The gather step: `run_all(settings)` returns exactly the shape of
`render/sample_data.json`, plus an `errors` dict.

House rule 3: the paper is produced every morning even when a source
fails. Every gatherer therefore runs in its own thread, inside its own
try/except, with a wall-clock timeout; a failure or a hang yields that
section's empty value and an entry in `errors`, never an exception out of
`run_all`.

Gatherer modules are imported lazily, inside the worker, so a module that
is missing or has a syntax error is just another error entry.
"""
from __future__ import annotations

import importlib
import logging
import time
import traceback
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable

import papers

logger = logging.getLogger(__name__)

#: Wall-clock budget for the whole gather step (PLAN.md, run.py step 2).
#: Every gatherer starts at once, so this is also each gatherer's timeout.
TIMEOUT_SECONDS = 30.0

#: section name -> (module, function) — the lazy import happens in the worker.
GATHERERS: dict[str, tuple[str, str]] = {
    "weather": ("gather.weather", "fetch"),
    "events": ("gather.calendar", "fetch"),
    "lists": ("gather.lists", "fetch"),
    "articles": ("gather.substack", "fetch"),
}

# Used when gather.weather cannot even be imported.
_WEATHER_UNAVAILABLE: dict[str, Any] = {
    "summary": "Forecast unavailable",
    "high": None,
    "low": None,
    "wind": "",
    "sunrise": "",
    "sunset": "",
    "hourly": [],
}


def _empty_weather() -> dict:
    """The empty weather value: gather.weather.unavailable(), if reachable."""
    try:
        mod = importlib.import_module("gather.weather")
        return dict(mod.unavailable())
    except Exception:
        logger.debug("gather.weather.unavailable() not available", exc_info=True)
        return dict(_WEATHER_UNAVAILABLE)


def _empty(section: str) -> Any:
    if section == "weather":
        return _empty_weather()
    return []


def _call(module_name: str, func_name: str, settings) -> Any:
    """Import the gatherer lazily and call it. Runs on a worker thread."""
    started = time.monotonic()
    mod = importlib.import_module(module_name)
    fn: Callable = getattr(mod, func_name)
    try:
        return fn(settings)
    finally:
        # Only ever read in an enhanced-logging run, where "which source was
        # slow this morning" is most of the question.
        logger.debug("%s.%s took %.1fs", module_name, func_name,
                     time.monotonic() - started)


def _describe(section: str, value: Any) -> str:
    """One line about what a gatherer came back with, for a debug log."""
    try:
        if section == "weather":
            w = value or {}
            return (f"{w.get('summary')}, high {w.get('high')}, low {w.get('low')}, "
                    f"{len(w.get('hourly') or [])} hourly row(s)")
        if section == "events":
            return f"{len(value or [])} event(s)"
        if section == "lists":
            return "; ".join(f"{li.get('slug')}: {len(li.get('items') or [])} item(s)"
                             for li in (value or [])) or "no lists"
        if section == "articles":
            titles = "; ".join(str((a or {}).get("title", "")) for a in (value or []))
            return f"{len(value or [])} post(s) offered to the layout: {titles}"
    except Exception:  # noqa: BLE001 - a log line is never worth an exception
        pass
    return type(value).__name__


def submit(section: str, fn: Callable, *args) -> Future:
    """
    Run `fn` on a daemon thread and report through a `concurrent.futures`
    Future, so the caller can wait with `future.result(timeout=...)`.

    Public because run.py fetches the crossword the same way: outside
    `run_all`, but under the same rule that a hang is abandoned rather than
    waited on.

    Not a ThreadPoolExecutor: its workers are non-daemon, and the interpreter
    joins every non-daemon thread on the way out, so one wedged gatherer would
    keep the container alive forever. A daemon thread is abandoned instead.
    """
    future: Future = Future()

    def runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            future.set_exception(exc)

    # The gatherer works for the paper that asked (its lists, its state).
    papers.start_thread(runner, name=f"gather-{section}")
    return future


def run_all(settings) -> dict:
    """
    Run every gatherer and return the render contract.

    Returns `{paper, weather, events, lists, articles, errors}`. `paper.volume`
    and `paper.date` are left empty for run.py to fill (it owns the issue
    counter and the run date). `errors` maps a section name to a one-line
    reason; an empty dict means everything worked.
    """
    look = settings.look
    data: dict[str, Any] = {
        "paper": {
            "name": look.paper_name,
            "volume": "",       # run.py: "Vol. I, No. {n}" from state.json
            "date": "",         # run.py: "Wednesday, September 16, 2026"
            "imprint": look.imprint,
            "price": look.price,
        },
        "weather": None,
        "events": [],
        # One entry per configured list, in the order of the Sources tab.
        "lists": [],
        "articles": [],
        # The puzzle is fetched by run.py, outside the gather step, but the
        # key belongs to the contract, so it is here and nullable.
        "crossword": None,
        "errors": {},
    }
    errors: dict[str, str] = data["errors"]

    futures = {
        section: submit(section, _call, module_name, func_name, settings)
        for section, (module_name, func_name) in GATHERERS.items()
    }

    # Every gatherer starts at once, so one shared deadline is also each
    # gatherer's own timeout, and the whole step can never take longer.
    timeout = float(TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    for section, future in futures.items():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            result = future.result(timeout=remaining)
        except FutureTimeout:
            logger.error("%s: timed out after %.0fs", section, timeout)
            errors[section] = f"timed out after {timeout:.0f}s"
            data[section] = _empty(section)
        except Exception as exc:
            logger.error("%s failed: %s\n%s", section, exc, traceback.format_exc())
            errors[section] = f"{type(exc).__name__}: {exc}"
            data[section] = _empty(section)
        else:
            data[section] = result
            logger.debug("%s: %s", section, _describe(section, result))

    if data["weather"] is None:  # a gatherer returned None
        data["weather"] = _empty("weather")
    return data
