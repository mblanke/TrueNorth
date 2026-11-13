"""Companion oracles for the confirmed audit findings of #443, one per strict-xfail test in
``test_audit_relay_443.py`` (integrator standing order, card #450). Each pins the *same*
secure behaviour as its oracle from a second angle — a second role, or the second handshake
record uvicorn emits — with a different input, so a fix that only satisfies the original
oracle's input does not pass unnoticed. Like the oracles, each asserts the correct behaviour
and therefore fails at base 280a838b, under ``xfail(strict=True)``; when the fix lands the
unexpected pass turns into a hard failure and the marker comes off. No app code is touched.

* Companion of ``test_the_grant_in_the_ws_handshake_line_is_redacted`` (finding
  ``control-plane/api/app/middleware.py:617``): the *refused* handshake — the ``403`` record
  uvicorn's websockets impl logs at ``websockets_sansio_impl.py:465`` — carries the same
  ``/ws/console/<grant>`` path through the same ``uvicorn.error`` logger (:70) that
  ``setup_middleware`` does not cover; the token must be redacted there too.
* Companion of ``test_a_console_just_closed_frees_the_live_cap`` (finding
  ``control-plane/api/app/console_relay/service.py:143``): the cap is per user regardless of
  role, so an *instructor's* closed-but-unexpired consoles hold the instructor's four seats
  exactly as a Student's do; the finding is not the Student's alone.
"""

from __future__ import annotations

import io
import logging

import pytest
from app.console_relay.models import ConsoleGrant
from app.console_relay.service import MAX_LIVE_GRANTS, GrantRefusedError
from fastapi import FastAPI

# Fixtures and helpers come from the neighbouring relay test file, by module path (never
# ``from conftest import``), as the oracle file does.
from test_console_relay import RFB_GREETING, _open, _released, _wait_for, world  # noqa: F401

pytestmark = pytest.mark.audit443


@pytest.mark.xfail(strict=True, reason="audit #443: the redaction filter covers the wrong logger (control-plane/api/app/middleware.py:617) — companion")
def test_the_grant_in_the_refused_ws_handshake_line_is_redacted():
    """Companion of ``test_the_grant_in_the_ws_handshake_line_is_redacted``: the same finding
    (control-plane/api/app/middleware.py:617) seen from the handshake's *other* outcome. A
    socket the relay refuses is logged by the very same ``uvicorn.error`` logger
    (uvicorn/protocols/websockets/websockets_sansio_impl.py:70) as ``403`` with the full
    ``/ws/console/<grant>`` path (:465), not only the ``[accepted]`` line the oracle replays —
    so a refused dial leaks the token the same way, and a fix must cover both records."""
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
        # exactly the record uvicorn's websockets impl emits for a refused handshake (:465)
        handshake_logger.info('%s - "WebSocket %s" 403', "10.30.30.5:55555", f"/ws/console/{token}")
    finally:
        handshake_logger.removeHandler(handler)
        handshake_logger.setLevel(old_level)

    logged = stream.getvalue()
    assert token not in logged, f"grant token in clear where uvicorn logs the refused handshake: {logged!r}"
    assert "/ws/console/<redacted>" in logged


@pytest.mark.xfail(strict=True, reason="audit #443: the live cap counts closed-but-unexpired grants (control-plane/api/app/console_relay/service.py:143) — companion")
def test_an_instructors_just_closed_consoles_free_their_live_cap(client, db_session, request):
    """Companion of ``test_a_console_just_closed_frees_the_live_cap``: the same finding
    (control-plane/api/app/console_relay/service.py:143) from a second role. ``issue`` counts
    by user and tenant only (service.py:141-143), so the cap binds the instructor like the
    Student: after the instructor has opened and closed four consoles — none live — the
    instructor's fifth machine must not answer 429 "You already have 4 consoles open"."""
    relay_world = request.getfixturevalue("world")
    for i in range(MAX_LIVE_GRANTS):
        with _open(client, relay_world.grant(relay_world.lead, vm_name=f"vm{i}"), who="kc-lead") as ws:
            assert ws.receive_bytes() == RFB_GREETING
        assert _wait_for(_released(db_session))  # the socket is closed and the grant released

    instructor_grants = db_session.query(ConsoleGrant).filter(ConsoleGrant.user_id == relay_world.lead.id).all()
    assert len(instructor_grants) == MAX_LIVE_GRANTS and not any(
        g.live_until is not None for g in instructor_grants), (
        "every socket is closed: no console of the instructor is open")

    refused = None
    try:
        token = relay_world.grant(relay_world.lead, vm_name="vm5")
    except GrantRefusedError as exc:  # the base behaviour: 429, four seats held by closed consoles
        refused = exc
    assert refused is None, f"zero consoles open, yet the instructor's 5th machine is refused: {refused}"
    assert token
