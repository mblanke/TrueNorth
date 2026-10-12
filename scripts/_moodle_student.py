"""A Student's Moodle web session without a browser, for scripts/staging_acceptance.py.

Integration coverage that **bypasses the browser**: the same pages and forms a browser
requests (local_truenorth's ``sso.php`` with TrueNorth's ticket, the course page, a page,
the quiz's start, attempt, process and summary pages), as plain HTTP from the one-off api
container. No JavaScript runs and nothing is rendered; the browser journey is the CI
``moodle`` lane's (tests/integration/test_moodle_student_journey.py) and the click-through
in docs/runbooks/moodle.md.

How it reaches Moodle: as TrueNorth's own server-side calls do (``app.lti13.platform_route``):
URLs on the Moodle's public origin (``lti_issuer``) go to the platform's ``base_url`` (the
address the api container reaches, e.g. ``http://moodle-default:8080``) with the public Host
header, so Moodle sees its own site URL. No Moodle token or password is used: the session is
the one TrueNorth's sign-in ticket opens. Cookies are kept here rather than in httpx's jar,
because Moodle marks them Secure (it is https behind its edge) and the internal hop is http.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})


class MoodleWebError(Exception):
    """Moodle did not do what a Student's browser would have seen it do."""


def norm(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split()).strip().lower()


