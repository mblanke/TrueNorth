# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scenario_engine.validators.manual_ack and deliverable_check: the two summative gates.
ManualAckValidator is the context's manual acknowledgement truth table; DeliverableCheck
stats the deliverable object in MinIO through the context's client, with the default or a
configured bucket, and fails closed. The MinIO client is a recording fake."""

from __future__ import annotations

from scenario_engine.validators.deliverable_check import DeliverableCheckValidator
from scenario_engine.validators.manual_ack import ManualAckValidator


class FakeMinio:
    def __init__(self, fail=False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def stat_object(self, bucket, key):
        self.calls.append((bucket, key))
        if self.fail:
            raise RuntimeError("minio unreachable")
        return {"object_name": key}


def test_manual_ack_is_false_until_the_learner_acknowledges():
    v = ManualAckValidator()
    assert v.validate({}) is False
    assert v.validate({"manual_ack": False}) is False
    assert v.validate({"manual_ack": None}) is False


def test_manual_ack_passes_on_a_truthy_acknowledgement():
    v = ManualAckValidator()
    assert v.validate({"manual_ack": True}) is True
    assert v.validate({"manual_ack": "yes"}) is True


def test_deliverable_check_stats_the_object_in_the_default_bucket():
    minio = FakeMinio()
    v = DeliverableCheckValidator({"key": "runs/ex1/report.pdf"})
    assert v.validate({"minio_client": minio}) is True
    assert minio.calls == [("truenorth-deliverables", "runs/ex1/report.pdf")]


def test_deliverable_check_honours_a_configured_bucket():
    minio = FakeMinio()
    v = DeliverableCheckValidator({"bucket": "proof", "key": "evidence.gz"})
    assert v.validate({"minio_client": minio}) is True
    assert minio.calls == [("proof", "evidence.gz")]


def test_deliverable_check_fails_without_a_minio_client():
    minio = FakeMinio()
    v = DeliverableCheckValidator({"key": "runs/ex1/report.pdf"})
    assert v.validate({}) is False
    assert minio.calls == []


def test_deliverable_check_fails_closed_when_minio_raises():
    v = DeliverableCheckValidator({"key": "runs/ex1/report.pdf"})
    assert v.validate({"minio_client": FakeMinio(fail=True)}) is False
