"""`/data/settings.json`: defaults, round trip, corruption, file mode."""
from __future__ import annotations

import json
import stat

from app.settings import Env, Look, Settings, SubstackSource


def test_defaults_with_an_empty_data_dir(data_dir):
    assert not (data_dir / "settings.json").exists()
    s = Settings.load()
    assert s.look.paper_name == "Personal Paper"
    assert s.look.body_size_pt == 9.0
    assert s.look.ear.initials == ""
    assert s.output.print.enabled is False
    assert s.output.schedule.time == "06:00"


def test_round_trip(data_dir):
    s = Settings()
    s.look.paper_name = "The Evening Gull"
    s.look.body_size_pt = 10.5
    s.look.body_font = "EB Garamond"
    s.look.ear.lines = ["Stories continue inside."]
    s.sources.substacks.append(SubstackSource(name="astralcodexten", paid=True))
    s.output.email.to = ["reader@example.com"]
    s.save()

    again = Settings.load()
    assert again.look.paper_name == "The Evening Gull"
    assert again.look.body_size_pt == 10.5
    assert again.look.body_font == "EB Garamond"
    assert again.look.ear.lines == ["Stories continue inside."]
    assert again.sources.substacks[0].name == "astralcodexten"
    assert again.sources.substacks[0].paid is True
    assert again.output.email.to == ["reader@example.com"]
    assert again.model_dump() == s.model_dump()


def test_a_corrupt_file_yields_defaults(data_dir):
    (data_dir / "settings.json").write_text("{ this is not json")
    s = Settings.load()
    assert s.model_dump() == Settings().model_dump()

    # ... and so does a file of the right shape with the wrong types.
    (data_dir / "settings.json").write_text(json.dumps({"look": {"body_size_pt": "huge"}}))
    assert Settings.load().look.body_size_pt == 9.0


def test_saved_file_is_private(data_dir):
    Settings().save()
    mode = stat.S_IMODE((data_dir / "settings.json").stat().st_mode)
    assert mode == 0o600
    assert not list(data_dir.glob("*.tmp"))


def test_unknown_font_name_is_stored_not_rejected():
    """The render falls back; settings themselves must not raise."""
    assert Look(body_font="Nonesuch").body_font == "Nonesuch"


def test_env_reports_what_is_set_without_leaking_it(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "hunter2")
    monkeypatch.delenv("TASKS_TOKEN", raising=False)
    status = Env.status()
    assert status["WEB_PASSWORD"] is True
    assert status["TASKS_TOKEN"] is False
    assert "hunter2" not in json.dumps(status)