class Page(HTMLParser):
    """What the session needs from a Moodle page: its forms (with every input), the text of
    elements whose id names an answer label, and each quiz question's text."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        self.qtext: dict[str, str] = {}  # question prefix "q<usage>:<slot>_" -> text
        self._stack: list[tuple[str, str | None, bool]] = []  # (tag, label id, in qtext)
        self._question = ""
        self._form: dict[str, Any] | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "div" and (m := re.match(r"^question-(\d+)-(\d+)$", a.get("id", ""))):
            self._question = f"q{m.group(1)}:{m.group(2)}_"
        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": a.get("method", "get").lower(), "inputs": []}
            self.forms.append(self._form)
        if tag in ("input", "button") and self._form is not None and a.get("name"):
            self._form["inputs"].append({**a, "_tag": tag})
        if tag in VOID:
            return
        label = a.get("id") if a.get("id", "").endswith("_label") else None
        qtext = "qtext" in a.get("class", "").split() or (self._stack and self._stack[-1][2])
        self._stack.append((tag, label, bool(qtext)))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        for _, label, _ in self._stack:
            if label:
                self.labels[label] = self.labels.get(label, "") + data
        if self._stack and self._stack[-1][2] and self._question:
            self.qtext[self._question] = self.qtext.get(self._question, "") + data

    def form(self, action_part: str) -> dict[str, Any]:
        found = [f for f in self.forms if action_part in f["action"]]
        if not found:
            raise MoodleWebError(f"no form posting to {action_part} on this page")
        return found[0]


def hidden(form: dict[str, Any]) -> dict[str, str]:
    """The fields a browser sends without the user touching anything (hidden inputs)."""
    return {i["name"]: i.get("value", "") for i in form["inputs"] if i.get("type", "text") == "hidden"}


class MoodleWeb:
    def __init__(self, issuer: str, base_url: str | None, *, timeout: float = 90, transport: Any = None):
        self.platform = SimpleNamespace(lti_issuer=issuer.rstrip("/"), base_url=(base_url or issuer).rstrip("/"))
        self.cookies: dict[str, str] = {}
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
        self.url = ""  # the public URL of the last page

    # -- transport ------------------------------------------------------------------------
    def request(self, method: str, url: str, data: dict | None = None) -> str:
        """Fetch ``url`` (public, or relative to the last page) as the Student's browser
        would, following redirects; returns the final page's HTML."""
        from app.lti13 import platform_route

        url = urljoin(self.url or self.platform.lti_issuer + "/", url)
        for _ in range(10):
            target, headers = platform_route(self.platform, url)
            if self.cookies:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            resp = self.client.request(method, target, data=data, headers=headers)
            for raw in resp.headers.get_list("set-cookie"):
                name, _, rest = raw.partition("=")
                value = rest.split(";", 1)[0]
                if value in ("", "deleted") or "max-age=0" in raw.lower():
                    self.cookies.pop(name.strip(), None)
                else:
                    self.cookies[name.strip()] = value
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                if resp.status_code in (301, 302, 303):
                    method, data = "GET", None
                continue
            self.url = url
            if resp.status_code >= 400:
                # Moodle answers a refused sign-in (moodle_exception) with an error page: say what it says.
                said = re.search(r'<(\w+)[^>]*class="[^"]*errormessage[^"]*"[^>]*>(.*?)</\1>', resp.text, re.S)
                raise MoodleWebError(f"{method} {urlsplit(url).path} => {resp.status_code}"
                                     + (f": {norm(said.group(2))[:200]}" if said else ""))
            return resp.text
        raise MoodleWebError(f"too many redirects from {url}")

    def get(self, url: str) -> str:
        return self.request("GET", url)

    def submit(self, form: dict[str, Any], fields: dict[str, str]) -> str:
        return self.request("POST" if form["method"] == "post" else "GET", form["action"], fields)

    # -- what a Student does --------------------------------------------------------------------
    def sign_in(self, action: str, token: str) -> str:
        """POST TrueNorth's ticket to sso.php, as the SPA's hidden form does: the course page."""
        html = self.request("POST", action, {"token": token})
        if "/course/view.php" not in urlsplit(self.url).path:
            raise MoodleWebError(f"sign-in did not open the course (at {urlsplit(self.url).path}): {norm(html)[:160]}")
        return html

    @staticmethod
    def activities(html: str, kind: str) -> list[int]:
        """Course-module ids of the activities of ``kind`` linked from a course page, in order."""
        seen: list[int] = []
        for cm in re.findall(rf"/mod/{kind}/view\.php\?id=(\d+)", html):
            if int(cm) not in seen:
                seen.append(int(cm))
        return seen

    def start_quiz(self, cmid: int) -> int:
        """Open the quiz and press "Attempt quiz" (or "Continue your attempt"): the attempt id."""
        page = Page(self.get(f"/mod/quiz/view.php?id={cmid}"))
        start = page.form("startattempt.php")
        self.submit(start, hidden(start))
        m = re.search(r"/mod/quiz/attempt\.php\?attempt=(\d+)", self.url)
        if not m:
            raise MoodleWebError(f"starting the quiz did not open an attempt (at {urlsplit(self.url).path})")
        return int(m.group(1))

    def answer_page(self, html: str, key: list[dict]) -> tuple[int, int, str]:
        """Answer every question on this attempt page from ``key`` (TrueNorth's questions:
        stem, options, correct) and go on ("Next page", or to the summary on the last page).
        Returns (questions on the page, answered from the key, the next page's HTML)."""
        page = Page(html)
        form = page.form("processattempt.php")
        fields = hidden(form)
        shown = matched = 0
        for prefix, text in page.qtext.items():
            shown += 1
            entry = next((q for q in key if norm(q["stem"]) == norm(text)), None)
            right = {norm(entry["options"][i]) for i in entry["correct"]} if entry else set()
            choices = [i for i in form["inputs"] if i["name"].startswith(prefix) and i.get("type") in ("radio", "checkbox")]
            picked = [c for c in choices if norm(re.sub(r"^\s*[a-z]\.\s*", "", page.labels.get(c.get("aria-labelledby", ""), ""), flags=re.I)) in right]
            matched += bool(entry and picked)
            for c in picked or choices[:1]:  # unkeyed: the first option, so the attempt still has an answer
                fields[c["name"]] = c.get("value", "")
        nav = next((i for i in form["inputs"] if i.get("type") == "submit" and i["name"] == "next"), None)
        if nav is not None:
            fields["next"] = nav.get("value", "")
        return shown, matched, self.submit(form, fields)

    def finish(self, html: str) -> str:
        """On the summary page, "Submit all and finish" (and confirm): the review page."""
        page = Page(html)
        form = next((f for f in page.forms if "processattempt.php" in f["action"]
                     and any(i["name"] == "finishattempt" for i in f["inputs"])), None)
        if form is None:
            raise MoodleWebError(f"no 'Submit all and finish' on {urlsplit(self.url).path}")
        out = self.submit(form, hidden(form))
        if "/mod/quiz/review.php" not in self.url:
            raise MoodleWebError(f"finishing the attempt did not open its review (at {urlsplit(self.url).path})")
        return out
