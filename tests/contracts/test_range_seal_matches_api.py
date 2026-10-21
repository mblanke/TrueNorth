"""The worker opens every range password the API stores (ADR 0013 §3).

The API seals range credentials with a key derived for that purpose (``tnsec:rc1:``,
app/range_credentials/seal.py); the worker cannot import the API and derives the same key
(worker/range_seal.py). If they drift, every build gets no passwords. The worker also reads
rows written before ADR 0013 (``tnsec:v1:``) and legacy plain values, as the API's column
type does, and never takes a general secretbox value for a range seal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app import secretbox as api_secretbox  # noqa: E402
from app.range_credentials import seal as api_seal  # noqa: E402
from app.range_credentials.sealed_column import RangeSealedText, unseal_stored  # noqa: E402
from worker import range_seal  # noqa: E402
from worker import secretbox as worker_secretbox  # noqa: E402


@pytest.mark.parametrize("password", ["Pw-Range-1!", "ünïcode-Pass-2", "x" * 128])
def test_the_worker_opens_what_the_api_seals(password):
    assert range_seal.unseal_stored(api_seal.seal(password)) == password


def test_earlier_values_are_still_read_on_both_sides():
    v1 = api_secretbox.seal("Old-Pass-1!")
    assert range_seal.unseal_stored(v1) == unseal_stored(v1) == "Old-Pass-1!"
    assert range_seal.unseal_stored("plain-legacy") == unseal_stored("plain-legacy") == "plain-legacy"
    assert range_seal.unseal_stored(None) is None


def test_the_column_writes_rc1_even_when_handed_a_v1_value():
    col = RangeSealedText()
    stored = col.process_bind_param(api_secretbox.seal("Old-Pass-1!"), None)
    assert stored.startswith(api_seal.PREFIX) and col.process_result_value(stored, None) == "Old-Pass-1!"
    assert col.process_bind_param(stored, None) == stored  # already rc1: as it is


def test_a_range_seal_another_key_made_is_refused(monkeypatch):
    monkeypatch.setenv(api_secretbox.KEY_ENV, "another-platforms-key-0123456789abcdef")
    foreign = api_seal.seal("Pw-1!")
    monkeypatch.undo()
    with pytest.raises(worker_secretbox.SecretUnreadableError):
        range_seal.unseal_stored(foreign)
