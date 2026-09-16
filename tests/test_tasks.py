"""Tasks gatherer: a fresh sync prints, a stale one does not, a missing one does not."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.settings import Settings, Sources
from gather import tasks

TASKS = ["Return library books", "Order more coffee beans", "Water the fig tree"]


def settings_with(max_age_hours: int = 24) -> Settings:
    return Settings(sources=Sources(tasks_max_age_hours=max_age_hours))


def write(data_dir, payload: dict) -> None:
    (data_dir / "tasks.json").write_text(json.dumps(payload))


def hours_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def test_fresh_sync(data_dir):
    write(data_dir, {"tasks": TASKS, "updated": hours_ago(2)})
    assert tasks.fetch(settings_with()) == TASKS


def test_stale_sync_is_empty(data_dir, caplog):
    write(data_dir, {"tasks": TASKS, "updated": hours_ago(30)})
    with caplog.at_level("WARNING"):
        assert tasks.fetch(settings_with()) == []
    assert "tasks not synced" in caplog.text


def test_stale_threshold_comes_from_settings(data_dir):
    write(data_dir, {"tasks": TASKS, "updated": hours_ago(30)})
    assert tasks.fetch(settings_with(max_age_hours=48)) == TASKS


def test_missing_file_is_empty(data_dir, caplog):
    with caplog.at_level("WARNING"):
        assert tasks.fetch(settings_with()) == []
    assert "tasks not synced" in caplog.text


def test_unreadable_file_is_empty(data_dir, caplog):
    (data_dir / "tasks.json").write_text("{not json")
    with caplog.at_level("WARNING"):
        assert tasks.fetch(settings_with()) == []


def test_missing_timestamp_is_not_synced(data_dir, caplog):
    write(data_dir, {"tasks": TASKS})
    with caplog.at_level("WARNING"):
        assert tasks.fetch(settings_with()) == []
    assert "tasks not synced" in caplog.text


def test_z_suffix_timestamp(data_dir):
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    write(data_dir, {"tasks": TASKS, "updated": stamp.replace("+00:00", "Z")})
    assert tasks.fetch(settings_with()) == TASKS


def test_cleaning(data_dir):
    write(data_dir, {
        "tasks": ["  Return library books  ", "", "   ", "Water the fig tree"],
        "updated": hours_ago(1),
    })
    assert tasks.fetch(settings_with()) == ["Return library books", "Water the fig tree"]


def test_cap_at_forty(data_dir):
    write(data_dir, {
        "tasks": [f"Task {n}" for n in range(60)], "updated": hours_ago(1),
    })
    got = tasks.fetch(settings_with())
    assert len(got) == 40
    assert got[0] == "Task 0" and got[-1] == "Task 39"


def test_write_tasks_round_trip(data_dir):
    written = tasks.write_tasks(["  Call the vet ", "", "Send Mom the photos"])
    assert written == ["Call the vet", "Send Mom the photos"]
    payload = json.loads((data_dir / "tasks.json").read_text())
    assert payload["tasks"] == written
    assert datetime.fromisoformat(payload["updated"]).tzinfo is not None
    assert tasks.fetch(settings_with()) == written
    assert not list(data_dir.glob("*.tmp"))


def test_write_tasks_creates_the_data_dir(tmp_path, monkeypatch):
    fresh = tmp_path / "elsewhere"
    monkeypatch.setenv("DATA_DIR", str(fresh))
    tasks.write_tasks(["Only one"])
    assert (fresh / "tasks.json").exists()


def test_status(data_dir):
    assert tasks.status() == {"age_hours": None, "count": 0, "updated": None}
    stamp = hours_ago(3)
    write(data_dir, {"tasks": TASKS, "updated": stamp})
    status = tasks.status()
    assert status["count"] == 3
    assert status["updated"] == stamp
    assert status["age_hours"] == pytest.approx(3.0, abs=0.1)
