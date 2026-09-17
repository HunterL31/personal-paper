"""
The crossword gatherer: what it fetches, what it refuses to fetch, and the
one thing it must never hand on — the answers.

Nothing here touches the network: `requests.get` is monkeypatched and the
response is `tests/fixtures/nyt_daily.json`, a realistic v6 daily puzzle.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import state as state_module
from app.settings import Settings
from gather import crossword

MONDAY = dt.date(2026, 9, 14)
THURSDAY = dt.date(2026, 9, 17)     # the fixture's publication date


# ------------------------------------------------------------------ doubles
class FakeResponse:
    def __init__(self, status=200, *, json_body=None, body=b"", content_type=None):
        self.status_code = status
        self._json = json_body
        self.content = json.dumps(json_body).encode() if json_body is not None else body
        default = "application/json" if json_body is not None else "text/html; charset=utf-8"
        self.headers = {"content-type": content_type or default}

    def json(self):
        if self._json is None:
            raise ValueError("no JSON here")
        return self._json


LOGIN_PAGE = b"<!DOCTYPE html><html><head><title>Log in</title></head><body></body></html>"


@pytest.fixture
def puzzle_json(fixtures):
    return json.loads((fixtures / "nyt_daily.json").read_text())


@pytest.fixture
def cookie(monkeypatch):
    monkeypatch.setenv("NYT_S", "a-long-opaque-cookie-value")


@pytest.fixture
def requests_log(monkeypatch):
    """Every request the module makes, and what it got back."""
    calls: list[dict] = []
    answers: list = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if not answers:
            raise AssertionError(f"no canned answer for {url}")
        answer = answers[0] if len(answers) == 1 else answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(crossword.requests, "get", fake_get)
    return {"calls": calls, "answers": answers}


def _enabled(days=None) -> Settings:
    settings = Settings()
    settings.sources.crossword.enabled = True
    if days is not None:
        settings.sources.crossword.days = days
    return settings


# -------------------------------------------------------- when not to fetch
def test_disabled_fetches_nothing(requests_log):
    assert crossword.fetch(Settings(), today=THURSDAY) is None
    assert requests_log["calls"] == []


def test_a_day_that_is_switched_off_fetches_nothing(requests_log, cookie):
    settings = _enabled(days=[0, 1, 2])          # Mon-Wed only; the 17th is a Thursday
    assert crossword.fetch(settings, today=THURSDAY) is None
    assert requests_log["calls"] == []


def test_a_day_that_is_switched_on_does_fetch(requests_log, cookie, puzzle_json):
    requests_log["answers"].append(FakeResponse(json_body=puzzle_json))
    settings = _enabled(days=[3])                 # Thursday only
    assert crossword.fetch(settings, today=THURSDAY) is not None


def test_without_the_cookie_there_is_no_puzzle(requests_log, monkeypatch, caplog):
    monkeypatch.delenv("NYT_S", raising=False)
    with caplog.at_level("WARNING"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert requests_log["calls"] == []
    assert "NYT_S" in caplog.text


# ------------------------------------------------------------- the happy day
@pytest.fixture
def puzzle(requests_log, cookie, puzzle_json, data_dir):
    requests_log["answers"].append(FakeResponse(json_body=puzzle_json))
    return crossword.fetch(_enabled(), today=THURSDAY)


def test_the_request_carries_the_cookie_and_a_browser_agent(puzzle, requests_log):
    call = requests_log["calls"][0]
    assert call["url"] == "https://www.nytimes.com/svc/crosswords/v6/puzzle/daily/2026-09-17.json"
    assert call["headers"]["Cookie"] == "NYT-S=a-long-opaque-cookie-value"
    assert "Mozilla/5.0" in call["headers"]["User-Agent"]
    assert call["timeout"] == 20


def test_the_shape_of_what_comes_back(puzzle):
    assert puzzle["provider"] == "nyt"
    assert puzzle["date"] == "2026-09-17"
    assert puzzle["title"] == "Cross Purposes"
    assert puzzle["author"] == "Robyn Weintraub, Brad Wiegmann"
    assert puzzle["editor"] == "Will Shortz"
    assert puzzle["width"] == 15 and puzzle["height"] == 15
    assert len(puzzle["grid"]) == 15
    assert all(len(row) == 15 for row in puzzle["grid"])
    assert set(puzzle) == {
        "provider", "date", "title", "author", "editor",
        "width", "height", "grid", "across", "down",
    }


def test_black_squares_are_none_and_white_squares_are_numbers_only(puzzle):
    blacks = [cell for row in puzzle["grid"] for cell in row if cell is None]
    whites = [cell for row in puzzle["grid"] for cell in row if cell is not None]
    assert len(blacks) == 36 and len(whites) == 225 - 36
    assert all(set(cell) == {"n"} for cell in whites)
    assert all(cell["n"] is None or isinstance(cell["n"], int) for cell in whites)


def test_the_grid_is_numbered_by_the_standard_rule(puzzle):
    """Every square that starts an entry is numbered, in reading order."""
    black = [[cell is None for cell in row] for row in puzzle["grid"]]
    expected = crossword.numbering(black)
    actual = [[None if cell is None else cell["n"] for cell in row] for row in puzzle["grid"]]
    assert actual == expected
    numbers = [n for row in expected for n in row if n]
    assert numbers == sorted(numbers)                 # left to right, top to bottom
    assert numbers[0] == 1 and numbers[-1] == max(numbers)


def test_the_clue_numbers_are_the_squares_the_entries_start_on(puzzle):
    grid, height, width = puzzle["grid"], puzzle["height"], puzzle["width"]
    black = [[cell is None for cell in row] for row in grid]

    across, down = [], []
    for r in range(height):
        for c in range(width):
            if black[r][c]:
                continue
            n = grid[r][c]["n"]
            if (c == 0 or black[r][c - 1]) and (c + 1 < width and not black[r][c + 1]):
                across.append(n)
            if (r == 0 or black[r - 1][c]) and (r + 1 < height and not black[r + 1][c]):
                down.append(n)

    assert [e["n"] for e in puzzle["across"]] == across
    assert [e["n"] for e in puzzle["down"]] == down
    assert len(puzzle["across"]) + len(puzzle["down"]) == 72


def test_clues_are_plain_text_with_the_markup_flattened(puzzle):
    across = {e["n"]: e["clue"] for e in puzzle["across"]}
    down = {e["n"]: e["clue"] for e in puzzle["down"]}
    # The plain form wins where the response has one...
    assert across[1] == "Cleopatra's undoing, in Antony & Cleopatra"
    # ...and where it does not, the tags go and the entities are unescaped.
    assert down[1] == "Quaint “thank you”"
    assert all("<" not in e["clue"] and "&" not in e["clue"].replace(" & ", "")
               for e in puzzle["across"] + puzzle["down"])
    assert all(e["clue"] for e in puzzle["across"] + puzzle["down"])


def test_no_answer_ever_leaves_this_module(puzzle, puzzle_json):
    """The solution is in the response and must not be in the paper's data."""
    blob = json.dumps(puzzle)
    assert "answer" not in blob
    # The fixture's 1-Across answer, three letters, is nowhere in the output.
    first_three = "".join(
        cell["answer"] for cell in puzzle_json["body"][0]["cells"][:3]
    )
    assert first_three not in blob
    assert "board" not in blob


