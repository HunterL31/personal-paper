"""The email route: stdlib smtplib, the PDF attached, nothing clever.

`smtp` is whatever `app.settings.Env.smtp()` returned: a dict of
`host`, `port`, `user`, `password`, or `None` when the container variables
are not set.
"""
from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ._util import DEFAULT_PAPER_NAME, issue_label, slug

_LOGGER = logging.getLogger(__name__)

NOT_CONFIGURED = "SMTP not configured in container variables"
SSL_PORT = 465


def _check(smtp: dict | None) -> dict:
    if not smtp:
        raise RuntimeError(NOT_CONFIGURED)
    missing = [k for k in ("host", "user", "password") if not smtp.get(k)]
    if missing:
        raise RuntimeError(f"{NOT_CONFIGURED} (missing {', '.join(missing)})")
    return smtp


def attachment_name(pdf: Path, paper_name: str = DEFAULT_PAPER_NAME) -> str:
    """`<Paper name> YYYY-MM-DD.pdf` — what lands in the reader's mail app."""
    return f"{slug(paper_name)} {issue_label(Path(pdf))}.pdf"


def _send(message: EmailMessage, smtp: dict) -> None:
    host = smtp["host"]
    port = int(smtp.get("port") or 587)
    user = smtp["user"]
    password = smtp["password"]

    if port == SSL_PORT:
        with smtplib.SMTP_SSL(host, port, timeout=60) as server:
            server.login(user, password)
            server.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=60) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except (smtplib.SMTPNotSupportedError, smtplib.SMTPException):
            # A LAN relay (or the test server) may not offer STARTTLS; the
            # paper is not a secret, and the alternative is no paper.
            _LOGGER.warning("SMTP server %s:%s does not support STARTTLS", host, port)
        if password:
            try:
                server.login(user, password)
            except smtplib.SMTPNotSupportedError:
                _LOGGER.warning("SMTP server %s:%s does not offer AUTH", host, port)
        server.send_message(message)


def _message(to: list[str], subject: str, smtp: dict, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = smtp["user"]
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_pdf(
    pdf: Path,
    to: list[str],
    subject: str,
    smtp: dict,
    *,
    paper_name: str = DEFAULT_PAPER_NAME,
) -> None:
    """Mail today's issue to `to`. Raises on failure."""
    smtp = _check(smtp)
    if not to:
        raise RuntimeError("No email recipients configured")

    pdf = Path(pdf)
    name = attachment_name(pdf, paper_name)
    subtype = (mimetypes.guess_type(name)[0] or "application/pdf").split("/")[-1]

    message = _message(
        list(to),
        subject,
        smtp,
        f"{name} is attached.\n\n{slug(paper_name)}, printed at home before sunrise.\n",
    )
    message.add_attachment(
        pdf.read_bytes(), maintype="application", subtype=subtype, filename=name
    )

    _send(message, smtp)
    _LOGGER.info("emailed %s to %s", name, ", ".join(to))


def send_test(
    to: list[str], smtp: dict, *, paper_name: str = DEFAULT_PAPER_NAME
) -> None:
    """The Output tab's "Send test" button. Raises on failure."""
    smtp = _check(smtp)
    if not to:
        raise RuntimeError("No email recipients configured")

    name = slug(paper_name)
    message = _message(
        list(to),
        f"{name} — test message",
        smtp,
        f"This is a test from {name}.\n"
        f"If it arrived, the morning paper can be mailed from {smtp['user']}.\n",
    )
    _send(message, smtp)
    _LOGGER.info("sent test email to %s", ", ".join(to))
