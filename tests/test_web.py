"""
The web layer: auth, the four tabs saving their own section, the Check
endpoints never crashing the page, `POST /tasks`, the archive route's name
check, a real sample preview, and the scheduler's trigger.

Nothing here touches the network: every gatherer call is monkeypatched but
the preview, which renders the sample issue in the bundled Chromium.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler
from app.settings import CalendarSource, Schedule, Settings, SubstackSource

AUTH = ("molly", "pw")


@pytest.fixture(autouse=True)
def web_env(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "pw")
    monkeypatch.setenv("TASKS_TOKEN", "tok")
    monkeypatch.setenv("MOLLY_NO_SCHEDULER", "1")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    jobs.reset()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def wait_for(job_id: str, timeout: float = 120.0) -> jobs.Job:
    """Poll the job table (not the HTTP endpoint) until the job finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get(job_id)
        assert job is not None
        if job.status != "running":
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


# ------------------------------------------------------------------- auth
def test_look_needs_a_password(client):
    response = client.get("/look")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("www-authenticate", "")


def test_look_with_auth(client):
    response = client.get("/look", auth=AUTH)
    assert response.status_code == 200
    assert "The Molly Ledger" in response.text


def test_wrong_password_is_refused(client):
    assert client.get("/look", auth=("molly", "nope")).status_code == 401


