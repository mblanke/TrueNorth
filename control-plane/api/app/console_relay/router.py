"""``/ws/console/{grant}``: the browser's end of a console relay (#366).

The browser opens ``wss://<tn>/ws/console/<grant>`` as for any WebSocket here:
``new WebSocket(url, ["bearer", accessToken])`` (``ws_auth``). The API checks the grant
belongs to that user and tenant and is unexpired and not already in use, asks the range's
console adapter where to connect (``dial``: for vSphere a WebMKS ticket taken now), connects
with the site's TLS trust and pumps bytes both ways. The stream is RFB; the relay does not
parse it (S4 filters a view-only grant's input). Bytes are never logged; neither are tickets,
URLs or a whole grant id.

Close codes (the reason is safe to show the Student):

====  ================================================================================
1008  Not allowed: no valid sign-in, or a grant that is unknown, another user's or
      another tenant's. Sent before the handshake completes (HTTP 403), as ``/ws/{channel}``.
1000  The console was closed by the machine's side, cleanly.
1001  The sign-in (token) expired while the console was open; reconnect with a fresh token.
1008  Also: the user may no longer open this console (``policy.py``: the session window closed,
      the Student left the roll call, observer consoles were switched off, the role or the
      lab changed). Asked at connect and every ``service.RENEW_SECONDS`` while open.
1009  The browser sent a message larger than ``MAX_CLIENT_MESSAGE`` (RFB input is tiny).
4404  The machine is gone: the range was deleted or is not running, or the grant was withdrawn.
1011  The database failed under a renewal (not the Student's doing); reconnect.
4408  Idle: no bytes either way for ``IDLE_SECONDS``.
4409  This console grant is already open in another socket (or was taken by one).
4410  The grant expired (about five minutes after issue) or was used for its ``MAX_DIALS``
      connections; ask for a new console.
4429  Too many connection attempts by this user in a minute.
4502  The hypervisor did not give a console (refused the ticket, or was unreachable).
4503  The connection to the hypervisor dropped while the console was open.
4504  The hypervisor did not answer in time.
====  ================================================================================

The socket never outlives the session window or lab it was opened in, nor the sign-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from .. import ws_auth
from ..console_backends import ConsoleError, DialGrant, get_console_backend
from ..db import get_db
from ..models import Range
from . import policy, service

logger = logging.getLogger("truenorth.console_relay")

router = APIRouter()

CLOSE_FORBIDDEN, CLOSE_NORMAL, CLOSE_TOKEN_EXPIRED = 1008, 1000, 1001
CLOSE_TOO_BIG, CLOSE_SERVICE, CLOSE_IDLE, CLOSE_RATE = 1009, 1011, 4408, 4429
CLOSE_GONE, CLOSE_BUSY, CLOSE_EXPIRED = 4404, 4409, 4410
CLOSE_UPSTREAM_REFUSED, CLOSE_UPSTREAM_LOST, CLOSE_UPSTREAM_TIMEOUT = 4502, 4503, 4504
#: Longest the hypervisor may take to hand over a ticket and accept the connection.
DIAL_SECONDS = 15
#: A single RFB message (a framebuffer update) can be large; bound it, not the stream.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
#: The other way: RFB input (keys, pointer, clipboard text) is small. 1 MiB is generous.
MAX_CLIENT_MESSAGE = 1024 * 1024
#: No bytes either way for this long closes the socket (a forgotten tab holds a hypervisor
#: console and a grant).
IDLE_SECONDS = 600
#: Consecutive failed renewals (an exception, never a policy "no") the socket survives: it
#: closes on this many. The tick is ``service.RENEW_SECONDS`` and the claim lasts three ticks.
MAX_RENEW_FAILURES = 3


async def _refuse(ws: WebSocket, code: int, reason: str, subprotocol: str | None) -> None:
    """Close after the handshake, so the browser sees the code and reason (before it, a
    browser only sees a failed connection)."""
    await ws.accept(subprotocol=subprotocol)
    await ws.close(code=code, reason=reason)


@contextlib.contextmanager
def _session(app: Any):
    """A database session for one short piece of work, from the same factory the requests use
    (``get_db``, with a test's override). The transaction is committed when the step succeeded
    (a read leaves one open) and rolled back when it raised; the session is closed: an open console holds no connection and no
    transaction between its renewals, only while a statement runs."""
    gen = app.dependency_overrides.get(get_db, get_db)()
    db = next(gen)
    try:
        yield db
    except BaseException:
        with contextlib.suppress(Exception):
            db.rollback()
        raise
    else:
        db.commit()
    finally:
        gen.close()


async def _call(app: Any, fn: Any, *args: Any) -> Any:
    """``fn(db, *args)`` on a worker thread with a session of its own."""

    def run() -> Any:
        with _session(app) as db:
            return fn(db, *args)

    return await run_in_threadpool(run)


async def _close_quietly(upstream: Any) -> None:
    with contextlib.suppress(Exception):  # already gone
        await upstream.close()


async def _give_back(app: Any, grant_id: uuid.UUID, holder: str) -> None:
    """Free the grant's one socket, on a session of its own (a renewal's thread may still be
    running on another). Done before the closing frame goes out, so a browser that reconnects
    the moment it sees the close does not meet its own previous socket. A database error is
    retried once, then logged: the claim lapses by itself within ``service.LIVE_LEASE``."""
    for attempt in (1, 2):
        try:
            await _call(app, service.release, grant_id, holder)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("console %s: releasing the grant failed (try %d): %s",
                           service.short(grant_id), attempt, type(exc).__name__)


async def _clean_up(app: Any, upstream: Any, grant_id: uuid.UUID, holder: str) -> None:
    if upstream is not None:
        await _close_quietly(upstream)
    await _give_back(app, grant_id, holder)


async def _upstream_connect(target: Any):
    """The hypervisor's side: one WebSocket to ``target.url`` with its TLS and subprotocols."""
    from websockets.asyncio.client import connect

    kwargs: dict[str, Any] = {"subprotocols": list(target.subprotocols), "open_timeout": DIAL_SECONDS,
                              "max_size": MAX_MESSAGE_BYTES, "compression": None}
    if target.url.startswith("wss://"):
        kwargs["ssl"] = target.ssl if target.ssl is not None else ssl.create_default_context()
    return await connect(target.url, **kwargs)


