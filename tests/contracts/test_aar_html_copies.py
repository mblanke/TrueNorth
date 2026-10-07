"""The AAR renderer exists twice: the API renders ``/aar/html`` from it and the worker's
``generate_aar`` stores ``report_html`` from it. The services ship as separate images, so
the file is copied, and the copies must be the same bytes or the stored page and the
served page disagree (and an escaping fix lands in only one of them)."""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "control-plane/api/app/aar_html.py"
WORKER = ROOT / "control-plane/worker/worker/aar_html.py"


def test_the_worker_copy_matches_the_api():
    assert WORKER.read_bytes() == API.read_bytes(), f"{WORKER} drifted from {API}; copy the API's file over"
