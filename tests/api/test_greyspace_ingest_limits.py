"""WARC ingest refuses malformed lengths (review of #133, 2026-10-11).

``int(headers["content-length"])`` took "-1", and ``fh.read(-1)`` then read (and held)
the rest of the stream as one record; an oversized record was skipped by reading it all
into memory; a truncated record was yielded short.
"""

from __future__ import annotations

import io

import pytest
from app.greyspace import ingest


class _Counting(io.BytesIO):
    """Records the largest single read, so a test can see nothing was slurped."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.max_read = 0

    def read(self, size=-1):
        out = super().read(size)
        self.max_read = max(self.max_read, len(out))
        return out


def _record(length: str, block: bytes, uri: str = "https://a.example.com/") -> bytes:
    return (f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: {uri}\r\n"
            f"Content-Length: {length}\r\n\r\n").encode() + block + b"\r\n\r\n"


GOOD = ingest.http_response(b"<h1>ok</h1>", "text/html")


def test_a_well_formed_record_is_read():
    [rec] = ingest.read_records(io.BytesIO(_record(str(len(GOOD)), GOOD)))
    assert rec.block == GOOD and rec.uri == "https://a.example.com/"


@pytest.mark.parametrize("length", ["-1", "-5000", "+5", "1_0", "abc", "", "٣", "1" * 16])
def test_a_malformed_content_length_is_refused(length):
    tail = _record(str(len(GOOD)), GOOD) * 3  # what fh.read(-1) used to swallow
    fh = _Counting(_record(length, b"") + tail)
    with pytest.raises(ValueError, match="bad WARC Content-Length"):
        list(ingest.read_records(fh))
    assert fh.max_read == 0  # refused before reading any block


def test_an_oversized_record_is_skipped_without_holding_it(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_RECORD_BYTES", 1000)
    monkeypatch.setattr(ingest, "_DISCARD_CHUNK", 256)
    big = b"x" * 5000
    fh = _Counting(_record(str(len(big)), big, "https://a.example.com/big.mp4") + _record(str(len(GOOD)), GOOD))
    [rec] = ingest.read_records(fh)
    assert rec.block == GOOD
    assert fh.max_read <= 256


def test_a_declared_length_past_the_end_is_a_truncated_record(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_RECORD_BYTES", 1000)
    with pytest.raises(ValueError, match="truncated"):  # oversized: discarding runs out
        list(ingest.read_records(io.BytesIO(_record("999999999", b"short"))))
    with pytest.raises(ValueError, match="truncated"):  # within the limit: the block is short
        list(ingest.read_records(io.BytesIO(_record("900", b"short"))))


def test_extract_fails_on_a_hostile_archive_and_stores_nothing(tmp_path):
    warc = tmp_path / "bad.warc"
    warc.write_bytes(_record("-1", b"") + _record(str(len(GOOD)), GOOD))
    with pytest.raises(ValueError):
        ingest.extract([warc], tmp_path / "out")
    assert not (tmp_path / "out/sites").exists()
