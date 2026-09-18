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
from gather import submit as real_submit
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
    calls: dict[str, object] = {"pdfs": [], "result": {"archive": None}, "pages": None}

    def deliver(pdf, settings, *, test=False, pages=None):
        calls["pdfs"].append(Path(pdf))
        calls["pages"] = pages
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
    assert fake_deliver["pages"] == result.pages == 2
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


def test_a_second_paper_the_same_day_is_filed_beside_the_first(data_dir, fake_gather,
                                                              fake_deliver):
    """Pressing the button again never writes over the morning's sheet."""
    first = run(Settings())
    day = first.date
    morning = data_dir / "archive" / f"{day}.pdf"
    assert first.pdf == morning
    morning.write_bytes(b"%PDF-1.4 the morning's own sheet\n")

    second = run(Settings())

    assert second.date == day
    assert second.pdf == data_dir / "archive" / f"{day}-2.pdf"
    assert morning.read_bytes() == b"%PDF-1.4 the morning's own sheet\n"
    assert fake_deliver["pdfs"][-1] == data_dir / "archive" / f"{day}-2.pdf"
    state = state_module.load_state()
    assert state["last_pdf"].endswith(f"{day}-2.pdf")
    assert state["last_issue"] == 2

    third = run(Settings())
    assert third.pdf == data_dir / "archive" / f"{day}-3.pdf"


def test_a_replay_files_its_own_copy_and_counts_nothing(data_dir, fake_gather, fake_deliver):
    """A replay re-reads out/<date>/data.json as today, whatever is archived."""
    first = run(Settings())
    replay = run(Settings(), date=first.date)

    assert replay.ok and replay.date == first.date
    assert fake_gather["count"] == 1                        # the replay did not gather
    assert (data_dir / "archive" / f"{first.date}.pdf").exists()
    assert replay.pdf == data_dir / "archive" / f"{first.date}-2.pdf"
    state = state_module.load_state()
    assert state["issue"] == 1                              # one issue, one number
    assert state["last_issue"] == 0                         # the replay carries none


def test_the_latest_archive_is_the_newest_paper_on_file(data_dir, fake_gather, fake_deliver):
    assert run_module.latest_archive() is None
    first = run(Settings())
    assert run_module.latest_archive() == first.pdf

    # With state lost -- or `last_pdf` pointing at a file that is gone -- the
    # archive folder itself is asked.
    state_module.update_state(last_pdf="/nowhere/2026-01-01.pdf")
    assert run_module.latest_archive() == first.pdf


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
    # An empty paper is still a paper -- one page of it, with nothing to
    # continue onto a second.
    assert result.pages == 1
    assert result.printed == []


def test_a_morning_with_no_articles_is_a_one_page_paper(data_dir, fake_gather, fake_deliver):
    """It prints, it delivers, and it counts as an issue -- on one page."""
    def run_all(settings):
        return {**json.loads(SAMPLE.read_text()), "articles": [], "errors": {}}

    sys.modules["gather"].run_all = run_all

    result = run(Settings())

    assert result.ok and result.error is None
    assert result.pages == 1 and result.printed == []
    assert state_module.load_state()["last_pages"] == 1
    assert state_module.load_state()["issue"] == 1
    # What the render laid out is what the print route is told.
    assert fake_deliver["pages"] == 1


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
    """Articles carry a `guid` and a `url`; `mark_seen` records its calls."""
    seen: list[list[str]] = []
    orig = sys.modules["gather"].run_all

    def run_all(settings):
        data = orig(settings)
        for i, a in enumerate(data["articles"]):
            a["guid"] = f"post-{i}"
            a["url"] = f"https://example.com/p/post-{i}"
        return data

    sys.modules["gather"].run_all = run_all
    sub = types.ModuleType("gather.substack")
    sub.mark_seen = lambda guids: seen.append(list(guids))
    monkeypatch.setitem(sys.modules, "gather.substack", sub)
    return seen


