"""The Windows password staff are shown is the one the worker gives the VM (owner decision).

For a Windows machine with no declared Administrator, the worker's Sysprep sets
``range_accounts.default_password()``; the API shows staff
``template_defaults.windows_password()`` as its "template default". The API may not import
the worker, so each decides from the same settings; if they drift, staff are shown a
password that does not log in. Lab sessions are random on both sides (tested where each
decides it: worker attach, API ``windows_random``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app.range_credentials import template_defaults  # noqa: E402
from worker import range_accounts  # noqa: E402


@pytest.mark.parametrize(("template", "alias", "expected"), [
    ("Win-Build-1!", "", "Win-Build-1!"),
    ("Win-Build-1!", "Old-Default-1!", "Win-Build-1!"),  # the template setting wins
    ("", "Old-Default-1!", "Old-Default-1!"),  # the deprecated alias, only when it is unset
    ("  ", "", None),  # blank is unset: random per VM
    ("", "", None),
])
def test_worker_and_api_choose_the_same_windows_password(monkeypatch, template, alias, expected):
    monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", template)
    monkeypatch.setenv("TN_RANGE_DEFAULT_PASSWORD", alias)
    worker = range_accounts.default_password()
    assert (worker.reveal() if worker else None) == template_defaults.windows_password() == expected