def _dial_blocking(app: Any, ident: DialGrant, tenant_id: uuid.UUID, abandoned: threading.Event) -> Any:
    """``backend.dial`` on a worker thread with a session of its own (the request's is not
    thread-safe and may be gone). When the relay gave up waiting (``abandoned``) the thread
    still runs to its end, and the adapter's own ``finally`` closes its hypervisor login; this
    logs that it did."""
    try:
        with _session(app) as db:
            # tenant-safe: the grant's own range, in the grant's tenant.
            rng = (db.query(Range)
                   .filter(Range.id == uuid.UUID(ident.range_id), Range.tenant_id == tenant_id,
                           Range.deleted_at.is_(None))
                   .first())
            if rng is None:
                raise ConsoleError("The machine is not running")
            # The adapter is handed the session because it needs it (vSphere: the range's
            # vCenter binding). It must end the transaction before any network call, as
            # vsphere_webmks does right after resolving the binding; this session is closed
            # on the way out either way.
            return get_console_backend(rng.provisioner_backend).dial(ident, db=db, rng=rng)
    finally:
        if abandoned.is_set():
            logger.info("console %s: a dial the relay had given up on has finished; its session is closed",
                        service.short(ident.id))


@router.websocket("/ws/console/{grant}")
async def console_relay(ws: WebSocket, grant: str) -> None:
    """Relay a VM's console to the signed-in owner of ``grant``. Not an HTTP operation: the
    published API contract lists the grant's issuing endpoints (``kind: rfb-ws``).

    No request-long database session: each step below takes one for as long as its statements
    run (``_call``), so many open consoles cost no connections."""
    app = ws.app
    subprotocol = ws_auth.SUBPROTOCOL if ws_auth.bearer_token(ws) else None
    with _session(app) as db:
        who = await ws_auth.ws_user(ws, db)
    if who is None:
        await ws.close(code=CLOSE_FORBIDDEN)
        return
    claimed = await _call(app, service.claim, grant, who.user)
    if claimed.outcome == service.DENIED:
        await ws.close(code=CLOSE_FORBIDDEN)
        return
    facts = claimed.facts  # plain values: what DialGrant and policy.check read
    ident = DialGrant(id=str(facts.id), range_id=str(facts.range_id), vm_id=facts.vm_id, vm_name=facts.vm_name,
                      view_only=bool(facts.view_only))
    grant_id, tenant_id, holder = facts.id, facts.tenant_id, claimed.holder
    if claimed.outcome == service.EXPIRED:
        await _refuse(ws, CLOSE_EXPIRED, "This console link has expired; open the console again", subprotocol)
        return
    if claimed.outcome == service.SPENT:
        await _refuse(ws, CLOSE_EXPIRED, "This console link has been used up; open the console again", subprotocol)
        return
    if claimed.outcome == service.BUSY:
        await _refuse(ws, CLOSE_BUSY, "This console is already open in another window", subprotocol)
        return

    upstream = None
    try:
        verdict = await _call(app, policy.check, facts, who.user)
        if not verdict.ok:
            await _give_back(app, grant_id, holder)
            await _refuse(ws, verdict.code, verdict.reason, subprotocol)
            return
        if not service.dial_allowed(who.user.id):
            await _give_back(app, grant_id, holder)
            await _refuse(ws, CLOSE_RATE, "Too many console connections; wait a minute", subprotocol)
            return
        abandoned = threading.Event()
        try:
            target = await asyncio.wait_for(
                run_in_threadpool(_dial_blocking, app, ident, tenant_id, abandoned), DIAL_SECONDS)
        except TimeoutError:
            abandoned.set()
            logger.warning("console %s: dial timed out after %ss", service.short(grant_id), DIAL_SECONDS)
            await _give_back(app, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_TIMEOUT, "The hypervisor did not answer in time", subprotocol)
            return
        except (ValueError, ConsoleError) as exc:
            logger.info("console %s: dial refused: %s", service.short(grant_id), type(exc).__name__)
            reason = str(exc) if isinstance(exc, ConsoleError) else "No console for this machine"
            await _give_back(app, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_REFUSED, reason[:120], subprotocol)
            return
        try:
            upstream = await _upstream_connect(target)
        except TimeoutError:
            await _give_back(app, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_TIMEOUT, "The hypervisor did not answer in time", subprotocol)
            return
        except Exception as exc:  # noqa: BLE001 — never put the exception text in a log or reason: it may carry the ticket URL
            logger.info("console %s: hypervisor connect failed: %s", service.short(grant_id), type(exc).__name__)
            await _give_back(app, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_REFUSED, "The hypervisor refused the console", subprotocol)
            return

        if not await _call(app, service.record_dial, grant_id, holder):
            # The claim lapsed during a slow dial and another socket took the grant.
            await _close_quietly(upstream)
            await _refuse(ws, CLOSE_BUSY, "This console was opened in another window", subprotocol)
            return
        await ws.accept(subprotocol=subprotocol)
        code, reason = await _pump(ws, upstream, app, facts, holder, who, verdict.until)
        await _give_back(app, grant_id, holder)
        with contextlib.suppress(RuntimeError):  # already closed by the browser
            await ws.close(code=code, reason=reason)
    finally:
        # Any way out, a cancelled task included (the shield lets these finish): the hypervisor
        # connection is closed, and the claim given back (idempotent; only its holder can clear it).
        await asyncio.shield(_clean_up(app, upstream, grant_id, holder))


