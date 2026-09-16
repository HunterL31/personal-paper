"""Small shared helpers for the delivery routes."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def issue_date(pdf: Path | None = None) -> date:
    """The issue date a PDF belongs to.

    `run.py` writes the archive copy as `/data/archive/<YYYY-MM-DD>.pdf` and
    the working copy under `/data/out/<YYYY-MM-DD>/paper.pdf`, so the date is
    usually recoverable from the path; otherwise it is today's.
    """
    if pdf is not None:
        for part in (pdf.stem, pdf.parent.name):
            m = _DATE_RE.fullmatch(part)
            if m:
                try:
                    return date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    pass
    return date.today()


def issue_label(pdf: Path | None = None) -> str:
    """`YYYY-MM-DD`, as used in the attachment name and the IPP job name."""
    return issue_date(pdf).isoformat()
