import os
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets an empty, private DATA_DIR."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("DATA_DIR", str(d))
    monkeypatch.delenv("SETTINGS_PATH", raising=False)
    import app.settings as s
    monkeypatch.setattr(s, "DATA_DIR", d)
    return d


@pytest.fixture(autouse=True)
def local_time():
    """A test that sets `TZ` leaves the process on that zone once anything
    has read it: monkeypatch puts the variable back but not the C library's
    idea of local time. Re-read it after every test, so `date.today()` is
    not a day behind in the next one."""
    yield
    time.tzset()


@pytest.fixture
def fixtures():
    return FIXTURES


@pytest.fixture
def sample_data():
    import json
    return json.loads((REPO / "render" / "sample_data.json").read_text())
