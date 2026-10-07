"""worker/aar.py build_report: the report dict and the HTML page stored with it."""

from __future__ import annotations

import json
from collections import namedtuple

from worker.aar import build_report

ExRow = namedtuple("ExRow", ["id", "name", "state", "total_score", "max_score", "started_at", "completed_at"])
ObjRow = namedtuple("ObjRow", ["ref_id", "description", "objective_type", "points", "achieved", "evidence", "achieved_at"])


def _rows(name="Op Tern"):
    ex = ExRow("ex-1", name, "completed", 40, 100, "2026-01-15T10:00:00+00:00", "2026-01-15T12:00:00+00:00")
    objs = [
        ObjRow("det-1", "Detect beacon", "detection", 40, True, "alert 7", "2026-01-15T11:00:00+00:00"),
        ObjRow("rep-1", "Write <report>", "deliverable", 60, False, None, None),
    ]
    return ex, objs


def test_report_sections_and_flat_keys():
    report, _ = build_report("ex-1", *_rows())
    json.dumps(report)  # stored as JSON
    assert report["exercise"]["name"] == "Op Tern"
    assert report["scores"] == {"total": 40, "max": 100, "pct": 40.0}
    assert report["summary"] == {"total_objectives": 2, "achieved": 1, "score_pct": 40.0}
    assert [t["title"] for t in report["timeline"]] == [
        "Exercise started",
        "Objective det-1 achieved",
        "Exercise completed",
    ]
    assert report["injects"] == [] and report["participants"] == []
    assert report["exercise_name"] == "Op Tern" and report["total_score"] == 40  # legacy flat keys


def test_html_is_the_full_escaped_page():
    _, page = build_report("ex-1", *_rows(name="<b>Op</b>"))
    assert page.startswith("<!doctype html>")
    assert "&lt;b&gt;Op&lt;/b&gt;" in page and "<b>Op</b>" not in page
    assert "Write &lt;report&gt;" in page
    for section in ("summary", "objectives", "detections", "timeline", "participants"):
        assert f'id="{section}"' in page
    assert "Detection points: 40 of 40" in page


def test_no_scores_and_no_objectives():
    ex = ExRow("ex-2", "Empty", "pending", None, None, None, None)
    report, page = build_report("ex-2", ex, [])
    assert report["scores"] == {"total": 0, "max": 0, "pct": 0.0}
    assert report["timeline"] == []
    assert "No objectives were recorded" in page
