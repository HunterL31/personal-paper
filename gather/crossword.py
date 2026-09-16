"""
Crossword gatherer — interface only in v1.

`fetch()` always returns None, deliberately. The NYT puzzle has no API and
its print PDF sits behind her login; scripting that login is fragile and
against their terms, so whether and how to include a puzzle is a decision
for the owner of the paper, not for this implementation (PLAN.md,
"crossword.py"). The interface exists so the rest of the pipeline can be
written once: if a Path is ever returned it is appended as the back page(s)
via `render.py --crossword`, and any failure here returns None so the paper
never waits on it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def fetch(settings) -> Optional[Path]:
    """No puzzle in v1. Always None; never raises."""
    logger.debug("crossword: not implemented in v1 (see PLAN.md)")
    return None


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.settings import Settings

    result = fetch(Settings.load())
    print(json.dumps(str(result) if result else None))