def test_real_run_marks_only_printed_posts_seen(data_dir, fake_substack, fake_deliver, monkeypatch):
    """With the crossword on, the sample sheet has room for three of its four
    stories; the fourth stays unseen so it can print another morning."""
    puzzle = json.loads(SAMPLE.read_text())["crossword"]
    xw = types.ModuleType("gather.crossword")
    xw.fetch = lambda settings, **kw: puzzle
    monkeypatch.setitem(sys.modules, "gather.crossword", xw)
    sys.modules["gather"].submit = lambda name, fn, *a: _Done(fn(*a))
    s = Settings()
    s.sources.crossword.enabled = True

    result = run(s)
    assert result.ok and result.crossword
    assert result.printed == [0, 1, 2]
    assert fake_substack == [["post-0", "post-1", "post-2"]]
    written = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert all("guid" not in a for a in written["articles"])
    # `url` is the contract's, not the run's bookkeeping: it stays.
    assert written["articles"][0]["url"] == "https://example.com/p/post-0"
    assert written["crossword"]["title"] == puzzle["title"]


class _Done:
    """A resolved future, for the crossword's submit() seam."""

    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def test_without_a_crossword_every_sample_story_prints(data_dir, fake_substack, fake_deliver):
    result = run(Settings())            # crossword source is off by default
    assert not result.crossword
    assert result.printed == [0, 1, 2, 3]
    assert fake_substack == [["post-0", "post-1", "post-2", "post-3"]]


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


# ------------------------------------------------------------ partial stories
class _Rendered:
    """What `render.render()` hands back, for the runs that fake it."""

    def __init__(self, pdf, pages, printed, partial):
        self.pdf = pdf
        self.html = pdf.with_suffix(".html")
        self.pages = pages
        self.printed = list(printed)
        self.partial = dict(partial)
        self.pngs: list = []


