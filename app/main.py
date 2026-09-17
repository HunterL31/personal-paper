"""
Personal Paper's web page: four tabs of settings, a preview, the task
endpoint the iPhone Shortcut posts to, and the in-process scheduler.

    DATA_DIR=/data WEB_PASSWORD=... uvicorn app.main:app --host 0.0.0.0 --port 8080

Everything the reader edits goes through `Settings.load()` -> mutate one
section -> `.save()`, so two tabs open at once cannot erase each other.
Credentials are never shown, only reported as set or not set.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:  # so `state`, `run` and `render` import cleanly
    sys.path.insert(0, str(REPO))

from app import auth, jobs, scheduler  # noqa: E402
from app.settings import (  # noqa: E402
    FONT_CHOICES_BODY,
    FONT_CHOICES_HEAD,
    FONT_CHOICES_MASTHEAD,
    CalendarSource,
    Env,
    Settings,
    SubstackSource,
)
from state import data_dir, load_state  # noqa: E402

log = logging.getLogger(__name__)

SAMPLE_DATA = REPO / "render" / "sample_data.json"
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
ARCHIVE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.pdf$")
PREVIEW_FILE = re.compile(r"^(page-\d+\.png|paper\.html)$")
JOB_ID = re.compile(r"^[0-9a-f]{6,32}$")
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
#: body_size_pt choices: 8.0 to 11.0 in half points.
BODY_SIZES = [8.0 + 0.5 * i for i in range(7)]
TABS = [("look", "Look"), ("sources", "Sources"), ("output", "Output"), ("preview", "Preview")]
#: Which container variables each tab shows as "set in container / not set".
ENV_ON_SOURCES = [*Env.IMAP, Env.TASKS_TOKEN, Env.NYT_S, Env.TZ]
ENV_ON_OUTPUT = [*Env.SMTP]


# --------------------------------------------------------------- lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not auth.enabled():
        logging.getLogger(__name__).warning(
            "%s is not set: the web page is open to anyone on the network",
            Env.WEB_PASSWORD,
        )
    scheduler.start(Settings.load())
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Personal Paper", lifespan=lifespan, docs_url=None, redoc_url=None)
app.middleware("http")(auth.middleware)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ----------------------------------------------------------------- helpers
def _stamp(value: str) -> str:
    """An ISO timestamp from state.json as the strip shows it."""
    try:
        return f"{datetime.fromisoformat(value):%Y-%m-%d %H:%M}"
    except (TypeError, ValueError):
        return value or ""


def _printer_check(st: dict[str, Any]) -> dict[str, Any] | None:
    """The last printer check, for the strip; None when there is no printer."""
    check = st.get("printer_check")
    if not isinstance(check, dict) or not check.get("summary"):
        return None
    route = Settings.load().output.print
    if not (route.enabled or route.printer_host):
        return None
    try:
        stamp = f"{datetime.fromisoformat(str(check.get('when'))):%H:%M}"
    except (TypeError, ValueError):
        stamp = ""
    return {"ok": bool(check.get("ok")), "summary": str(check["summary"]), "when": stamp}


def _crossword_check(st: dict[str, Any]) -> dict[str, Any] | None:
    """The last crossword check, for the strip; None when it is switched off."""
    check = st.get("crossword_check")
    if not isinstance(check, dict) or not check.get("summary"):
        return None
    if not Settings.load().sources.crossword.enabled:
        return None
    try:
        stamp = f"{datetime.fromisoformat(str(check.get('when'))):%H:%M}"
    except (TypeError, ValueError):
        stamp = ""
    return {"ok": bool(check.get("ok")), "summary": str(check["summary"]), "when": stamp}


def _status() -> dict[str, Any]:
    """The strip at the top of every tab."""
    st = load_state()
    pdf_name = Path(st.get("last_pdf") or "").name
    when = scheduler.next_run_time()
    return {
        "printer_check": _printer_check(st),
        "crossword_check": _crossword_check(st),
        "last_run": _stamp(st.get("last_run") or ""),
        "last_success": _stamp(st.get("last_success") or ""),
        "last_error": st.get("last_error") or "",
        "last_pages": st.get("last_pages") or 0,
        "issue": st.get("issue") or 0,
        "archive_url": f"/archive/{pdf_name}" if ARCHIVE_NAME.match(pdf_name) else "",
        "archive_name": pdf_name,
        "next_run": f"{when:%Y-%m-%d %H:%M %Z}".strip() if when else "",
        "login_off": not auth.enabled(),
        "scheduler_on": when is not None,
    }


def page(request: Request, tab: str, template: str, **extra: Any) -> Response:
    context = {
        "request": request,
        "tab": tab,
        "tabs": TABS,
        # The paper's name is the reader's, set on the Look tab.
        "paper_name": Settings.load().look.paper_name,
        "status": _status(),
        "saved": request.query_params.get("saved") == "1",
    }
    context.update(extra)
    return TEMPLATES.TemplateResponse(request, template, context)


def saved(path: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?saved=1", status_code=303)


def env_rows(names: list[str]) -> list[dict[str, Any]]:
    status = Env.status()
    return [{"name": n, "set": bool(status.get(n))} for n in names]


async def check_json(work: Callable[[], Any]) -> JSONResponse:
    """Run a blocking gatherer/printer call off the loop; never raise."""
    try:
        result = await run_in_threadpool(work)
    except Exception as exc:  # noqa: BLE001 - the page shows the text, nothing crashes
        log.warning("check failed: %s", exc, exc_info=True)
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return JSONResponse({"ok": True, "result": jsonable_encoder(result)})


async def body_dict(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def form_text(form: FormData, name: str, fallback: str = "") -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else fallback


def form_flag(form: FormData, name: str) -> bool:
    return form.get(name) is not None


_NEW_KEY = re.compile(r"^n\d{1,4}$")


def _new_row_keys(form: FormData, field: str) -> list[str]:
    """Keys of the "add" rows the page submitted (n0, n1, ...), in order."""
    seen: list[str] = []
    for raw in form.getlist(field):
        if isinstance(raw, str) and _NEW_KEY.match(raw) and raw not in seen:
            seen.append(raw)
    return seen


def form_number(form: FormData, name: str, fallback: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(form_text(form, name))))
    except (TypeError, ValueError):
        return fallback


def one_of(value: str, choices: list[str], fallback: str) -> str:
    return value if value in choices else fallback


def mask(url: str) -> str:
    """A saved iCal URL as the page shows it: dots and the last six characters."""
    url = (url or "").strip()
    return f"••••{url[-6:]}" if len(url) > 6 else ("••••" if url else "")


# ------------------------------------------------------------------- pages
@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/preview", status_code=303)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


# --------------------------------------------------------------- Look tab
@app.get("/look")
def look_get(request: Request) -> Response:
    return page(
        request, "look", "look.html",
        look=Settings.load().look,
        fonts_masthead=FONT_CHOICES_MASTHEAD,
        fonts_head=FONT_CHOICES_HEAD,
        fonts_body=FONT_CHOICES_BODY,
        body_sizes=BODY_SIZES,
    )


@app.post("/look")
async def look_post(request: Request) -> RedirectResponse:
    form = await request.form()
    settings = Settings.load()
    look = settings.look

    look.paper_name = form_text(form, "paper_name") or look.paper_name
    look.imprint = form_text(form, "imprint")
    look.price = form_text(form, "price")
    look.ear.initials = form_text(form, "ear_initials")
    look.ear.lines = [ln.strip() for ln in form_text(form, "ear_lines").splitlines() if ln.strip()]
    look.masthead_font = one_of(form_text(form, "masthead_font"), FONT_CHOICES_MASTHEAD, look.masthead_font)
    look.headline_font = one_of(form_text(form, "headline_font"), FONT_CHOICES_HEAD, look.headline_font)
    look.body_font = one_of(form_text(form, "body_font"), FONT_CHOICES_BODY, look.body_font)
    look.body_size_pt = form_number(form, "body_size_pt", look.body_size_pt, 8.0, 11.0)
    look.lead_body_height_in = form_number(form, "lead_body_height_in", look.lead_body_height_in, 1.5, 5.0)
    look.show_todo = form_flag(form, "show_todo")
    look.show_hourly = form_flag(form, "show_hourly")
    look.show_notes = form_flag(form, "show_notes")
    look.justify = form_flag(form, "justify")

    settings.save()
    return saved("/look")


# ------------------------------------------------------------ Sources tab
@app.get("/sources")
def sources_get(request: Request) -> Response:
    settings = Settings.load()
    from gather import tasks as tasks_gather

    calendars = [
        {"index": i, "masked": mask(c.url), "name": c.name}
        for i, c in enumerate(settings.sources.calendars)
    ]
    substacks = [
        {"index": i, "name": s.name, "paid": s.paid} for i, s in enumerate(settings.sources.substacks)
    ]
    host = request.headers.get("host") or "unraid.local:8080"
    return page(
        request, "sources", "sources.html",
        sources=settings.sources,
        calendars=calendars,
        substacks=substacks,
        tasks_status=tasks_gather.status(),
        tasks_host=host,
        tasks_token_set=Env.is_set(Env.TASKS_TOKEN),
        crossword=settings.sources.crossword,
        days=list(enumerate(DAY_LABELS)),
        nyt_cookie_set=Env.is_set(Env.NYT_S),
        env_rows=env_rows(ENV_ON_SOURCES),
    )


@app.post("/sources")
async def sources_post(request: Request) -> RedirectResponse:
    form = await request.form()
    settings = Settings.load()
    old_calendars = list(settings.sources.calendars)
    old_substacks = list(settings.sources.substacks)

    calendars: list[CalendarSource] = []
    for raw in form.getlist("cal_index"):
        i = int(raw)
        if form_flag(form, f"cal_remove_{i}") or i >= len(old_calendars):
            continue
        existing = old_calendars[i]
        url = form_text(form, f"cal_url_{i}")
        name = form_text(form, f"cal_name_{i}")
        # An empty box means "keep the URL you already have" — it is masked
        # on the page, so there is nothing to type back.
        calendars.append(CalendarSource(url=url or existing.url, name=name))
    for key in _new_row_keys(form, "cal_new"):
        new_url = form_text(form, f"cal_new_url_{key}")
        if new_url:
            calendars.append(CalendarSource(url=new_url, name=form_text(form, f"cal_new_name_{key}")))

    rows: list[tuple[float, SubstackSource]] = []
    for raw in form.getlist("sub_index"):
        i = int(raw)
        if form_flag(form, f"sub_remove_{i}") or i >= len(old_substacks):
            continue
        name = form_text(form, f"sub_name_{i}") or old_substacks[i].name
        order = form_number(form, f"sub_order_{i}", float(i + 1), 1.0, 99.0)
        rows.append((order, SubstackSource(name=name, paid=form_flag(form, f"sub_paid_{i}"))))
    for key in _new_row_keys(form, "sub_new"):
        new_name = form_text(form, f"sub_new_name_{key}")
        if new_name:
            order = form_number(form, f"sub_new_order_{key}", float(len(rows) + 1), 1.0, 99.0)
            rows.append((order, SubstackSource(name=new_name, paid=form_flag(form, f"sub_new_paid_{key}"))))

    settings.sources.calendars = calendars
    settings.sources.substacks = [s for _, s in sorted(rows, key=lambda r: r[0])]
    settings.sources.weather.lat = form_number(form, "lat", settings.sources.weather.lat, -90.0, 90.0)
    settings.sources.weather.lon = form_number(form, "lon", settings.sources.weather.lon, -180.0, 180.0)
    settings.sources.tasks_max_age_hours = int(
        form_number(form, "tasks_max_age_hours", float(settings.sources.tasks_max_age_hours), 1.0, 168.0)
    )
    settings.sources.crossword.enabled = form_flag(form, "crossword_enabled")
    settings.sources.crossword.days = [d for d in range(7) if form_flag(form, f"crossword_day_{d}")]
    settings.save()
    return saved("/sources")


@app.post("/sources/calendar/check")
async def calendar_check(request: Request) -> JSONResponse:
    payload = await body_dict(request)
    url = str(payload.get("url") or "").strip()
    index = payload.get("index")
    if not url and isinstance(index, int):
        calendars = Settings.load().sources.calendars
        if 0 <= index < len(calendars):
            url = calendars[index].url
    if not url:
        return JSONResponse({"ok": False, "error": "No calendar URL to check"})

    from gather import calendar as calendar_gather

    return await check_json(lambda: calendar_gather.check(url))


@app.post("/sources/substack/check")
async def substack_check(request: Request) -> JSONResponse:
    name = str((await body_dict(request)).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "No publication to check"})

    from gather import substack as substack_gather

    return await check_json(lambda: substack_gather.check(name))


@app.post("/sources/crossword/check")
async def crossword_check() -> JSONResponse:
    from gather import crossword as crossword_gather

    return await check_json(crossword_gather.check)


@app.post("/sources/weather/check")
async def weather_check(request: Request) -> JSONResponse:
    payload = await body_dict(request)
    try:
        lat, lon = float(payload.get("lat")), float(payload.get("lon"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Latitude and longitude must be numbers"})

    from gather import weather as weather_gather

    return await check_json(lambda: weather_gather.check(lat, lon))


# ------------------------------------------------------------- Output tab
@app.get("/output")
def output_get(request: Request) -> Response:
    settings = Settings.load()
    return page(
        request, "output", "output.html",
        output=settings.output,
        email_to=", ".join(settings.output.email.to),
        days=list(enumerate(DAY_LABELS)),
        env_rows=env_rows(ENV_ON_OUTPUT),
        smtp_user=Env.get("SMTP_USER") or "",
    )


@app.post("/output")
async def output_post(request: Request) -> RedirectResponse:
    form = await request.form()
    settings = Settings.load()
    out = settings.output

    out.print.enabled = form_flag(form, "print_enabled")
    out.print.printer_host = form_text(form, "printer_host")
    out.print.printer_name = form_text(form, "printer_name")
    out.print.duplex = form_flag(form, "duplex")

    out.email.enabled = form_flag(form, "email_enabled")
    out.email.to = [a.strip() for a in form_text(form, "email_to").split(",") if a.strip()]
    out.email.subject = form_text(form, "email_subject") or out.email.subject

    hour, minute = scheduler.parse_time(form_text(form, "schedule_time", out.schedule.time))
    out.schedule.time = f"{hour:02d}:{minute:02d}"
    out.schedule.days = [d for d in range(7) if form_flag(form, f"day_{d}")]

    notify = form_text(form, "notify")
    out.notify = notify if notify in ("none", "email", "unraid") else out.notify
    out.notify_email = form_text(form, "notify_email")

    settings.save()
    scheduler.reschedule(settings)
    return saved("/output")


@app.post("/output/printer/discover")
async def printer_discover() -> JSONResponse:
    import deliver

    return await check_json(deliver.discover)


@app.post("/output/printer/test")
async def printer_test(request: Request) -> JSONResponse:
    host = str((await body_dict(request)).get("host") or "").strip()
    if not host:
        return JSONResponse({"ok": False, "error": "No printer host set"})

    import deliver

    # diagnose() calls asyncio.run() internally, so it must not run on the
    # event loop's thread. It never raises: a failure is a failed step.
    def work() -> Any:
        diagnosis = deliver.diagnose(host)
        deliver.record_printer_check(diagnosis)
        return diagnosis

    return await check_json(work)


@app.post("/output/printer/test-page")
async def printer_test_page(request: Request) -> JSONResponse:
    payload = await body_dict(request)
    settings = Settings.load()
    host = str(payload.get("host") or settings.output.print.printer_host or "").strip()
    if not host:
        return JSONResponse({"ok": False, "error": "No printer host set"})
    duplex = bool(payload.get("duplex", settings.output.print.duplex))

    def work() -> dict[str, Any]:
        import deliver
        from render.render import render

        # Check the printer before rendering: a test page is not worth a
        # wait on a job that can never be picked up.
        diagnosis = deliver.diagnose(host)
        deliver.record_printer_check(diagnosis)
        step = diagnosis.failed
        if step is not None:
            raise RuntimeError(f"{step.detail} {step.hint}".strip())

        out = data_dir() / "preview" / "test-page"
        out.mkdir(parents=True, exist_ok=True)
        data = json.loads(SAMPLE_DATA.read_text())
        with jobs.RUN_LOCK:
            result = render(data, settings.look, out)
            try:
                deliver.print_pdf(
                    result.pdf,
                    host,
                    duplex=duplex,
                    paper_name=settings.look.paper_name,
                )
            except Exception as err:
                deliver.record_print_result(host, f"{type(err).__name__}: {err}")
                raise
        deliver.record_print_result(host)
        return {"sent": True, "pages": result.pages, "host": host}

    return await check_json(work)


@app.post("/output/email/test")
async def email_test(request: Request) -> JSONResponse:
    payload = await body_dict(request)
    raw = payload.get("to")
    if isinstance(raw, list):
        to = [str(a).strip() for a in raw if str(a).strip()]
    else:
        to = [a.strip() for a in str(raw or "").split(",") if a.strip()]
    to = to or list(Settings.load().output.email.to)
    if not to:
        return JSONResponse({"ok": False, "error": "No recipients set"})

    import deliver

    def work() -> dict[str, Any]:
        deliver.send_test(
            to, Env.smtp(), paper_name=Settings.load().look.paper_name
        )
        return {"sent_to": to}

    return await check_json(work)


# ------------------------------------------------------- runs and previews
@app.post("/run")
def run_now(dry: int = 0) -> JSONResponse:
    dry_run = bool(dry)

    def work(job: jobs.Job) -> dict[str, Any]:
        from run import run as run_paper

        result = run_paper(Settings.load(), dry_run=dry_run)
        return {
            "dry_run": dry_run,
            "date": result.date,
            "ok": result.ok,
            "pages": result.pages,
            "error": result.error or "",
            "delivery": {k: (v or "ok") for k, v in (result.delivery or {}).items()},
            "gather_errors": result.gather_errors,
            "pdf": Path(result.pdf).name if result.pdf else "",
        }

    job = jobs.start("run", work)
    return JSONResponse({"ok": True, "job": job.id})


@app.get("/preview")
def preview_get(request: Request) -> Response:
    job_id = request.query_params.get("job") or ""
    job = jobs.get(job_id) if JOB_ID.match(job_id) else None
    return page(request, "preview", "preview.html", job=job.as_dict() if job else None)


@app.post("/preview")
def preview_post(source: str = "sample") -> RedirectResponse:
    which = "today" if source == "today" else "sample"

    def work(job: jobs.Job) -> dict[str, Any]:
        from render.render import render

        settings = Settings.load()
        out = jobs.preview_dir(job.id)
        out.mkdir(parents=True, exist_ok=True)
        if which == "today":
            from run import run as run_paper

            result = run_paper(settings, dry_run=True)
            source_data = data_dir() / "out" / result.date / "data.json"
            if not source_data.exists():
                raise RuntimeError(result.error or f"no data for {result.date}")
            # data.json carries today's puzzle under "crossword" when the
            # source is on; the template typesets it. The sample issue has
            # a made-up puzzle so "Render example" shows the layout.
            data = json.loads(source_data.read_text())
        else:
            data = json.loads(SAMPLE_DATA.read_text())
        rendered = render(data, settings.look, out, png=True)
        jobs.prune_previews()
        titles = [str((a or {}).get("title", "")) for a in (data.get("articles") or [])]
        return {
            "source": which,
            "pages": rendered.pages,
            "crossword": bool(data.get("crossword")),
            "printed": [titles[i] for i in rendered.printed if i < len(titles)],
            "held": [t for i, t in enumerate(titles) if i not in rendered.printed],
            "pngs": [f"/preview/{job.id}/{p.name}" for p in rendered.pngs],
            "html": f"/preview/{job.id}/paper.html",
        }

    job = jobs.start("preview", work)
    return RedirectResponse(f"/preview?job={job.id}", status_code=303)


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    job = jobs.get(job_id) if JOB_ID.match(job_id) else None
    if job is None:
        return JSONResponse({"detail": "no such job"}, status_code=404)
    return JSONResponse(job.as_dict())


@app.get("/preview/{job_id}/{name}")
def preview_file(job_id: str, name: str) -> Response:
    if not (JOB_ID.match(job_id) and PREVIEW_FILE.match(name)):
        return JSONResponse({"detail": "not found"}, status_code=404)
    path = jobs.preview_dir(job_id) / name
    if not path.is_file():
        return JSONResponse({"detail": "not found"}, status_code=404)
    media = "image/png" if name.endswith(".png") else "text/html"
    return FileResponse(path, media_type=media)


# ------------------------------------------------------------ archive, log
@app.get("/archive/{name}")
def archive(name: str) -> Response:
    if not ARCHIVE_NAME.match(name):
        return JSONResponse({"detail": "not found"}, status_code=404)
    path = data_dir() / "archive" / name
    if not path.is_file():
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.get("/log")
def log_page(request: Request) -> Response:
    path = data_dir() / "logs" / "run.log"
    try:
        lines = path.read_text(errors="replace").splitlines()[-200:]
    except OSError:
        lines = []
    return page(request, "log", "log.html", lines=lines, log_path=str(path))


# ------------------------------------------------------------- POST /tasks
@app.post("/tasks")
async def post_tasks(request: Request) -> JSONResponse:
    """What the iPhone Shortcut posts every morning. Bearer token, no cookies."""
    token = Env.get(Env.TASKS_TOKEN)
    if not token:
        return JSONResponse(
            {"ok": False, "detail": f"{Env.TASKS_TOKEN} is not set in the container"},
            status_code=503,
        )
    scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(given.strip().encode(), token.encode()):
        return JSONResponse({"ok": False, "detail": "bad token"}, status_code=401)

    raw = await request.body()
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "detail": "body is not JSON"}, status_code=400)
        tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    else:
        # iOS Shortcuts sometimes sends text/plain: one task per line.
        tasks = raw.decode("utf-8", "replace").splitlines()

    from gather import tasks as tasks_gather

    written = tasks_gather.write_tasks(tasks if isinstance(tasks, list) else [])
    return JSONResponse({"ok": True, "count": len(written)})
