"""`/data/settings.json`: defaults, round trip, corruption, file mode."""
from __future__ import annotations

import json
import stat

import pytest
from pydantic import ValidationError

from app.settings import EarBox, Env, Look, Settings, SubstackSource


def test_defaults_with_an_empty_data_dir(data_dir):
    assert not (data_dir / "settings.json").exists()
    s = Settings.load()
    assert s.look.paper_name == "Personal Paper"
    assert s.look.body_size_pt == 9.0
    assert s.look.ear_left.kind == "weather"
    assert s.look.ear_right.kind == "monogram"
    assert s.look.ear_right.initials == ""
    assert s.output.print.enabled is False
    assert s.output.schedule.time == "06:00"


def test_round_trip(data_dir):
    s = Settings()
    s.look.paper_name = "The Evening Gull"
    s.look.body_size_pt = 10.5
    s.look.body_font = "EB Garamond"
    s.look.ear_right.lines = ["Stories continue inside."]
    s.sources.substacks.append(SubstackSource(name="astralcodexten", paid=True))
    s.output.email.to = ["reader@example.com"]
    s.save()

    again = Settings.load()
    assert again.look.paper_name == "The Evening Gull"
    assert again.look.body_size_pt == 10.5
    assert again.look.body_font == "EB Garamond"
    assert again.look.ear_right.lines == ["Stories continue inside."]
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


# --------------------------------------------------- the ears, old and new
def test_a_settings_file_from_before_two_ears_still_has_its_monogram(data_dir):
    """One `ear` is the right-hand box: the paper is set exactly as it was."""
    (data_dir / "settings.json").write_text(json.dumps({
        "look": {"ear": {"initials": "M. L.", "lines": ["Continued stories inside."]}}
    }))
    look = Settings.load().look
    assert look.ear_right.kind == "monogram"
    assert look.ear_right.initials == "M. L."
    assert look.ear_right.lines == ["Continued stories inside."]
    # ... and the left one is still the weather.
    assert look.ear_left.kind == "weather"


def test_the_legacy_ear_does_not_overwrite_a_box_that_is_already_set(data_dir):
    (data_dir / "settings.json").write_text(json.dumps({"look": {
        "ear": {"initials": "M. L.", "lines": ["Old"]},
        "ear_right": {"kind": "sun"},
    }}))
    assert Settings.load().look.ear_right.kind == "sun"


def test_an_ear_keeps_the_words_of_the_kinds_it_is_not_set_to(data_dir):
    """Trying another kind and coming back loses nothing."""
    s = Settings()
    s.look.ear_left = EarBox(kind="countdown", initials="M. L.", lines=["A line"],
                             countdown_date="2026-12-24", countdown_label="Christmas")
    s.look.date_place = "above"
    s.look.date_font = "Playfair Display"
    s.look.date_size_pt = 12.5
    s.save()

    look = Settings.load().look
    assert look.ear_left.kind == "countdown"
    assert (look.ear_left.initials, look.ear_left.lines) == ("M. L.", ["A line"])
    assert look.ear_left.countdown_date == "2026-12-24"
    assert look.ear_left.countdown_label == "Christmas"
    assert (look.date_place, look.date_font, look.date_size_pt) == ("above", "Playfair Display", 12.5)


def test_the_date_size_is_clamped_to_the_sizes_the_tab_offers():
    assert Look(date_size_pt=6.0).date_size_pt == 6.0
    assert Look(date_size_pt=24.0).date_size_pt == 24.0
    with pytest.raises(ValidationError):
        Look(date_size_pt=48.0)


# ------------------------------------------------------- enhanced logging
def test_enhanced_logging_is_off_and_keeps_seven_runs():
    logs = Settings().logs
    assert logs.enhanced is False
    assert logs.keep_runs == 7


def test_a_settings_file_from_before_enhanced_logging_still_loads(data_dir):
    (data_dir / "settings.json").write_text(json.dumps({"look": {"paper_name": "The Gull"}}))
    settings = Settings.load()
    assert settings.look.paper_name == "The Gull"
    assert settings.logs.enhanced is False


def test_enhanced_logging_round_trips(data_dir):
    s = Settings()
    s.logs.enhanced = True
    s.save()
    assert Settings.load().logs.enhanced is True


def test_the_number_of_runs_kept_is_clamped():
    with pytest.raises(ValidationError):
        Settings(logs={"keep_runs": 0})
    with pytest.raises(ValidationError):
        Settings(logs={"keep_runs": 31})