async def _pump(ws: WebSocket, upstream: Any, app: Any, facts: Any, holder: str, who: Any,
                until: float | None) -> tuple[int, str]:
    """Move bytes both ways until either side closes; returns what to close the browser
    with. The hypervisor connection is closed on the way out, whichever side ended."""
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

    grant_id = facts.id
    last_bytes = time.monotonic()

    async def to_upstream() -> tuple[int, str]:
        nonlocal last_bytes
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return CLOSE_NORMAL, ""
            data = message.get("bytes")
            data = data if data is not None else message.get("text") or ""
            if len(data) > MAX_CLIENT_MESSAGE:
                return CLOSE_TOO_BIG, "Message too large"
            last_bytes = time.monotonic()
            await upstream.send(data)

    async def to_browser() -> tuple[int, str]:
        nonlocal last_bytes
        try:
            async for message in upstream:
                last_bytes = time.monotonic()
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)
        except ConnectionClosedError:
            return CLOSE_UPSTREAM_LOST, "The connection to the machine was lost"
        except ConnectionClosedOK:
            pass
        return CLOSE_NORMAL, "The console was closed"

    async def idle_watch() -> tuple[int, str]:
        while time.monotonic() - last_bytes < IDLE_SECONDS:
            await asyncio.sleep(min(5.0, max(0.05, IDLE_SECONDS - (time.monotonic() - last_bytes))))
        return CLOSE_IDLE, "The console was idle for too long"

    async def keep_claim() -> tuple[int, str]:
        """Every few seconds: renew the claim and ask ``policy`` again, each on a session that
        is gone again before the sleep. Ends the socket at the session window's end and at the
        sign-in's."""
        window = until
        failures = 0  # consecutive failed renew/check steps (the database, not a denial)
        last_ok = time.monotonic()
        lease = service.LIVE_LEASE.total_seconds()
        while True:
            ends = [e for e in (who.expires_at, window) if e is not None]
            wait = float(service.RENEW_SECONDS)
            if ends:
                wait = min(wait, max(min(ends) - time.time(), 0.0))
            await asyncio.sleep(wait)
            now = time.time()
            if who.expires_at is not None and now >= who.expires_at:
                return CLOSE_TOKEN_EXPIRED, "Your sign-in expired; reopen the console"
            if window is not None and now >= window:
                return CLOSE_FORBIDDEN, "Your session has ended"
            try:
                renewed = await _call(app, service.renew, grant_id, holder)
                if renewed == service.GONE:
                    return CLOSE_GONE, "This console was withdrawn"
                if renewed == service.LOST:
                    return CLOSE_BUSY, "This console was opened in another window"
                verdict = await _call(app, policy.check, facts, who.user)
                failures = 0
                last_ok = time.monotonic()
            except Exception as exc:  # noqa: BLE001 — the database, not the Student's doing
                failures += 1
                logger.warning("console %s: renewal failed (%d in a row): %s", service.short(grant_id),
                               failures, type(exc).__name__)
                # A transient failure (a pool timeout) is tolerated twice; the third, or one
                # after which the next tick would find the claim lapsed, ends the socket.
                if failures >= MAX_RENEW_FAILURES or time.monotonic() - last_ok + service.RENEW_SECONDS >= lease:
                    return CLOSE_SERVICE, "The console service had a problem. Reconnect."
                continue
            if not verdict.ok:
                return verdict.code, verdict.reason
            window = verdict.until

    tasks = [asyncio.ensure_future(f()) for f in (to_upstream, to_browser, keep_claim, idle_watch)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        first = next(iter(done))
        try:
            return first.result()
        except (WebSocketDisconnect, RuntimeError):  # the browser went away mid-send
            return CLOSE_NORMAL, ""
        except Exception as exc:  # noqa: BLE001 — upstream send failed, etc.
            logger.info("console %s: relay ended: %s", service.short(grant_id), type(exc).__name__)
            return CLOSE_UPSTREAM_LOST, "The connection to the machine was lost"
    finally:
        for t in tasks:
            t.cancel()
        await _close_quietly(upstream)
        await asyncio.gather(*tasks, return_exceptions=True)
