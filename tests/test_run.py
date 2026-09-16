"""
`run(settings)`: gather -> data.json -> render -> archive -> deliver, with
the issue counter and the state file behaving.

`gather.run_all` and `deliver.deliver` are monkeypatched here; this is the
wiring test, not their test.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import run as run_module
import state as state_module
from app.settings import Settings
from run import RunResult, run

SAMPLE = Path(__file__).resolve().parent.parent / "render" / "sample_data.json"


@pytest.fixture
def fake_gather(monkeypatch, sample_data):
    """`from gather import run_all` returns the sample data (plus errors)."""
    calls: dict[str, object] = {"count": 0, "errors": {}}

    def run_all(settings):
        calls["count"] += 1
        data = json.loads(SAMPLE.read_text())
        data["errors"] = dict(calls["errors"])
        return data

    mod = types.ModuleType("gather")
    mod.run_all = run_all
    monkeypatch.setitem(sys.modules, "gather", mod)
    return calls


@pytest.fixture
def fake_deliver(monkeypatch):
    """`from deliver import deliver` records the PDF it was handed."""
    calls: dict[str, object] = {"pdfs": [], "result": {"archive": None}}

    def deliver(pdf, settings, *, test=False):
        calls["pdfs"].append(Path(pdf))
        return dict(calls["result"])

    mod = types.ModuleType("deliver")
    mod.deliver = deliver
    monkeypatch.setitem(sys.modules, "deliver", mod)
    monkeypatch.setitem(sys.modules, "deliver.notify", types.ModuleType("deliver.notify"))
    return calls


def _today(result: RunResult) -> str:
    return result.date


def test_dry_run_writes_data_archive_and_pdf(data_dir, fake_gather, fake_deliver):
    result = run(Settings(), dry_run=True)

    assert result.ok and result.error is None
    assert result.pages == 2
    day = _today(result)
    data_json = data_dir / "out" / day / "data.json"
    assert data_json.exists()
    data = json.loads(data_json.read_text())
    assert data["paper"]["volume"] == "Vol. I, No. 1"
    assert data["paper"]["date"].startswith(("Monday", "Tuesday", "Wednesday", "Thursday",
                                             "Friday", "Saturday", "Sunday"))
    assert (data_dir / "out" / day / "paper.pdf").exists()
    assert (data_dir / "archive" / f"{day}.pdf").exists()
    assert result.pdf == data_dir / "archive" / f"{day}.pdf"

    # A dry run delivers nothing and does not count as a printed issue.
    assert fake_deliver["pdfs"] == []
    assert result.delivery == {}
    assert state_module.load_state()["issue"] == 0
    assert state_module.load_state()["last_success"] == ""
    assert state_module.load_state()["last_pages"] == 2


def test_a_real_run_delivers_and_bumps_the_issue(data_dir, fake_gather, fake_deliver):
    result = run(Settings())

    assert result.ok
    assert result.delivery == {"archive": None}
    assert fake_deliver["pdfs"] == [data_dir / "archive" / f"{result.date}.pdf"]
    state = state_module.load_state()
    assert state["issue"] == 1
    assert state["last_success"]
    assert state["last_error"] == ""
    assert state["last_pdf"].endswith(f"{result.date}.pdf")

    # The next issue carries the next number.
    second = run(Settings())
    data = json.loads((data_dir / "out" / second.date / "data.json").read_text())
    assert data["paper"]["volume"] == "Vol. I, No. 2"
    assert state_module.load_state()["issue"] == 2


def test_all_routes_disabled_still_bumps(data_dir, fake_gather, fake_deliver):
    fake_deliver["result"] = {}                     # nothing enabled
    result = run(Settings())
    assert result.ok and result.delivery == {}
    assert state_module.load_state()["issue"] == 1


def test_a_failing_route_is_reported(data_dir, fake_gather, fake_deliver, monkeypatch):
    fake_deliver["result"] = {"print": "printer offline"}
    notified: list[str] = []
    notify_mod = types.ModuleType("deliver.notify")
    notify_mod.notify_failure = lambda settings, message: notified.append(message)
    monkeypatch.setitem(sys.modules, "deliver.notify", notify_mod)

    result = run(Settings())

    assert result.ok is False
    assert "printer offline" in (result.error or "")
    assert notified and "printer offline" in notified[0]
    assert state_module.load_state()["issue"] == 0            # nothing was printed
    assert "printer offline" in state_module.load_state()["last_error"]
    # The paper still exists: the archive copy is written before any route runs.
    assert (data_dir / "archive" / f"{result.date}.pdf").exists()


def test_one_good_route_is_enough(data_dir, fake_gather, fake_deliver):
    fake_deliver["result"] = {"print": "printer offline", "email": None}
    result = run(Settings())
    assert result.ok
    assert state_module.load_state()["issue"] == 1


def test_gather_errors_are_carried_not_raised(data_dir, fake_gather, fake_deliver):
    fake_gather["errors"] = {"weather": "ConnectionError: no route to host"}
    result = run(Settings())
    assert result.ok
    assert result.gather_errors == {"weather": "ConnectionError: no route to host"}
    data = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert "errors" not in data


def test_a_missing_gather_module_still_prints(data_dir, fake_deliver, monkeypatch):
    monkeypatch.setitem(sys.modules, "gather", None)     # import gather -> ImportError
    result = run(Settings(), dry_run=True)
    assert result.ok
    assert "gather" in result.gather_errors
    assert result.pages == 1                              # an empty paper is still a paper


def test_sample_mode_does_not_gather(data_dir, fake_deliver, monkeypatch):
    monkeypatch.setitem(sys.modules, "gather", None)
    result = run(Settings(), dry_run=True, sample=True)
    assert result.ok and result.pages == 2
    assert result.gather_errors == {}


def test_date_replays_an_archived_day(data_dir, fake_gather, fake_deliver):
    first = run(Settings(), dry_run=True)
    day = first.date
    before = json.loads((data_dir / "out" / day / "data.json").read_text())

    replay = run(Settings(), date=day)

    assert replay.ok and replay.date == day
    assert fake_gather["count"] == 1                      # the replay did not gather
    assert fake_deliver["pdfs"] == []                     # and did not deliver
    after = json.loads((data_dir / "out" / day / "data.json").read_text())
    assert after == before                                # including the volume and date
    assert state_module.load_state()["issue"] == 0


def test_replaying_a_day_that_was_never_printed_fails(data_dir, fake_gather, fake_deliver):
    result = run(Settings(), date="1999-01-01")
    assert result.ok is False
    assert "1999-01-01" in (result.error or "")
    assert state_module.load_state()["last_error"]


def test_cli_dry_run_with_sample(data_dir, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "gather", None)
    code = run_module.main(["--dry-run", "--sample"])
    assert code == 0
    assert "page(s)" in capsys.readouterr().out
    assert (data_dir / "logs" / "run.log").exists()


def test_cli_exits_non_zero_on_failure(data_dir, monkeypatch):
    assert run_module.main(["--date", "1999-01-01"]) == 1


# ---------------------------------------------------------------- seen posts
@pytest.fixture
def fake_substack(monkeypatch, fake_gather):
    """Articles carry a `guid`; `gather.substack.mark_seen` records calls."""
    seen: list[list[str]] = []
    orig = sys.modules["gather"].run_all

    def run_all(settings):
        data = orig(settings)
        for i, a in enumerate(data["articles"]):
            a["guid"] = f"post-{i}"
        return data

    sys.modules["gather"].run_all = run_all
    sub = types.ModuleType("gather.substack")
    sub.mark_seen = lambda guids: seen.append(list(guids))
    monkeypatch.setitem(sys.modules, "gather.substack", sub)
    return seen


def test_real_run_marks_posts_seen_and_strips_guid(data_dir, fake_substack, fake_deliver):
    result = run(Settings())
    assert result.ok
    assert fake_substack == [["post-0", "post-1", "post-2", "post-3"]]
    written = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert all("guid" not in a for a in written["articles"])


def test_dry_run_does_not_mark_posts_seen(data_dir, fake_substack, fake_deliver):
    result = run(Settings(), dry_run=True)
    assert result.ok
    assert fake_substack == []


def test_failed_run_does_not_mark_posts_seen(data_dir, fake_substack, fake_deliver):
    s = Settings()
    s.output.email.enabled = True
    s.output.email.to = ["a@b.c"]
    fake_deliver["result"] = {"email": "boom"}
    result = run(s)
    assert not result.ok
    assert fake_substack == []