def test_the_raw_response_is_kept_for_debugging(puzzle, data_dir, puzzle_json):
    saved = data_dir / "out" / "2026-09-17" / "crossword.json"
    assert saved.exists()
    assert json.loads(saved.read_text()) == puzzle_json


def test_the_check_is_recorded_for_the_status_strip(puzzle):
    check = state_module.load_state()["crossword_check"]
    assert check["ok"] is True
    assert "Cross Purposes" in check["summary"]
    assert "72 clues" in check["summary"] and "15x15" in check["summary"]
    assert check["when"]


# ----------------------------------------------------------- the sad days
def test_a_login_page_says_the_cookie_was_rejected(requests_log, cookie, caplog):
    requests_log["answers"].append(FakeResponse(200, body=LOGIN_PAGE))
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "cookie rejected (got a login page)" in caplog.text
    assert "NYT-S" in caplog.text
    assert state_module.load_state()["crossword_check"]["ok"] is False


def test_403_says_the_cookie_expired(requests_log, cookie, caplog):
    requests_log["answers"].append(FakeResponse(403, body=b"{}", content_type="application/json"))
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "expired or invalid" in caplog.text
    assert "HTTP 403" in caplog.text


def test_404_names_the_date_and_the_url(requests_log, cookie, caplog):
    missing = FakeResponse(404, body=b"{}", content_type="application/json")
    requests_log["answers"].extend([missing, missing])     # v6, then the v3 fallback
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "no puzzle for 2026-09-17" in caplog.text
    assert "v3/puzzle/daily-2026-09-17.json" in caplog.text