@pytest.fixture
def fake_render(monkeypatch):
    """A render that reports what it printed without starting Chromium."""
    report: dict[str, object] = {"printed": [0, 1], "partial": {1: 2}}

    def render_paper(data, look, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 not a real paper\n")
        return _Rendered(pdf, 2, report["printed"], report["partial"])

    monkeypatch.setattr(run_module, "render_paper", render_paper)
    return report


def test_a_partly_printed_story_counts_as_used(data_dir, fake_substack, fake_deliver,
                                               fake_render, caplog):
    """The reader has its beginning on paper and the address of the rest, so
    the post is used up: its guid is marked seen with the whole ones."""
    with caplog.at_level("INFO", logger="run"):
        result = run(Settings())

    assert result.ok
    assert result.printed == [0, 1]
    assert result.partial == {1: 2}
    assert fake_substack == [["post-0", "post-1"]]

    titles = [a["title"] for a in json.loads(SAMPLE.read_text())["articles"]]
    assert f"printed the first 2 paragraphs of {titles[1]}; the rest is online" in caplog.text
    # the ones that did not fit at all are held, by title
    assert "held for another day" in caplog.text
    assert titles[2] in caplog.text

    written = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert written["articles"][1]["url"] == "https://example.com/p/post-1"
    assert all("guid" not in a for a in written["articles"])


def test_a_partial_index_the_layout_left_out_still_counts(data_dir, fake_substack,
                                                          fake_deliver, fake_render):
    """`printed` is whole + partial, even if the layout only lists the whole."""
    fake_render["printed"] = [0]
    fake_render["partial"] = {2: 1}
    result = run(Settings())
    assert result.printed == [0, 2]
    assert fake_substack == [["post-0", "post-2"]]


def test_no_partial_is_an_empty_dict(data_dir, fake_substack, fake_deliver, fake_render, caplog):
    fake_render["partial"] = {}
    with caplog.at_level("INFO", logger="run"):
        result = run(Settings())
    assert result.partial == {}
    assert "the rest is online" not in caplog.text


def test_a_render_without_partial_still_runs(data_dir, fake_substack, fake_deliver, monkeypatch):
    """Until the render side lands, `partial` may simply not be there."""
    def render_paper(data, look, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 not a real paper\n")
        rendered = _Rendered(pdf, 2, [0, 1], {})
        del rendered.partial
        return rendered

    monkeypatch.setattr(run_module, "render_paper", render_paper)
    result = run(Settings())
    assert result.ok and result.partial == {} and result.printed == [0, 1]


# ----------------------------------------------------------------- crossword
SAMPLE_PUZZLE = {
    "provider": "nyt",
    "date": "2026-09-17",
    "title": "Cross Purposes",
    "author": "Robyn Weintraub",
    "editor": "Will Shortz",
    "width": 3,
    "height": 3,
    "grid": [[{"n": 1}, {"n": 2}, None], [{"n": 3}, {"n": None}, {"n": 4}], [None, {"n": 5}, {"n": None}]],
    "across": [{"n": 1, "clue": "Word after fire or fly"}],
    "down": [{"n": 1, "clue": "Quaint \u201cthank you\u201d"}],
}


@pytest.fixture
def fake_crossword(monkeypatch, fake_gather):
    """`gather.crossword.fetch` is ours; the real daemon-thread helper is not."""
    calls: dict[str, object] = {"count": 0, "puzzle": SAMPLE_PUZZLE, "raises": None}

    def fetch(settings, **kwargs):
        calls["count"] += 1
        if calls["raises"]:
            raise calls["raises"]
        return calls["puzzle"]

    module = types.ModuleType("gather.crossword")
    module.fetch = fetch
    monkeypatch.setitem(sys.modules, "gather.crossword", module)
    sys.modules["gather"].crossword = module
    sys.modules["gather"].submit = real_submit
    return calls


def _crossword_in(data_dir, result) -> object:
    data = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert "crossword" in data, "the key is always there, nullable"
    return data["crossword"]


def test_an_enabled_crossword_lands_in_the_data(data_dir, fake_crossword, fake_deliver):
    settings = Settings()
    settings.sources.crossword.enabled = True

    result = run(settings, dry_run=True)

    assert result.ok and result.crossword is True
    assert fake_crossword["count"] == 1
    assert _crossword_in(data_dir, result) == SAMPLE_PUZZLE


def test_no_crossword_when_the_source_is_off(data_dir, fake_crossword, fake_deliver):
    result = run(Settings(), dry_run=True)

    assert result.ok and result.crossword is False
    assert fake_crossword["count"] == 0, "a source that is off is not fetched from"
    assert _crossword_in(data_dir, result) is None


def test_a_crossword_that_raises_still_prints_the_paper(data_dir, fake_crossword, fake_deliver):
    fake_crossword["raises"] = RuntimeError("NYT-S cookie expired or invalid")
    settings = Settings()
    settings.sources.crossword.enabled = True

    result = run(settings, dry_run=True)

    assert result.ok and result.pages == 2           # the paper is the paper
    assert result.crossword is False
    assert _crossword_in(data_dir, result) is None


def test_a_crossword_that_finds_nothing_is_not_an_error(data_dir, fake_crossword, fake_deliver):
    fake_crossword["puzzle"] = None                  # e.g. today is not one of its days
    settings = Settings()
    settings.sources.crossword.enabled = True

    result = run(settings, dry_run=True)
    assert result.ok and result.crossword is False
    assert _crossword_in(data_dir, result) is None


def test_a_replay_keeps_the_archived_puzzle_without_refetching(data_dir, fake_crossword, fake_deliver):
    settings = Settings()
    settings.sources.crossword.enabled = True
    first = run(settings, dry_run=True)

    replay = run(settings, date=first.date)

    assert replay.ok and replay.crossword is True
    assert fake_crossword["count"] == 1              # the replay did not fetch
    assert _crossword_in(data_dir, replay) == SAMPLE_PUZZLE


# ------------------------------------------------------------------ volume
def test_the_volume_counts_years_of_publication(data_dir, fake_gather, fake_deliver, monkeypatch):
    import state as state_mod
    from datetime import date, datetime

    first = run(Settings())
    assert first.ok
    written = json.loads((data_dir / "out" / first.date / "data.json").read_text())
    assert written["paper"]["volume"].startswith("Vol. I, No. 1")
    recorded = state_mod.load_state()["first_issue_date"]
    assert recorded == first.date

    # A day short of the anniversary is still volume I; the anniversary is II.
    y, m, d = (int(x) for x in recorded.split("-"))
    assert state_mod.volume_number(date(y + 1, m, d).replace(day=max(1, d - 1)) if d > 1 else date(y + 1, m, d)) in (1, 2)
    assert state_mod.volume_number(date(y + 1, m, d)) == 2
    assert state_mod.volume_number(date(y + 3, m, d)) == 4

    # A second print in the same volume is No. 2.
    second = run(Settings())
    written = json.loads((data_dir / "out" / second.date / "data.json").read_text())
    assert written["paper"]["volume"].startswith("Vol. I, No. 2")

    # Three years on: a new volume, and the numbering starts again at 1.
    later = datetime(y + 3, m, d, 6, 0, tzinfo=datetime.now().astimezone().tzinfo)
    monkeypatch.setattr("run._now", lambda: later)
    third = run(Settings())
    written = json.loads((data_dir / "out" / third.date / "data.json").read_text())
    assert written["paper"]["volume"].startswith("Vol. IV, No. 1")
    st = state_mod.load_state()
    assert st["volume"] == 4 and st["issue"] == 1 and st["issues_total"] == 3
    # And the one after it is No. 2 of Vol. IV.
    fourth = run(Settings())
    written = json.loads((data_dir / "out" / fourth.date / "data.json").read_text())
    assert written["paper"]["volume"].startswith("Vol. IV, No. 2")


def test_roman_numerals():
    from state import roman
    assert [roman(n) for n in (1, 2, 4, 5, 9, 10, 14, 40, 99)] == ["I", "II", "IV", "V", "IX", "X", "XIV", "XL", "XCIX"]


def test_the_folio_date_follows_the_chosen_style(data_dir, fake_gather, fake_deliver):
    s = Settings()
    s.look.date_format = "iso"
    result = run(s, dry_run=True)
    written = json.loads((data_dir / "out" / result.date / "data.json").read_text())
    assert written["paper"]["date"] == result.date          # YYYY-MM-DD


# ------------------------------------------------------------------ reprint
def test_reprint_hands_the_latest_issue_back_to_the_routes(data_dir, fake_substack,
                                                           fake_deliver, monkeypatch, caplog):
    """The sheet that was already made goes out again, and nothing is made:
    no gather, no issue, no post marked seen, no state touched."""
    first = run(Settings())
    monkeypatch.setattr(run_module, "_page_count", lambda pdf: 2)
    fake_deliver["pdfs"].clear()
    fake_substack.clear()
    before = state_module.load_state()

    with caplog.at_level("INFO", logger="run"):
        result = run_module.reprint(Settings())

    assert result.ok and result.error is None
    assert result.pdf == first.pdf and result.pages == 2
    assert fake_deliver["pdfs"] == [first.pdf] and fake_deliver["pages"] == 2
    assert fake_substack == []                              # nothing marked seen
    assert state_module.load_state() == before              # and no state written
    assert f"reprinted {first.pdf.name}: archive: ok" in caplog.text


def test_reprint_without_an_archive_says_so(data_dir, fake_deliver):
    result = run_module.reprint(Settings())
    assert result.ok is False
    assert result.error == run_module.NOTHING_TO_REPRINT
    assert fake_deliver["pdfs"] == []


def test_reprint_reports_the_route_that_failed(data_dir, fake_gather, fake_deliver, monkeypatch):
    run(Settings())
    monkeypatch.setattr(run_module, "_page_count", lambda pdf: None)
    fake_deliver["result"] = {"print": "printer offline"}

    result = run_module.reprint(Settings())

    assert result.ok is False
    assert result.pages is None                             # an uncounted PDF still goes
    assert "printer offline" in (result.error or "")
    assert state_module.load_state()["last_error"] == ""    # the run's record is its own
