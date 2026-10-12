"""scripts/_moodle_student.py, the acceptance suite's browserless Moodle Student, against a
mock Moodle (httpx.MockTransport) that serves Moodle 5.2's markup for the pages it uses.

The real round trip is the staging check itself (``lms / Moodle loop``) and, in a browser,
tests/integration/test_moodle_student_journey.py. These pin the parts that are easy to get
quietly wrong: the internal route with the public Host, Secure cookies over the internal
http hop, redirects, keyed answers, resuming the same attempt, and a refusal that says why.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from _moodle_student import MoodleWeb, MoodleWebError, Page  # noqa: E402

PUBLIC = "https://moodle.example.test:8443"
INTERNAL = "http://moodle-default:8080"
KEY = [
    {"stem": "<p>Which port does HTTPS use?</p>", "options": ["80", "443", "22"], "correct": [1]},
    {"stem": "Which tool reads Windows event logs?", "options": ["Event Viewer", "ping"], "correct": [0]},
]


def question(usage: int, slot: int, stem: str, options: list[str]) -> str:
    """One multichoice question as Moodle 5.2 renders it (answers shuffled: listed reversed)."""
    p = f"q{usage}:{slot}_"
    answers = "".join(
        f'<div class="r{i % 2}"><input type="radio" name="{p}answer" value="{i}" id="{p}answer{i}" '
        f'aria-labelledby="{p}answer{i}_label"><div class="d-flex w-auto" id="{p}answer{i}_label" '
        f'data-region="answer-label"><span class="answernumber">{chr(97 + n)}. </span>'
        f'<div class="flex-fill ms-1"><p>{options[i]}</p></div></div></div>'
        for n, i in enumerate(reversed(range(len(options))))
    )
    return (f'<div id="question-{usage}-{slot}" class="que multichoice deferredfeedback notyetanswered">'
            f'<div class="formulation clearfix"><input type="hidden" name="{p}:sequencecheck" value="1">'
            f'<div class="qtext">{stem}</div><div class="ablock"><div class="answer">{answers}</div></div>'
            f'<input type="radio" name="{p}answer" value="-1" class="sr-only" id="{p}clear"></div></div>')


class FakeMoodle:
    """sso.php, the course, a page, a two-question quiz (one per page), summary, review."""

    def __init__(self, refuse_sso: bool = False):
        self.refuse_sso = refuse_sso
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, str] = {}
        self.attempts = 0
        self.at = 0  # the page the Student was last sent to: where "Continue" resumes
        self.finished = False

    def page(self, body: str, status: int = 200, cookie: str | None = None) -> httpx.Response:
        headers = {"Set-Cookie": f"MoodleSession={cookie}; path=/; Secure; HttpOnly"} if cookie else {}
        return httpx.Response(status, text=f"<html><body>{body}</body></html>", headers=headers)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path, q = req.url.path, parse_qs(req.url.query.decode())
        form = parse_qs(req.content.decode()) if req.method == "POST" else {}
        if path == "/local/truenorth/sso.php":
            if self.refuse_sso:
                return self.page('<div class="alert alert-danger"><p class="errormessage">TrueNorth sign-in was '
                                 "refused. Open the course again from the TrueNorth app.</p></div>", 404)
            assert form["token"] == ["tkn"]
            return httpx.Response(303, headers={"Location": f"{PUBLIC}/course/view.php?id=7",
                                                "Set-Cookie": "MoodleSession=s1; path=/; Secure; HttpOnly"})
        assert req.headers.get("cookie") == "MoodleSession=s1", f"{path}: no session cookie"
        if path == "/course/view.php":
            return self.page('<a href="https://moodle.example.test:8443/mod/page/view.php?id=21">Read</a>'
                             '<a href="https://moodle.example.test:8443/mod/quiz/view.php?id=22">Quiz</a>'
                             '<a href="https://moodle.example.test:8443/mod/page/view.php?id=21">again</a>')
        if path == "/mod/page/view.php":
            return self.page("<h2>Read</h2>")
        if path == "/mod/quiz/view.php":
            label = "Continue your attempt" if self.attempts else "Attempt quiz"
            return self.page(f'<form method="post" action="{PUBLIC}/mod/quiz/startattempt.php">'
                             '<input type="hidden" name="cmid" value="22"><input type="hidden" name="sesskey" value="k">'
                             f'<button type="submit">{label}</button></form>')
        if path == "/mod/quiz/startattempt.php":
            assert form == {"cmid": ["22"], "sesskey": ["k"]}
            self.attempts += 1
            return httpx.Response(303, headers={"Location": f"{PUBLIC}/mod/quiz/attempt.php?attempt=55&cmid=22"
                                                            + (f"&page={self.at}" if self.at else "")})
        if path == "/mod/quiz/attempt.php":
            page = int(q.get("page", ["0"])[0])
            stem, options = KEY[page]["stem"], KEY[page]["options"]
            nextpage = page + 1 if page + 1 < len(KEY) else -1
            return self.page(
                f'<form action="{PUBLIC}/mod/quiz/processattempt.php?cmid=22" method="post" id="responseform">'
                + question(900, page + 1, stem, options)
                + f'<input type="hidden" name="attempt" value="55"><input type="hidden" name="thispage" value="{page}">'
                f'<input type="hidden" name="nextpage" value="{nextpage}"><input type="hidden" name="sesskey" value="k">'
                f'<input type="hidden" name="slots" value="{page + 1}">'
                '<input type="submit" name="next" value="Next page"></form>')
        if path == "/mod/quiz/processattempt.php":
            if form.get("finishattempt") == ["1"]:
                self.finished = True
                return httpx.Response(303, headers={"Location": f"{PUBLIC}/mod/quiz/review.php?attempt=55"})
            self.answers.update({k: v[0] for k, v in form.items() if k.endswith("_answer")})
            nxt = int(form["nextpage"][0])
            self.at = max(nxt, 0)
            where = "summary.php?attempt=55" if nxt == -1 else f"attempt.php?attempt=55&cmid=22&page={nxt}"
            return httpx.Response(303, headers={"Location": f"{PUBLIC}/mod/quiz/{where}"})
        if path == "/mod/quiz/summary.php":
            return self.page(f'<form method="post" action="{PUBLIC}/mod/quiz/processattempt.php">'
                             '<input type="hidden" name="attempt" value="55"><input type="hidden" name="finishattempt" '
                             'value="1"><input type="hidden" name="timeup" value="0"><input type="hidden" name="slots" '
                             'value=""><input type="hidden" name="sesskey" value="k">'
                             '<button type="submit">Submit all and finish</button></form>')
        if path == "/mod/quiz/review.php":
            return self.page("<h2>Review</h2>")
        return self.page("not found", 404)


def web(moodle: FakeMoodle) -> MoodleWeb:
    return MoodleWeb(PUBLIC, INTERNAL, transport=httpx.MockTransport(moodle))


def test_a_student_signs_in_reads_and_takes_the_quiz_with_a_resume():
    moodle = FakeMoodle()
    w = web(moodle)
    course = w.sign_in(f"{PUBLIC}/local/truenorth/sso.php", "tkn")
    assert MoodleWeb.activities(course, "page") == [21] and MoodleWeb.activities(course, "quiz") == [22]
    w.get("/mod/page/view.php?id=21")
    attempt = w.start_quiz(22)
    shown, matched, html = w.answer_page(w.get(w.url), KEY)
    assert (attempt, shown, matched) == (55, 1, 1)
    w.get("/course/view.php?id=7")  # leaves
    assert w.start_quiz(22) == 55  # "Continue your attempt": the same attempt
    html = w.get(w.url)
    while "/mod/quiz/summary.php" not in w.url:
        n, k, html = w.answer_page(html, KEY)
        shown, matched = shown + n, matched + k
    w.finish(html)
    assert (shown, matched, moodle.finished) == (2, 2, True)
    # The right options by their text, whatever order Moodle shuffled them into.
    assert moodle.answers == {"q900:1_answer": "1", "q900:2_answer": "0"}
    # Every request went to the internal address with Moodle's public Host.
    assert {(r.url.host, r.url.port, r.headers["host"]) for r in moodle.requests} == {
        ("moodle-default", 8080, "moodle.example.test:8443")
    }


def test_an_unkeyed_question_still_gets_an_answer_but_is_not_counted():
    moodle = FakeMoodle()
    w = web(moodle)
    w.sign_in(f"{PUBLIC}/local/truenorth/sso.php", "tkn")
    w.start_quiz(22)
    shown, matched, _ = w.answer_page(w.get(w.url), [])
    assert (shown, matched) == (1, 0) and "q900:1_answer" in moodle.answers


def test_a_refused_sign_in_says_what_moodle_said():
    w = web(FakeMoodle(refuse_sso=True))
    with pytest.raises(MoodleWebError, match="404: truenorth sign-in was refused"):
        w.sign_in(f"{PUBLIC}/local/truenorth/sso.php", "tkn")


def test_the_page_parser_reads_labels_and_question_text():
    page = Page("<form action='x/processattempt.php' method='post'>" + question(1, 3, "<b>Stem</b> text", ["A", "B"])
                + "</form>")
    assert page.qtext == {"q1:3_": "Stem text"}
    assert page.labels["q1:3_answer0_label"].strip().endswith("A")
    assert [i["name"] for i in page.form("processattempt.php")["inputs"]].count("q1:3_answer") == 3
    assert urlsplit(page.form("processattempt").get("action")).path == "x/processattempt.php"
