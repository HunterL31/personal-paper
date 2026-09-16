import os
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


@pytest.fixture
def fixtures():
    return FIXTURES


@pytest.fixture
def sample_data():
    import json
    return json.loads((REPO / "render" / "sample_data.json").read_text())
