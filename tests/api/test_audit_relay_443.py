"""Oracle tests for the confirmed audit findings of tracking issue #443 (console relay and
its logging), one test per finding, marked ``audit443``. Each test asserts the *correct*
behaviour, so it fails at the base commit the finding was confirmed on. Failing tests carry
``xfail(strict=True)`` (integrator decision 2026-11-05) so the suite stays green here while
still failing on ``--runxfail``; when a fix lands, strict xfail turns the unexpected pass
into a hard failure and the marker must come off. No app code is touched here.

Findings and their state at base 280a838b:

* ``control-plane/api/app/middleware.py:617`` — Console grant token is written in clear to
  the API log by uvicorn's WebSocket handshake line; the redaction filter and its test cover
  the wrong logger. STILL PRESENT: ``test_the_grant_in_the_ws_handshake_line_is_redacted``.
* ``control-plane/api/app/console_relay/service.py:143`` — Live-console cap
  (``MAX_LIVE_GRANTS=4``) counts closed-but-unexpired grants: a Student is refused the 5th
  machine with "You already have 4 consoles open" while none are open. STILL PRESENT:
  ``test_a_console_just_closed_frees_the_live_cap``.
* Console outlives its sign-in token (was: a console closed when the token expired) — FIXED
  in #442 (d96f8074). No new test: ``test_console_relay.py`` already pins it with the passing
  ``test_the_console_outlives_the_sign_in_token_it_was_opened_with`` (re-run at the base: 1
  passed); duplicating it here would only drift.
"""

from __future__ import annotations

import io
import logging

import pytest
from app.console_relay.models import ConsoleGrant
from app.console_relay.service import GrantRefusedError
from fastapi import FastAPI

# Fixtures and helpers come from the neighbouring relay test file, by module path (never
# ``from conftest import``). The imported ``world`` registers here as a fixture; the test
# fetches it by name so no parameter shadows the import.
from test_console_relay import RFB_GREETING, _open, _released, _wait_for, world  # noqa: F401

pytestmark = pytest.mark.audit443


@pytest.mark.xfail(strict=True, reason="audit #443: the redaction filter covers the wrong logger (control-plane/api/app/middleware.py:617)")
def test_the_grant_in_the_ws_handshake_line_is_redacted():
    """#443 finding (control-plane/api/app/middleware.py:617): the redaction filter covers the
    wrong logger. ``setup_middleware`` attaches ``RedactSecretPaths`` only to
    ``uvicorn.access``, but uvicorn 0.54 logs every WebSocket handshake outcome through the
    logger it hands the websockets server, ``uvicorn.error``
    (uvicorn/protocols/websockets/websockets_sansio_impl.py:70), with the full
    ``/ws/console/<grant>`` path as the message arg (:441 ``[accepted]``, :465 ``403``).
    Logger-level filters do not propagate, so the grant token reaches the API log in clear
    for as long as the grant lives, though ``redact_path`` promises to hide it."""
    token = "T0kenMustNotReachTheLog443"

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handshake_logger = logging.getLogger("uvicorn.error")
    handshake_logger.addHandler(handler)
    old_level = handshake_logger.level
    handshake_logger.setLevel(logging.INFO)
    try:
        from app.middleware import redact_path, setup_middleware

        assert redact_path(f"/ws/console/{token}") == "/ws/console/<redacted>"
        setup_middleware(FastAPI())
        # exactly the record uvicorn's websockets impl emits (sansio impl :441, msg + args)
        handshake_logger.info('%s - "WebSocket %s" [accepted]', "10.30.30.5:55555",
                              f"/ws/console/{token}")
    finally:
        handshake_logger.removeHandler(handler)
        handshake_logger.setLevel(old_level)

    logged = stream.getvalue()
    assert token not in logged, f"grant token in clear where uvicorn logs the handshake: {logged!r}"
    assert "/ws/console/<redacted>" in logged


@pytest.mark.xfail(strict=True, reason="audit #443: the live cap counts closed-but-unexpired grants (control-plane/api/app/console_relay/service.py:143)")
def test_a_console_just_closed_frees_the_live_cap(client, db_session, request):
    """#443 finding (control-plane/api/app/console_relay/service.py:143): the live-console cap
    counts closed-but-unexpired grants. ``issue`` counts every grant with ``expires_at > now
    AND dials < MAX_DIALS`` (service.py:142-143) and ``release`` (service.py:243-250) clears
    only the live claim, so a console the Student closed keeps consuming one of the four
    seats for the rest of its ``GRANT_SECONDS`` window — with zero consoles open, the 5th
    machine answers 429 "You already have 4 consoles open; close one first", which the
    Student cannot act on. A closed console is not live: closing it must free the seat."""
    relay_world = request.getfixturevalue("world")
    for i in range(4):
        with _open(client, relay_world.grant(relay_world.ann, vm_name=f"vm{i}")) as ws:
            assert ws.receive_bytes() == RFB_GREETING
        assert _wait_for(_released(db_session))  # the socket is closed and the grant released

    assert not any(g.live_until is not None for g in db_session.query(ConsoleGrant).all()), (
        "every socket is closed: no console is open")

    refused = None
    try:
        token = relay_world.grant(relay_world.ann, vm_name="vm5")
    except GrantRefusedError as exc:  # the base behaviour: 429, four seats held by closed consoles
        refused = exc
    assert refused is None, f"zero consoles open, yet the 5th machine is refused: {refused}"
    assert token