def test_v6_falling_over_is_retried_on_v3(requests_log, cookie, puzzle_json):
    requests_log["answers"].extend([
        FakeResponse(500, body=b"oops", content_type="text/plain"),
        FakeResponse(json_body={"results": [puzzle_json]}),
    ])
    puzzle = crossword.fetch(_enabled(), today=THURSDAY)
    assert puzzle is not None and puzzle["title"] == "Cross Purposes"
    assert [c["url"].rsplit("/", 2)[-2] for c in requests_log["calls"]] == ["daily", "puzzle"]


def test_a_network_failure_is_a_log_line_not_an_exception(requests_log, cookie, caplog):
    import requests

    requests_log["answers"].append(requests.ConnectionError("no route to host"))
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "could not reach" in caplog.text


def test_a_response_without_a_grid_is_refused(requests_log, cookie, caplog):
    requests_log["answers"].append(FakeResponse(json_body={"id": 1, "body": [{"cells": []}]}))
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "no grid" in caplog.text


def test_a_grid_that_does_not_match_its_dimensions_is_refused(requests_log, cookie, caplog):
    body = {"cells": [{} for _ in range(10)], "dimensions": {"width": 15, "height": 15},
            "clues": []}
    requests_log["answers"].append(FakeResponse(json_body={"body": [body]}))
    with caplog.at_level("ERROR"):
        assert crossword.fetch(_enabled(), today=THURSDAY) is None
    assert "15x15 but has 10 cells" in caplog.text


# ---------------------------------------------------------------- check()
def test_check_reports_the_puzzle(requests_log, cookie, puzzle_json):
    requests_log["answers"].append(FakeResponse(json_body=puzzle_json))
    result = crossword.check(today=THURSDAY)
    assert result["ok"] is True
    assert result["date"] == "2026-09-17"
    assert result["title"] == "Cross Purposes"
    assert result["author"] == "Robyn Weintraub, Brad Wiegmann"
    assert result["size"] == "15x15"
    assert result["clues"] == 72
    assert result["error"] == ""
    assert state_module.load_state()["crossword_check"]["ok"] is True


def test_check_raises_with_the_message_that_says_what_to_do(requests_log, cookie):
    requests_log["answers"].append(FakeResponse(200, body=LOGIN_PAGE))
    with pytest.raises(RuntimeError, match="cookie rejected"):
        crossword.check(today=THURSDAY)
    assert state_module.load_state()["crossword_check"]["ok"] is False


def test_check_without_a_cookie_names_the_variable(requests_log, monkeypatch):
    monkeypatch.delenv("NYT_S", raising=False)
    with pytest.raises(RuntimeError, match="NYT_S is not set in the container"):
        crossword.check(today=THURSDAY)
    assert requests_log["calls"] == []


def test_check_on_a_day_off_still_checks(requests_log, cookie, puzzle_json):
    """The button is a test of the cookie, not of the schedule."""
    requests_log["answers"].append(FakeResponse(json_body=puzzle_json))
    assert crossword.check(today=MONDAY)["ok"] is True


# --------------------------------------------------------------- numbering
def test_numbering_of_a_tiny_grid():
    black = [
        [False, False, True],
        [False, False, False],
        [True, False, False],
    ]
    assert crossword.numbering(black) == [
        [1, 2, None],
        [3, None, 4],
        [None, 5, None],
    ]


def test_flatten_keeps_the_words_and_drops_the_markup():
    assert crossword.flatten("<i>Hamlet</i> soliloquy opener") == "Hamlet soliloquy opener"
    assert crossword.flatten("Mate&rsquo;s greeting") == "Mate’s greeting"
    assert crossword.flatten("  spaced  ") == "spaced"
