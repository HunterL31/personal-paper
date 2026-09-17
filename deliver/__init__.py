"""Delivery: run every enabled output route, and never raise.

A run is a success if the render succeeded and at least one enabled route
succeeded; each failed route is logged, returned, and shown on the Status
strip. The archive copy is written by `run.py` before any route runs, so
the issue survives even when every route here fails.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from app.settings import Env

from ._util import issue_label, slug
from .email import send_pdf, send_test
from .notify import notify_failure
from .printer import (
    Diagnosis,
    DiscoveredPrinter,
    PrinterInfo,
    Step,
    diagnose,
    discover,
    print_pdf,
    record_print_result,
    record_printer_check,
    test_printer,
)

if TYPE_CHECKING:
    from app.settings import Settings

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "deliver",
    "print_pdf",
    "test_printer",
    "diagnose",
    "discover",
    "record_print_result",
    "record_printer_check",
    "send_pdf",
    "send_test",
    "notify_failure",
    "Diagnosis",
    "Step",
    "PrinterInfo",
    "DiscoveredPrinter",
]


def _paper_name(settings: "Settings") -> str:
    """What the reader called the paper on the Look tab, or the default."""
    return slug(getattr(getattr(settings, "look", None), "paper_name", ""))


def _explained(host: str, err: Exception) -> Exception:
    """Say why a print failed in the printer's terms, not `requests`'.

    A failed job is the one moment the reader is owed a plain sentence, so
    the printer is diagnosed on the spot; the original error is kept in
    parentheses, and kept whole when the diagnosis finds nothing wrong.
    """
    original = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    diagnosis = diagnose(host)
    step = diagnosis.failed
    if step is None:
        record_print_result(host, original)
        return err

    record_printer_check(diagnosis)
    said = f"{step.detail} {step.hint}".strip()
    return RuntimeError(f"{said} ({original})")


def _print_route(pdf: Path, settings: "Settings", *, test: bool) -> None:
    route = settings.output.print
    if not route.printer_host:
        raise RuntimeError("Print route is enabled but no printer is configured")
    # A test print is the same operation on a different PDF (the sample
    # issue), so `test` changes nothing here.
    try:
        print_pdf(
            pdf,
            route.printer_host,
            duplex=route.duplex,
            paper_name=_paper_name(settings),
        )
    except Exception as err:
        explained = _explained(route.printer_host, err)
        if explained is err:
            raise
        raise explained from err
    record_print_result(route.printer_host)


def _email_route(pdf: Path, settings: "Settings", *, test: bool) -> None:
    route = settings.output.email
    if not route.to:
        raise RuntimeError("Email route is enabled but no recipients are configured")
    paper_name = _paper_name(settings)
    subject = (route.subject or f"{paper_name}, {{date}}").replace(
        "{date}", issue_label(pdf)
    )
    if test:
        subject = f"[test] {subject}"
    send_pdf(pdf, list(route.to), subject, Env.smtp(), paper_name=paper_name)


_ROUTES = {"print": _print_route, "email": _email_route}


def deliver(pdf: Path, settings: "Settings", *, test: bool = False) -> dict[str, str | None]:
    """Run every enabled route over `pdf`.

    Returns one entry per *enabled* route: `None` when it succeeded, the
    error text when it did not. Never raises; a route that blows up is the
    other routes' business only insofar as it is reported.
    """
    pdf = Path(pdf)
    results: dict[str, str | None] = {}

    for name, run_route in _ROUTES.items():
        route = getattr(settings.output, name)
        if not route.enabled:
            continue
        try:
            run_route(pdf, settings, test=test)
        except Exception as err:  # noqa: BLE001 - every route is isolated
            _LOGGER.exception("%s route failed", name)
            results[name] = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
        else:
            _LOGGER.info("%s route ok", name)
            results[name] = None

    if not results:
        _LOGGER.info("no output routes enabled; archive only")

    return results
