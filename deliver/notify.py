"""Failure notification: a missing paper at 6:10 has to be visible somewhere
other than the log. Never raises — a failed notification must not turn one
failure into two.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.settings import Env

_LOGGER = logging.getLogger(__name__)

UNRAID_NOTIFY = Path("/usr/local/emhttp/webGui/scripts/notify")


def _notify_unraid(message: str) -> None:
    if not UNRAID_NOTIFY.exists():
        _LOGGER.warning(
            "unraid notification requested but %s is not mounted", UNRAID_NOTIFY
        )
        return
    subprocess.run(
        [
            str(UNRAID_NOTIFY),
            "-e",
            "Molly Ledger",
            "-s",
            "Paper failed",
            "-d",
            message,
            "-i",
            "alert",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    _LOGGER.info("posted unraid notification")


def _notify_email(address: str, message: str) -> None:
    from email.message import EmailMessage

    from .email import _check, _send

    smtp = _check(Env.smtp())
    if not address:
        raise RuntimeError("notify = email but no notify_email is set")

    mail = EmailMessage()
    mail["From"] = smtp["user"]
    mail["To"] = address
    mail["Subject"] = "The Molly Ledger — the paper failed"
    mail.set_content(f"{message}\n")
    _send(mail, smtp)
    _LOGGER.info("sent failure notification to %s", address)


def notify_failure(settings, message: str) -> None:
    """Tell someone the paper did not come out. Swallows every error."""
    route = getattr(settings.output, "notify", "none")
    if route == "none":
        _LOGGER.debug("failure notification disabled: %s", message)
        return

    try:
        if route == "email":
            _notify_email(settings.output.notify_email, message)
        elif route == "unraid":
            _notify_unraid(message)
        else:
            _LOGGER.warning("unknown notify route %r", route)
    except Exception:
        _LOGGER.exception("failure notification (%s) itself failed", route)
