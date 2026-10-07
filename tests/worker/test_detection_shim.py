"""worker/detection.py is only the seam tasks.py imports: the worker never scores (ADR 0005)."""

import logging

import pytest
from worker import detection, telemetry


@pytest.mark.parametrize("backend", ["mock", "vsphere_api", "proxmox"])
@pytest.mark.parametrize("flag", ["off", "on", "true", ""])
def test_the_worker_never_has_a_detection_scorer(monkeypatch, backend, flag):
    monkeypatch.setenv("DETECTION_SCORING", flag)
    assert detection.detection_scorer("ex-1", lambda: None, backend) is None


def test_a_deployment_still_asking_for_worker_scoring_is_told_it_is_retired(monkeypatch, caplog):
    monkeypatch.setenv("DETECTION_SCORING", "on")
    with caplog.at_level(logging.WARNING, logger="truenorth.worker.detection"):
        detection.detection_scorer("ex-9", lambda: None, "vsphere_api")
    assert "retired" in caplog.text and "ex-9" in caplog.text


def test_scoring_off_is_silent(monkeypatch, caplog):
    monkeypatch.delenv("DETECTION_SCORING", raising=False)
    with caplog.at_level(logging.WARNING, logger="truenorth.worker.detection"):
        detection.detection_scorer("ex-9", lambda: None, "vsphere_api")
    assert caplog.text == ""


def test_range_index_is_the_telemetry_modules():
    assert detection.range_index is telemetry.range_index
    assert detection.range_index("abc") == "range-abc"
