"""Worker ingest: server-stamped ingest time (ADR 0005, review finding 2).

The bulk_ingest tests land with bulk_ingest itself (.agent-patches/s4-detection-tasks.patch).
"""

from __future__ import annotations

from worker import telemetry


def test_stamp_overwrites_any_truenorth_field_the_sender_sent():
    forged = {"x": 1, "truenorth": {"ingested_at": "1999"}, "truenorth.ingested_at": "1999", "@timestamp": "1999"}
    out = telemetry.stamp(forged, "r-1", "2026-10-06T12:00:00+00:00")
    assert out["truenorth"] == {"ingested_at": "2026-10-06T12:00:00+00:00", "range_id": "r-1"}
    assert "truenorth.ingested_at" not in out
    assert out["@timestamp"] == "1999" and out["x"] == 1
