"""
More than one paper from one container (`papers.py`): each paper's own
folder, the web page's `/p/<id>/` prefix, the context carried into threads,
each paper's log, and the scheduler making papers set to the same time one
after the other, in print order.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import papers
from app import jobs, scheduler
from app.settings import Settings

AUTH = ("reader", "pw")


@pytest.fixture(autouse=True)
def web_env(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "pw")
    monkeypatch.delenv("TASKS_TOKEN", raising=False)
    monkeypatch.setenv("PAPER_NO_SCHEDULER", "1")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    jobs.reset()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def add_paper(name: str, *, time_: str = "06:00", days=None) -> str:
    pid = papers.add(name)
    with papers.using(pid):
        s = Settings()
        s.look.paper_name = name
        s.output.schedule.time = time_
        s.output.schedule.days = list(range(7)) if days is None else days
        s.save()
    return pid


# ------------------------------------------------------------ the registry
def test_a_container_with_no_registry_makes_the_main_paper(data_dir):
    assert papers.ids() == ["main"]
    assert papers.data_dir() == data_dir
    assert papers.url_prefix() == ""


def test_a_new_paper_gets_a_folder_and_a_place_at_the_end(data_dir):
    a = papers.add("Sam's Gazette")
    b = papers.add("Sam's Gazette")
    assert (a, b) == ("sam-s-gazette", "sam-s-gazette-2")
    assert papers.ids() == ["main", a, b]
    assert papers.data_dir(a) == data_dir / "papers" / a and papers.data_dir(a).is_dir()
    assert papers.url_prefix(a) == f"/p/{a}"
    assert json.loads((data_dir / "papers.json").read_text()) == {"order": ["main", a, b]}


def test_no_paper_takes_the_main_papers_id(data_dir):
    assert papers.add("Main") == "main-2"


def test_papers_move_in_the_print_order(data_dir):
    a, b = papers.add("A"), papers.add("B")
    papers.move(b, -1)
    assert papers.ids() == ["main", b, a]
    papers.move(b, -1)
    assert papers.ids() == [b, "main", a]
    papers.move(b, -5)                              # clamps at the top
    assert papers.ids() == [b, "main", a]


def test_removing_a_paper_keeps_its_folder(data_dir):
    a = papers.add("A")
    (papers.data_dir(a) / "archive").mkdir()
    (papers.data_dir(a) / "archive" / "2026-09-18.pdf").write_bytes(b"%PDF")
    papers.remove(a)
    assert papers.ids() == ["main"]
    kept = [p for p in (data_dir / "papers").iterdir() if p.name.startswith(f".removed-{a}-")]
    assert len(kept) == 1 and (kept[0] / "archive" / "2026-09-18.pdf").exists()
    with pytest.raises(ValueError):
        papers.remove("main")


def test_a_broken_registry_still_makes_the_main_paper(data_dir):
    (data_dir / "papers.json").write_text("{not json")
    assert papers.ids() == ["main"]


# -------------------------------------------------------------- isolation
def test_each_paper_keeps_its_own_settings_state_and_lists(data_dir):
    import state
    from gather import lists

    a = add_paper("Second")
    main = Settings.load()
    main.look.paper_name = "First"
    main.save()
    state.update_state(issue=41)
    lists.write_list("tasks", ["main's"])

    with papers.using(a):
        assert Settings.load().look.paper_name == "Second"
        assert state.load_state()["issue"] == 0
        lists.write_list("tasks", ["second's"])
        assert Settings.path() == data_dir / "papers" / a / "settings.json"

    assert Settings.load().look.paper_name == "First"
    assert state.load_state()["issue"] == 41
    assert json.loads((data_dir / "lists" / "tasks.json").read_text())["items"] == ["main's"]
    assert json.loads((data_dir / "papers" / a / "lists" / "tasks.json").read_text())["items"] == ["second's"]


def test_a_thread_started_for_a_paper_works_for_it(data_dir):
    from gather import submit

    a = papers.add("A")
    with papers.using(a):
        future = submit("x", papers.current)
        seen = []
        papers.start_thread(lambda: seen.append(papers.current()), name="t").join()
    assert future.result(timeout=5) == a
    assert seen == [a]
    assert papers.current() == "main"


def test_a_papers_log_file_carries_only_its_own_lines(data_dir):
    a = papers.add("A")
    root = logging.getLogger("paper-filter-test")
    root.setLevel(logging.INFO)
    lines: dict[str, list[str]] = {"main": [], a: []}
    handlers = []
    for pid in lines:
        h = logging.Handler()
        h.emit = lambda record, pid=pid: lines[pid].append(record.getMessage())
        h.addFilter(papers.PaperFilter(pid))
        root.addHandler(h)
        handlers.append(h)
    try:
        root.info("for main")
        with papers.using(a):
            root.info("for a")
    finally:
        for h in handlers:
            root.removeHandler(h)
    assert lines == {"main": ["for main"], a: ["for a"]}


# ---------------------------------------------------------------- the page
def test_adding_a_paper_from_the_page(client, data_dir):
    main = Settings.load()
    main.output.print.enabled = True
    main.output.print.printer_host = "10.0.0.9"
    main.output.schedule.time = "05:45"
    main.sources.substacks = []
    main.save()

    r = client.post("/papers/add", data={"name": "The Ledger", "copy_output": "on"},
                    auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/the-ledger/sources"
    with papers.using("the-ledger"):
        new = Settings.load()
    assert new.look.paper_name == "The Ledger"
    assert new.output.print.printer_host == "10.0.0.9" and new.output.print.enabled
    assert new.output.schedule.time == "05:45"
    assert new.sources.calendars == [] and new.sources.substacks == []


def test_each_papers_tabs_edit_that_paper(client, data_dir):
    a = add_paper("Second")
    body = client.get(f"/p/{a}/look", auth=AUTH).text
    assert "<h1>Second</h1>" in body
    # Every form, tab and switcher link on the page stays on this paper.
    assert f'action="/p/{a}/look"' in body
    assert f'href="/p/{a}/sources"' in body
    assert 'href="/look"' in body                   # the switcher's way back

    r = client.post(f"/p/{a}/look", data={"paper_name": "Renamed"}, auth=AUTH, follow_redirects=False)
    assert r.headers["location"] == f"/p/{a}/look?saved=1"
    with papers.using(a):
        assert Settings.load().look.paper_name == "Renamed"
    assert Settings.load().look.paper_name == "Personal Paper"


def test_the_switcher_shows_only_with_more_than_one_paper(client, data_dir):
    assert 'class="papers"' not in client.get("/look", auth=AUTH).text
    add_paper("Second")
    body = client.get("/output", auth=AUTH).text
    assert 'class="papers"' in body and 'href="/p/second/output"' in body


def test_an_unknown_paper_is_a_404(client):
    assert client.get("/p/nobody/look", auth=AUTH).status_code == 404
    assert client.get("/p/main/look", auth=AUTH).status_code == 404


def test_the_bare_prefix_is_the_papers_front_door(client, data_dir):
    a = add_paper("Second")
    r = client.get(f"/p/{a}", auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{a}/preview"


def test_another_papers_list_is_posted_under_its_prefix(client, data_dir):
    a = add_paper("Second")
    # Open like the main paper's endpoint: no browser password, the slug as token.
    r = client.post(f"/p/{a}/lists/tasks", json={"items": ["milk"]},
                    headers={"Authorization": "Bearer tasks"})
    assert r.status_code == 200, r.text
    assert json.loads((data_dir / "papers" / a / "lists" / "tasks.json").read_text())["items"] == ["milk"]
    assert not (data_dir / "lists" / "tasks.json").exists()
    # And the Sources tab tells the phone that address.
    assert f"/p/{a}/lists/tasks" in client.get(f"/p/{a}/sources", auth=AUTH).text


def test_a_run_started_on_a_papers_page_makes_that_paper(client, data_dir, monkeypatch):
    import run

    a = add_paper("Second")
    made = []

    def fake_run(settings, **kw):
        made.append((papers.current(), settings.look.paper_name))
        return run.RunResult(date="2026-09-23", ok=True, pages=2)

    monkeypatch.setattr(run, "run", fake_run)
    job = client.post(f"/p/{a}/run?dry=1", auth=AUTH).json()["job"]
    deadline = time.time() + 10
    while jobs.get(job).status == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert made == [(a, "Second")]


def test_papers_are_reordered_and_removed_from_the_page(client, data_dir):
    a, b = add_paper("A"), add_paper("B")
    client.post(f"/papers/{b}/move", data={"step": "-1"}, auth=AUTH)
    assert papers.ids() == ["main", b, a]

    # Not without the box ticked.
    client.post(f"/p/{a}/papers/{a}/remove", data={}, auth=AUTH)
    assert a in papers.ids()
    r = client.post(f"/p/{a}/papers/{a}/remove", data={"confirm": "on"}, auth=AUTH, follow_redirects=False)
    assert r.headers["location"] == "/papers?saved=1"
    assert papers.ids() == ["main", b]
    # The main paper is never removed.
    client.post("/papers/main/remove", data={"confirm": "on"}, auth=AUTH)
    assert papers.ids() == ["main", b]


def test_the_papers_tab_lists_them_in_print_order(client, data_dir):
    add_paper("Zed", time_="07:30", days=[0, 1, 2, 3, 4])
    body = client.get("/papers", auth=AUTH).text
    assert body.index("Personal Paper") < body.index("Zed")
    assert "07:30, Mon–Fri" in body


# -------------------------------------------------------------- scheduler
def test_papers_due_at_the_same_time_are_made_one_after_another(data_dir, monkeypatch):
    import run

    late = add_paper("Late", time_="07:00")
    second = add_paper("Second")
    weekends = add_paper("Weekends", days=[5, 6])
    papers.move(second, -1)                         # prints before Late
    made = []
    monkeypatch.setattr(run, "run", lambda settings, **kw: made.append(papers.current())
                        or run.RunResult(date="x", ok=True))
    # 2026-09-23 is a Wednesday.
    monkeypatch.setattr(scheduler, "_fired_on", lambda h, m, now: 2)

    scheduler.run_due(6, 0)
    assert made == ["main", second]
    assert weekends not in made and late not in made


def test_one_papers_failure_does_not_stop_the_next(data_dir, monkeypatch):
    import run

    second = add_paper("Second")
    made = []

    def flaky(settings, **kw):
        made.append(papers.current())
        if papers.current() == "main":
            raise RuntimeError("boom")
        return run.RunResult(date="x", ok=True)

    monkeypatch.setattr(run, "run", flaky)
    monkeypatch.setattr(scheduler, "_fired_on", lambda h, m, now: 2)
    scheduler.run_due(6, 0)
    assert made == ["main", second]


def test_the_fire_counts_for_the_day_it_was_due():
    # A 23:50 run that fired late, at 00:05, is still Monday's.
    assert scheduler._fired_on(23, 50, datetime(2026, 9, 22, 0, 5)) == 0
    assert scheduler._fired_on(6, 0, datetime(2026, 9, 23, 6, 1)) == 2


def test_one_job_per_print_time(data_dir, monkeypatch):
    monkeypatch.delenv("PAPER_NO_SCHEDULER")
    add_paper("Same")
    add_paper("Later", time_="07:15", days=[5])
    try:
        scheduler.start()
        ids = sorted(j.id for j in scheduler._scheduler.get_jobs())
        assert ids == ["daily-paper-0600", "daily-paper-0715"]
        later = scheduler._scheduler.get_job("daily-paper-0715")
        assert "sat" in str(later.trigger) and "mon" not in str(later.trigger)

        with papers.using("later"):
            s = Settings.load()
            s.output.schedule.days = []
            s.save()
            assert scheduler.next_run_time() is None
        scheduler.reschedule()
        assert [j.id for j in scheduler._scheduler.get_jobs()] == ["daily-paper-0600"]
    finally:
        scheduler.shutdown()
