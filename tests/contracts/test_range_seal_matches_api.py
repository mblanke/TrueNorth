"""The worker opens every stored range credential the API seals (ADR 0013 §3), and only
with the binding it was sealed for.

The API seals stored credentials with a key derived for them only (``tnsec:rk1:``,
AES-256-GCM, app/range_credentials/seal.py) bound to tenant, range and row (or tenant,
snapshot and row); the worker cannot import the API and derives the same key
(worker/range_seal.py). If they drift, every build gets no passwords; if the binding is
lost, one row's seal opens as another's. The worker also reads rows from before the
migration (``rc1``, ``tnsec:v1``, plain), and never a template seal.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app import secretbox as api_secretbox  # noqa: E402
from app.range_credentials import seal as api_seal  # noqa: E402
from worker import range_seal  # noqa: E402
from worker import secretbox as worker_secretbox  # noqa: E402

T, R, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize("password", ["Pw-Range-1!", "ünïcode-Pass-2", "x" * 128])
def test_the_worker_opens_what_the_api_seals_with_the_same_binding(password):
    assert api_seal.row_aad(T, R, C) == range_seal.row_aad(T, R, C)
    assert api_seal.snapshot_aad(T, R, C) == range_seal.snapshot_aad(T, R, C)
    sealed = api_seal.seal_row(password, api_seal.row_aad(T, R, C))
    assert range_seal.unseal(sealed, range_seal.row_aad(T, R, C)) == password


@pytest.mark.parametrize("other", [
    lambda: range_seal.row_aad(T, R, uuid.uuid4()),  # another row
    lambda: range_seal.row_aad(uuid.uuid4(), R, C),  # another tenant
    lambda: range_seal.snapshot_aad(T, R, C),  # a snapshot binding
])
def test_a_seal_opens_only_with_its_own_binding(other):
    sealed = api_seal.seal_row("Pw-1!", api_seal.row_aad(T, R, C))
    with pytest.raises(worker_secretbox.SecretUnreadableError):
        range_seal.unseal(sealed, other())


def test_earlier_values_are_still_read():
    aad = range_seal.row_aad(T, R, C)
    assert range_seal.unseal(api_secretbox.seal("Old-Pass-1!"), aad) == "Old-Pass-1!"
    assert range_seal.unseal(api_seal.seal_legacy("Old-Pass-2!"), aad) == "Old-Pass-2!"
    assert range_seal.unseal("plain-legacy", aad) == "plain-legacy"
    assert range_seal.unseal(None, aad) is None


def test_a_template_seal_is_not_a_stored_credential():
    token = api_seal.seal("Typed-1!", T)
    assert range_seal.unseal(token, range_seal.row_aad(T, R, C)) == token  # not opened: no rk1/rc1/v1 prefix
    with pytest.raises(api_seal.NotARangeSealError):
        api_seal.unseal_row(token.replace(api_seal.TEMPLATE_PREFIX, api_seal.ROW_PREFIX), api_seal.row_aad(T, R, C))


def test_a_seal_another_key_made_is_refused(monkeypatch):
    monkeypatch.setenv(api_secretbox.KEY_ENV, "another-platforms-key-0123456789abcdef")
    foreign = api_seal.seal_row("Pw-1!", api_seal.row_aad(T, R, C))
    monkeypatch.undo()
    with pytest.raises(worker_secretbox.SecretUnreadableError):
        range_seal.unseal(foreign, range_seal.row_aad(T, R, C))