def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_root_redirects_to_preview(client):
    response = client.get("/", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/preview"


# ------------------------------------------------------------- saving tabs
def test_saving_look_keeps_the_other_sections(client):
    settings = Settings()
    settings.output.email.to = ["her@example.com"]
    settings.output.schedule.time = "05:45"
    settings.sources.substacks = [SubstackSource(name="oneuseful")]
    settings.save()

    response = client.post(
        "/look",
        auth=AUTH,
        data={
            "paper_name": "The Evening Ledger",
            "imprint": "Printed at home",
            "price": "Free",
            "ear_initials": "M. L.",
            "ear_lines": "One line\nAnother line\n",
            "masthead_font": "UnifrakturCook",
            "headline_font": "Playfair Display",
            "body_font": "EB Garamond",
            "body_size_pt": "10.5",
            "lead_body_height_in": "3.1",
            "show_todo": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/look?saved=1"

    saved = Settings.load()
    assert saved.look.paper_name == "The Evening Ledger"
    assert saved.look.body_size_pt == 10.5
    assert saved.look.lead_body_height_in == 3.1
    assert saved.look.masthead_font == "UnifrakturCook"
    assert saved.look.ear.lines == ["One line", "Another line"]
    assert saved.look.show_todo is True
    assert saved.look.show_hourly is False        # unchecked boxes are off
    # The other tabs' sections are untouched.
    assert saved.output.email.to == ["her@example.com"]
    assert saved.output.schedule.time == "05:45"
    assert [s.name for s in saved.sources.substacks] == ["oneuseful"]


def test_look_ignores_an_unknown_font_and_clamps_the_size(client):
    client.post("/look", auth=AUTH, data={"paper_name": "X", "body_font": "Comic Sans", "body_size_pt": "99"})
    look = Settings.load().look
    assert look.body_font == "PT Serif"
    assert look.body_size_pt == 11.0


def test_saving_output_persists_and_reschedules(client, monkeypatch):
    calls: list[Settings] = []
    monkeypatch.setattr("app.main.scheduler.reschedule", lambda s: calls.append(s))
    Settings().save()

    response = client.post(
        "/output",
        auth=AUTH,
        data={
            "print_enabled": "on",
            "printer_host": "192.168.1.40",
            "printer_name": "Brother HL-L2460DW",
            "duplex": "on",
            "email_enabled": "on",
            "email_to": "her@example.com, him@example.com",
            "email_subject": "The Molly Ledger, {date}",
            "schedule_time": "07:15",
            "day_0": "on",
            "day_2": "on",
            "notify": "email",
            "notify_email": "her@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    out = Settings.load().output
    assert out.schedule.time == "07:15"
    assert out.schedule.days == [0, 2]
    assert out.print.enabled and out.print.printer_host == "192.168.1.40"
    assert out.email.to == ["her@example.com", "him@example.com"]
    assert out.notify == "email"
    assert len(calls) == 1


def test_saving_sources_keeps_masked_urls_and_adds_rows(client):
    settings = Settings()
    settings.sources.calendars = [CalendarSource(url="https://example.com/secret/basic.ics", name="Home")]
    settings.save()

    client.post(
        "/sources",
        auth=AUTH,
        data={
            "cal_index": "0",
            "cal_url_0": "",                      # masked on the page: keep what is stored
            "cal_name_0": "Home",
            "cal_new_url": "https://example.com/other/basic.ics",
            "cal_new_name": "",
            "sub_new_name": "oneuseful",
            "sub_new_paid": "on",
            "sub_new_order": "1",
            "lat": "37.87",
            "lon": "-122.27",
            "tasks_max_age_hours": "12",
        },
    )
    sources = Settings.load().sources
    assert [c.url for c in sources.calendars] == [
        "https://example.com/secret/basic.ics",
        "https://example.com/other/basic.ics",
    ]
    assert [(s.name, s.paid) for s in sources.substacks] == [("oneuseful", True)]
    assert sources.weather.lat == 37.87
    assert sources.tasks_max_age_hours == 12


def test_sources_page_shows_masked_url_and_shortcut_instructions(client):
    settings = Settings()
    settings.sources.calendars = [CalendarSource(url="https://example.com/abc123456", name="Home")]
    settings.save()

    body = client.get("/sources", auth=AUTH).text
    assert "abc123456" not in body
    assert "••••123456" in body
    assert "/tasks" in body and "Bearer" in body
    assert "TASKS_TOKEN" in body


# --------------------------------------------------------- check endpoints
def test_calendar_check_reports_errors(client, monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr("gather.calendar.check", boom)
    body = client.post("/sources/calendar/check", auth=AUTH, json={"url": "https://x/basic.ics"}).json()
    assert body["ok"] is False
    assert "404 Not Found" in body["error"]


def test_calendar_check_ok(client, monkeypatch):
    monkeypatch.setattr("gather.calendar.check", lambda url, **k: {"name": "Home", "events_today": 3})
    body = client.post("/sources/calendar/check", auth=AUTH, json={"url": "https://x/basic.ics"}).json()
    assert body == {"ok": True, "result": {"name": "Home", "events_today": 3}}


def test_weather_check_reports_errors(client, monkeypatch):
    def boom(lat, lon, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr("gather.weather.check", boom)
    body = client.post("/sources/weather/check", auth=AUTH, json={"lat": 37.8, "lon": -122.3}).json()
    assert body["ok"] is False and "no route to host" in body["error"]


def test_weather_check_ok(client, monkeypatch):
    monkeypatch.setattr(
        "gather.weather.check",
        lambda lat, lon, **k: {"summary": "Sunny all day", "high": "72", "low": "54"},
    )
    body = client.post("/sources/weather/check", auth=AUTH, json={"lat": "37.8", "lon": "-122.3"}).json()
    assert body["ok"] is True and body["result"]["summary"] == "Sunny all day"


def test_substack_check_reports_errors(client, monkeypatch):
    def boom(name):
        raise ValueError("no such feed")

    monkeypatch.setattr("gather.substack.check", boom)
    body = client.post("/sources/substack/check", auth=AUTH, json={"name": "nope"}).json()
    assert body["ok"] is False and "no such feed" in body["error"]


def test_printer_endpoints_never_crash(client, monkeypatch):
    monkeypatch.setattr("deliver.discover", lambda *a, **k: [])
    assert client.post("/output/printer/discover", auth=AUTH).json() == {"ok": True, "result": []}

    def boom(host):
        raise TimeoutError("printer did not answer")

    monkeypatch.setattr("deliver.test_printer", boom)
    body = client.post("/output/printer/test", auth=AUTH, json={"host": "192.168.1.40"}).json()
    assert body["ok"] is False and "printer did not answer" in body["error"]


def test_email_test_reports_errors(client, monkeypatch):
    def boom(to, smtp):
        raise RuntimeError("SMTP is not configured")

    monkeypatch.setattr("deliver.send_test", boom)
    body = client.post("/output/email/test", auth=AUTH, json={"to": "her@example.com"}).json()
    assert body["ok"] is False and "SMTP is not configured" in body["error"]


# -------------------------------------------------------------- POST /tasks
def test_tasks_rejects_a_bad_token(client):
    response = client.post("/tasks", json={"tasks": ["a"]}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    response = client.post("/tasks", json={"tasks": ["a"]})
    assert response.status_code == 401


def test_tasks_without_a_configured_token(client, monkeypatch):
    monkeypatch.delenv("TASKS_TOKEN")
    response = client.post("/tasks", json={"tasks": ["a"]}, headers={"Authorization": "Bearer tok"})
    assert response.status_code == 503
    assert "TASKS_TOKEN" in response.json()["detail"]


def test_tasks_writes_json_body(client, data_dir):
    from gather import tasks as tasks_gather

    response = client.post(
        "/tasks",
        json={"tasks": ["Call the school", "Buy stamps"]},
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "count": 2}
    stored = json.loads((data_dir / "tasks.json").read_text())
    assert stored["tasks"] == ["Call the school", "Buy stamps"]
    assert tasks_gather.status()["count"] == 2


def test_tasks_accepts_a_plain_text_body(client):
    from gather import tasks as tasks_gather

    response = client.post(
        "/tasks",
        content="Call the school\nBuy stamps\nWater the fig\n",
        headers={"Authorization": "Bearer tok", "Content-Type": "text/plain"},
    )
    assert response.json() == {"ok": True, "count": 3}
    assert tasks_gather.status()["count"] == 3


# ------------------------------------------------------------ archive, log
def test_archive_rejects_odd_names(client, data_dir):
    archive = data_dir / "archive"
    archive.mkdir()
    (archive / "2026-09-16.pdf").write_bytes(b"%PDF-1.4 fake")
    (archive / "secret.txt").write_text("nope")

    assert client.get("/archive/2026-09-16.pdf", auth=AUTH).status_code == 200
    assert client.get("/archive/secret.txt", auth=AUTH).status_code == 404
    assert client.get("/archive/2026-09-16.pdf.bak", auth=AUTH).status_code == 404
    assert client.get("/archive/../x", auth=AUTH).status_code == 404
    assert client.get("/archive/%2e%2e%2fstate.json", auth=AUTH).status_code == 404


def test_log_shows_the_tail(client, data_dir):
    logs = data_dir / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("\n".join(f"line {i}" for i in range(300)))
    body = client.get("/log", auth=AUTH).text
    assert "line 299" in body
    assert "line 99" not in body


# ------------------------------------------------------------------ preview
def test_preview_renders_the_sample_issue(client, data_dir):
    response = client.post("/preview?source=sample", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].split("job=")[1]

    job = wait_for(job_id)
    assert job.status == "done", job.error
    assert job.result["pages"] == 2
    assert len(job.result["pngs"]) == 2

    status = client.get(f"/jobs/{job_id}", auth=AUTH).json()
    assert status["status"] == "done"

    for url in job.result["pngs"]:
        png = client.get(url, auth=AUTH)
        assert png.status_code == 200
        assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(job.result["html"], auth=AUTH).status_code == 200

    page = client.get(f"/preview?job={job_id}", auth=AUTH).text
    assert job_id in page


def test_preview_files_are_name_checked(client):
    assert client.get("/preview/zzz/page-1.png", auth=AUTH).status_code == 404
    assert client.get("/preview/abc123/paper.pdf", auth=AUTH).status_code == 404


def test_jobs_endpoint_404s_for_unknown_jobs(client):
    assert client.get("/jobs/abc123", auth=AUTH).status_code == 404


def test_preview_keeps_only_three_job_directories(client):
    root = jobs.preview_root()
    for i in range(5):
        d = root / f"{i:012x}"
        d.mkdir(parents=True)
        (d / "paper.html").write_text("x")
    jobs.prune_previews()
    assert len(list(root.iterdir())) == 3


def test_run_now_starts_a_job(client, monkeypatch):
    import run as run_module

    calls: list[bool] = []

    def fake_run(settings, *, dry_run=False, **kwargs):
        calls.append(dry_run)
        return run_module.RunResult(date="2026-09-16", pages=2, ok=True, delivery={"print": None})

    monkeypatch.setattr(run_module, "run", fake_run)
    body = client.post("/run?dry=1", auth=AUTH).json()
    assert body["ok"] is True

    job = wait_for(body["job"], timeout=20)
    assert job.status == "done", job.error
    assert job.result["pages"] == 2
    assert job.result["delivery"] == {"print": "ok"}
    assert calls == [True]


# ---------------------------------------------------------------- scheduler
def test_build_trigger_fires_at_the_configured_time():
    trigger = scheduler.build_trigger(Schedule(time="06:00", days=[0, 1, 2, 3, 4, 5, 6]))
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 16, 12, 0, tzinfo=tz)
    when = trigger.get_next_fire_time(None, now)
    assert (when.hour, when.minute) == (6, 0)
    assert when.date() == datetime(2026, 9, 17, tzinfo=tz).date()


def test_build_trigger_weekends_only():
    trigger = scheduler.build_trigger(Schedule(time="06:00", days=[5, 6]))
    tz = ZoneInfo("America/Los_Angeles")
    when = trigger.get_next_fire_time(None, datetime(2026, 9, 16, 12, 0, tzinfo=tz))  # a Wednesday
    assert when.weekday() == 5                                   # Saturday
    assert when.date() == datetime(2026, 9, 19, tzinfo=tz).date()
    later = trigger.get_next_fire_time(when, when)
    assert later.weekday() == 6                                  # then Sunday


def test_build_trigger_without_days_is_off():
    assert scheduler.build_trigger(Schedule(time="06:00", days=[])) is None


def test_unreadable_schedule_time_falls_back():
    assert scheduler.parse_time("nonsense") == (6, 0)
    assert scheduler.parse_time("07:15") == (7, 15)


def test_app_refuses_to_start_without_a_password(monkeypatch):
    """Importable for tests, but the lifespan will not come up unset."""
    monkeypatch.delenv("WEB_PASSWORD")
    from app.main import app

    with pytest.raises(RuntimeError, match="WEB_PASSWORD"):
        with TestClient(app):
            pass
