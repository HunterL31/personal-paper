"""Lists gatherer: a fresh sync prints, a stale one does not, a missing one does not."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.settings import ListSource, RailSection, Settings, Sources, slugify
from gather import lists

ITEMS = ["Return library books", "Order more coffee beans", "Water the fig tree"]


def settings_with(*sources: ListSource) -> Settings:
    return Settings(sources=Sources(lists=list(sources) or [ListSource(name="To do", slug="tasks")]))


def write(data_dir, slug: str, payload: dict) -> None:
    folder = data_dir / "lists"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{slug}.json").write_text(json.dumps(payload))


def hours_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


# ------------------------------------------------------------------ fetch
def test_fetch_returns_one_entry_per_configured_list(data_dir):
    write(data_dir, "tasks", {"items": ITEMS, "updated": hours_ago(2)})
    write(data_dir, "groceries", {"items": ["Milk", "Bread"], "updated": hours_ago(1)})

    got = lists.fetch(settings_with(
        ListSource(name="To do", slug="tasks"),
        ListSource(name="Groceries", slug="groceries", style="plain", max_age_hours=48),
    ))

    assert got == [
        {"name": "To do", "slug": "tasks", "style": "checkbox", "items": ITEMS},
        {"name": "Groceries", "slug": "groceries", "style": "plain", "items": ["Milk", "Bread"]},
    ]


def test_a_list_with_no_file_is_present_and_empty(data_dir, caplog):
    with caplog.at_level("WARNING"):
        got = lists.fetch(settings_with(ListSource(name="Packing", slug="packing")))
    assert got == [{"name": "Packing", "slug": "packing", "style": "checkbox", "items": []}]
    assert "list packing not synced" in caplog.text


def test_a_stale_list_prints_empty(data_dir, caplog):
    write(data_dir, "tasks", {"items": ITEMS, "updated": hours_ago(30)})
    with caplog.at_level("WARNING"):
        got = lists.fetch(settings_with())
    assert got[0]["items"] == []
    assert "list tasks not synced" in caplog.text


def test_the_age_limit_is_per_list(data_dir):
    write(data_dir, "tasks", {"items": ITEMS, "updated": hours_ago(30)})
    got = lists.fetch(settings_with(ListSource(name="To do", slug="tasks", max_age_hours=48)))
    assert got[0]["items"] == ITEMS


def test_unreadable_file_is_empty(data_dir, caplog):
    (data_dir / "lists").mkdir()
    (data_dir / "lists" / "tasks.json").write_text("{not json")
    with caplog.at_level("WARNING"):
        assert lists.items("tasks") == []


def test_missing_timestamp_is_not_synced(data_dir, caplog):
    write(data_dir, "tasks", {"items": ITEMS})
    with caplog.at_level("WARNING"):
        assert lists.items("tasks") == []
    assert "list tasks not synced" in caplog.text


def test_z_suffix_timestamp(data_dir):
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    write(data_dir, "tasks", {"items": ITEMS, "updated": stamp.replace("+00:00", "Z")})
    assert lists.items("tasks") == ITEMS


def test_no_lists_configured_is_an_empty_contract(data_dir):
    settings = Settings()
    settings.sources.lists = []
    assert lists.fetch(settings) == []


# --------------------------------------------------------------- migration
def test_the_pre_lists_tasks_file_is_read_as_the_tasks_list(data_dir):
    (data_dir / "tasks.json").write_text(json.dumps({"tasks": ITEMS, "updated": hours_ago(2)}))
    got = lists.fetch(settings_with())
    assert got[0]["items"] == ITEMS
    assert lists.status("tasks")["count"] == 3


def test_the_new_file_wins_over_the_old_one(data_dir):
    (data_dir / "tasks.json").write_text(json.dumps({"tasks": ["Yesterday"], "updated": hours_ago(2)}))
    write(data_dir, "tasks", {"items": ["Today"], "updated": hours_ago(1)})
    assert lists.items("tasks") == ["Today"]


def test_only_the_tasks_slug_reads_the_old_file(data_dir):
    (data_dir / "tasks.json").write_text(json.dumps({"tasks": ITEMS, "updated": hours_ago(1)}))
    assert lists.items("groceries") == []


# ------------------------------------------------------------------ write
def test_cleaning(data_dir):
    write(data_dir, "tasks", {
        "items": ["  Return library books  ", "", "   ", "Water the fig tree"],
        "updated": hours_ago(1),
    })
    assert lists.items("tasks") == ["Return library books", "Water the fig tree"]


def test_cap_at_sixty(data_dir):
    written = lists.write_list("tasks", [f"Item {n}" for n in range(80)])
    assert len(written) == 60
    assert written[0] == "Item 0" and written[-1] == "Item 59"
    assert len(lists.items("tasks")) == 60


def test_write_list_round_trip(data_dir):
    written = lists.write_list("groceries", ["  Call the vet ", "", "Send Mom the photos"])
    assert written == ["Call the vet", "Send Mom the photos"]
    payload = json.loads((data_dir / "lists" / "groceries.json").read_text())
    assert payload["items"] == written
    assert datetime.fromisoformat(payload["updated"]).tzinfo is not None
    assert lists.items("groceries") == written
    assert not list((data_dir / "lists").glob("*.tmp"))


def test_write_list_creates_the_data_dir(tmp_path, monkeypatch):
    fresh = tmp_path / "elsewhere"
    monkeypatch.setenv("DATA_DIR", str(fresh))
    lists.write_list("tasks", ["Only one"])
    assert (fresh / "lists" / "tasks.json").exists()


def test_status(data_dir):
    assert lists.status("tasks") == {"age_hours": None, "count": 0, "updated": None}
    stamp = hours_ago(3)
    write(data_dir, "tasks", {"items": ITEMS, "updated": stamp})
    status = lists.status("tasks")
    assert status["count"] == 3
    assert status["updated"] == stamp
    assert status["age_hours"] == pytest.approx(3.0, abs=0.1)


# ------------------------------------------------------------------ slugs
@pytest.mark.parametrize("name, slug", [
    ("To do", "to-do"),
    ("Groceries", "groceries"),
    ("Weekend shopping!", "weekend-shopping"),
    ("  Pack   for Paris  ", "pack-for-paris"),
    ("Café 2026", "caf-2026"),
    ("", ""),
    ("!!!", ""),
    ("A" * 60, "a" * 40),
])
def test_slugify(name, slug):
    assert slugify(name) == slug


def test_a_list_derives_its_slug_from_its_name():
    assert ListSource(name="Weekend shopping").slug == "weekend-shopping"
    assert ListSource(name="To do", slug="tasks").slug == "tasks"
    assert ListSource(name="!!!").slug == "list"       # never empty: it is a URL


def test_an_old_settings_file_gets_the_default_list():
    settings = Settings.model_validate_json('{"sources": {"tasks_max_age_hours": 36}}')
    assert [(li.name, li.slug, li.max_age_hours) for li in settings.sources.lists] == [
        ("To do", "tasks", 36)
    ]
    assert [s.key for s in settings.look.layout.sections] == [
        "agenda", "list:tasks", "hourly", "notes"
    ]


def test_loading_keeps_the_layout_in_step_with_the_lists(data_dir):
    """A settings file edited by hand still prints every list it names."""
    settings = Settings()
    settings.sources.lists.append(ListSource(name="Groceries"))
    settings.look.layout.sections = [RailSection(key="agenda"), RailSection(key="list:gone")]
    settings.save()

    loaded = Settings.load()
    assert [s.key for s in loaded.look.layout.sections] == [
        "agenda", "list:tasks", "list:groceries"
    ]
