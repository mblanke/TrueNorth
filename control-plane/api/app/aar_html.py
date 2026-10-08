"""After-action report: HTML rendering and PDF-safe text.

Pure functions over the AAR report dict; no imports from the API or the worker. The same
file lives at ``control-plane/api/app/aar_html.py`` and
``control-plane/worker/worker/aar_html.py``, because the two services ship as separate
images. ``tests/contracts/test_aar_html_copies.py`` fails if the copies drift; edit one
and copy it over the other.

Every value from the report is escaped. Names, objective text, evidence and AI output
are all written by people or models, and the page is served from the API's origin.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import unicodedata
from typing import Any

# Characters outside latin-1 that NFKD cannot reduce but that have an obvious ASCII
# spelling. Keyed by code point so this source stays ASCII.
_PDF_SUBSTITUTES = {
    chr(cp): text
    for cp, text in (
        (0x2018, "'"),  # left single quote
        (0x2019, "'"),  # right single quote
        (0x201A, ","),  # low single quote
        (0x201C, '"'),  # left double quote
        (0x201D, '"'),  # right double quote
        (0x201E, '"'),  # low double quote
        (0x2013, "-"),  # en dash
        (0x2014, "-"),  # em dash
        (0x2015, "-"),  # horizontal bar
        (0x2022, "*"),  # bullet
        (0x2026, "..."),  # ellipsis
        (0x2190, "<-"),  # leftwards arrow
        (0x2192, "->"),  # rightwards arrow
        (0x2264, "<="),
        (0x2265, ">="),
        (0x2713, "v"),  # check mark
        (0x2714, "v"),  # heavy check mark
        (0x2717, "x"),  # ballot x
        (0x0141, "L"),  # L with stroke
        (0x0142, "l"),
        (0x0110, "D"),  # D with stroke
        (0x0111, "d"),
        (0x0152, "OE"),
        (0x0153, "oe"),
    )
}


def pdf_text(value: object) -> str:
    """``value`` as text that fpdf2's core fonts (latin-1) can draw without raising.

    Characters with a latin-1 form keep it; accented letters outside latin-1 lose the
    accent (NFKD); common typographic punctuation becomes ASCII; anything left over
    (CJK, emoji) becomes ``?``.
    """
    text = "" if value is None else str(value)
    out: list[str] = []
    for ch in text:
        if ch in _PDF_SUBSTITUTES:
            out.append(_PDF_SUBSTITUTES[ch])
            continue
        try:
            ch.encode("latin-1")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            base.encode("latin-1")
            out.append(base or "?")
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value: object) -> float:
    """A score or points value as a number; anything unparseable counts as 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return value
    try:
        return float(str(value))
    except ValueError:
        return 0


def detection_summary(objectives: list) -> dict[str, Any]:
    """Detection objectives and the points they carried, from the report's objectives."""
    items = [o for o in _list(objectives) if isinstance(o, dict) and o.get("type") == "detection"]
    detected = [o for o in items if o.get("achieved")]
    return {
        "total": len(items),
        "detected": len(detected),
        "points_earned": sum(_num(o.get("points")) for o in detected),
        "points_available": sum(_num(o.get("points")) for o in items),
        "items": items,
    }


def _md_lite(text: str) -> str:
    """Escaped text with headings, bold, bullets and paragraphs. Escapes first, so the
    tags added here are the only markup in the output."""
    out: list[str] = []
    in_list = False
    for raw in _e(text).splitlines():
        line = raw.strip()
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,3}) (.+)$", line)
        if heading:
            out.append(f"<h3>{heading.group(2)}</h3>")
        elif line:
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

_CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1b1f24;
background:#fff;max-width:960px;margin:0 auto;padding:24px;line-height:1.45}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px;
border-bottom:1px solid #d0d7de;padding-bottom:4px}h3{font-size:15px;margin:16px 0 6px}
.meta{color:#57606a;font-size:13px}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eaeef2;vertical-align:top}
th{background:#f6f8fa}.ok{color:#1a7f37;font-weight:600}.miss{color:#cf222e;font-weight:600}
.kpis{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}.kpi{border:1px solid #d0d7de;
border-radius:6px;padding:8px 12px;min-width:120px}.kpi b{display:block;font-size:20px}
.empty{color:#57606a;font-style:italic}
@media print{body{padding:0}h2{break-after:avoid}}
"""


# The headers the API serves ``/exercises/{id}/aar/html`` with when it is opened directly.
# The global ``default-src 'self'`` would block the one inline style block, so the page gets
# its own policy: that block allowed by hash, nothing else loadable, no script at all, and
# framing only by the app's own origin. Every other route keeps the global policy.
_CSS_HASH = base64.b64encode(hashlib.sha256(_CSS.encode("utf-8")).digest()).decode("ascii")
RESPONSE_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'sha256-{_CSS_HASH}'; img-src data:; script-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
    "X-Frame-Options": "SAMEORIGIN",
}


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict) -> str:
    """The full AAR page for ``report`` (the dict stored in ``after_action_reports.report_json``).

    Tolerates reports from older producers: any section may be missing, and the worker's
    flat keys (``exercise_name``, ``total_score`` ...) stand in for the nested ones.
    """
    report = _dict(report)
    ex = _dict(report.get("exercise"))
    name = ex.get("name") or report.get("exercise_name") or "Unknown exercise"
    state = ex.get("state") or report.get("state") or "-"
    scenario = _dict(report.get("scenario"))
    range_ = _dict(report.get("range"))
    scores = _dict(report.get("scores"))
    total = _num(scores.get("total", report.get("total_score")))
    max_score = _num(scores.get("max", report.get("max_score")))
    pct = scores.get("pct")
    if pct is None:
        pct = round(total / max(max_score, 1) * 100, 1)
    total, max_score = (int(v) if float(v).is_integer() else v for v in (total, max_score))
    objectives = [o for o in _list(report.get("objectives")) if isinstance(o, dict)]
    achieved = sum(1 for o in objectives if o.get("achieved"))
    det = detection_summary(objectives)

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        # Every value is escaped; this keeps the page inert if that ever slips. It also
        # applies inside the web app's sandboxed iframe, where the API's headers do not.
        f'<meta http-equiv="Content-Security-Policy" content="{_PAGE_CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>AAR: {_e(name)}</title><style>{_CSS}</style></head><body>",
        "<header>",
        f"<h1>After-Action Report: {_e(name)}</h1>",
        '<p class="meta">',
        f"State: {_e(state)}",
        f" &middot; Scenario: {_e(scenario.get('name') or '-')}",
        f" &middot; Range: {_e(range_.get('name') or '-')}" if range_ else "",
        f" &middot; Generated {_e(report.get('generated_at') or '-')}",
        f" by {_e(report.get('generated_by'))}" if report.get("generated_by") else "",
        "</p></header>",
    ]

    # Summary
    started = ex.get("started_at") or report.get("started_at")
    completed = ex.get("completed_at") or report.get("completed_at")
    parts += [
        '<section id="summary"><h2>Exercise summary</h2>',
        '<div class="kpis">',
        f'<div class="kpi"><b>{_e(pct)}%</b>Score ({_e(total)} / {_e(max_score)})</div>',
        f'<div class="kpi"><b>{achieved} / {len(objectives)}</b>Objectives achieved</div>',
        f'<div class="kpi"><b>{det["detected"]} / {det["total"]}</b>Detections</div>',
        "</div>",
        f'<p class="meta">Started: {_e(started or "-")} &middot; Completed: {_e(completed or "-")}</p>',
        "</section>",
    ]

    # Objectives
    obj_rows = [
        [
            '<span class="ok">Achieved</span>' if o.get("achieved") else '<span class="miss">Not achieved</span>',
            _e(o.get("ref_id")),
            _e(o.get("type")),
            _e(o.get("description")),
            _e(o.get("points", 0)),
            _e(o.get("evidence") or ""),
            _e(o.get("achieved_at") or ""),
        ]
        for o in objectives
    ]
    parts += [
        '<section id="objectives"><h2>Objectives</h2>',
        _table(
            ["Result", "Ref", "Type", "Objective", "Points", "Evidence", "Achieved at"],
            obj_rows,
            "No objectives were recorded for this exercise.",
        ),
        "</section>",
    ]

    # Detections and scores
    det_rows = [
        [
            '<span class="ok">Detected</span>' if o.get("achieved") else '<span class="miss">Missed</span>',
            _e(o.get("ref_id")),
            _e(o.get("description")),
            _e(o.get("points", 0)),
            _e(o.get("achieved_at") or ""),
        ]
        for o in det["items"]
    ]
    parts += [
        '<section id="detections"><h2>Detections and scores</h2>',
        f"<p>Detection points: {_e(det['points_earned'])} of {_e(det['points_available'])}. "
        f"Exercise score: {_e(total)} of {_e(max_score)} ({_e(pct)}%).</p>",
        _table(["Result", "Ref", "Detection", "Points", "Detected at"], det_rows, "No detection objectives."),
        "</section>",
    ]

    # Timeline: planned injects, then what happened
    injects = [i for i in _list(report.get("injects")) if isinstance(i, dict)]
    inject_rows = [
        [_e(i.get("at") or ""), _e(i.get("title") or ""), _e(i.get("detail") or ""), _e(i.get("status") or "")]
        for i in injects
    ]
    events = [t for t in _list(report.get("timeline")) if isinstance(t, dict)]
    event_rows = [
        [_e(t.get("at") or ""), _e(t.get("kind") or ""), _e(t.get("title") or ""), _e(t.get("detail") or "")]
        for t in events
    ]
    parts += [
        '<section id="timeline"><h2>Timeline</h2>',
        "<h3>Injects</h3>",
        _table(["When", "Inject", "Detail", "Status"], inject_rows, "No injects were scheduled."),
        "<h3>Events</h3>",
        _table(["When", "Kind", "Event", "Detail"], event_rows, "No events were recorded."),
        "</section>",
    ]

    # Participants
    people = [p for p in _list(report.get("participants")) if isinstance(p, dict)]
    people_rows = [[_e(p.get("name")), _e(p.get("team") or ""), _e(p.get("role") or "")] for p in people]
    parts += [
        '<section id="participants"><h2>Participants</h2>',
        _table(["Name", "Team", "Role"], people_rows, "No participants were recorded."),
        "</section>",
    ]

    ai = _dict(report.get("ai_analysis"))
    if ai.get("summary"):
        parts += [
            '<section id="ai-analysis"><h2>AI analysis</h2>',
            _md_lite(str(ai["summary"])),
            f'<p class="meta">Generated by {_e(ai.get("model") or "unknown model")}</p>',
            "</section>",
        ]

    parts.append("</body></html>")
    return "\n".join(p for p in parts if p)
