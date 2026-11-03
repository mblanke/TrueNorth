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
4404  The machine is gone: the range was deleted or is not running.
4409  This console grant is already open in another socket.
4410  The grant expired (about five minutes after issue); ask for a new console.
4502  The hypervisor did not give a console (refused the ticket, or was unreachable).
4503  The connection to the hypervisor dropped while the console was open.
4504  The hypervisor did not answer in time.
====  ================================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, WebSocket
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from .. import ws_auth
from ..classes.consoles import USABLE
from ..console_backends import ConsoleError, DialGrant, get_console_backend
from ..db import get_db
from ..models import Range
from . import service

logger = logging.getLogger("truenorth.console_relay")

router = APIRouter()

CLOSE_FORBIDDEN, CLOSE_NORMAL, CLOSE_TOKEN_EXPIRED = 1008, 1000, 1001
CLOSE_GONE, CLOSE_BUSY, CLOSE_EXPIRED = 4404, 4409, 4410
CLOSE_UPSTREAM_REFUSED, CLOSE_UPSTREAM_LOST, CLOSE_UPSTREAM_TIMEOUT = 4502, 4503, 4504
#: Longest the hypervisor may take to hand over a ticket and accept the connection.
DIAL_SECONDS = 15
#: A single RFB message (a framebuffer update) can be large; bound it, not the stream.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


async def _refuse(ws: WebSocket, code: int, reason: str, subprotocol: str | None) -> None:
    """Close after the handshake, so the browser sees the code and reason (before it, a
    browser only sees a failed connection)."""
    await ws.accept(subprotocol=subprotocol)
    await ws.close(code=code, reason=reason)


async def _close_quietly(upstream: Any) -> None:
    with contextlib.suppress(Exception):  # already gone
        await upstream.close()


async def _clean_up(upstream: Any, db: Session, grant_id: uuid.UUID, holder: str) -> None:
    if upstream is not None:
        await _close_quietly(upstream)
    await _give_back(db, grant_id, holder)


async def _give_back(db: Session, grant_id: uuid.UUID, holder: str) -> None:
    """Free the grant's one socket. Done before the closing frame goes out, so a browser that
    reconnects the moment it sees the close does not meet its own previous socket."""
    await run_in_threadpool(service.release, db, grant_id, holder)


async def _upstream_connect(target: Any):
    """The hypervisor's side: one WebSocket to ``target.url`` with its TLS and subprotocols."""
    from websockets.asyncio.client import connect

    kwargs: dict[str, Any] = {"subprotocols": list(target.subprotocols), "open_timeout": DIAL_SECONDS,
                              "max_size": MAX_MESSAGE_BYTES, "compression": None}
    if target.url.startswith("wss://"):
        kwargs["ssl"] = target.ssl if target.ssl is not None else ssl.create_default_context()
    return await connect(target.url, **kwargs)


@router.websocket("/ws/console/{grant}")
async def console_relay(ws: WebSocket, grant: str, db: Session = Depends(get_db)) -> None:
    """Relay a VM's console to the signed-in owner of ``grant``. Not an HTTP operation: the
    published API contract lists the grant's issuing endpoints (``kind: rfb-ws``)."""
    subprotocol = ws_auth.SUBPROTOCOL if ws_auth.bearer_token(ws) else None
    who = await ws_auth.ws_user(ws, db)
    if who is None:
        await ws.close(code=CLOSE_FORBIDDEN)
        return
    claimed = await run_in_threadpool(service.claim, db, grant, who.user)
    if claimed.outcome == service.DENIED:
        await ws.close(code=CLOSE_FORBIDDEN)
        return
    row = claimed.grant
    ident = DialGrant(id=str(row.id), range_id=str(row.range_id), vm_id=row.vm_id, vm_name=row.vm_name,
                      view_only=bool(row.view_only))
    grant_id, tenant_id, holder = row.id, row.tenant_id, claimed.holder
    if claimed.outcome == service.EXPIRED:
        await _refuse(ws, CLOSE_EXPIRED, "This console link has expired; open the console again", subprotocol)
        return
    if claimed.outcome == service.BUSY:
        await _refuse(ws, CLOSE_BUSY, "This console is already open in another window", subprotocol)
        return

    upstream = None
    try:
        # tenant-safe: the grant's own range, in the grant's tenant (the user's, checked by claim).
        rng = (db.query(Range)
               .filter(Range.id == uuid.UUID(ident.range_id), Range.tenant_id == tenant_id,
                       Range.deleted_at.is_(None))
               .first())
        if rng is None or rng.state not in USABLE:
            await _give_back(db, grant_id, holder)
            await _refuse(ws, CLOSE_GONE, "The machine is not running", subprotocol)
            return
        try:
            backend = get_console_backend(rng.provisioner_backend)
            target = await asyncio.wait_for(
                run_in_threadpool(backend.dial, ident, db=db, rng=rng), DIAL_SECONDS)
        except TimeoutError:
            await _give_back(db, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_TIMEOUT, "The hypervisor did not answer in time", subprotocol)
            return
        except (ValueError, ConsoleError) as exc:
            logger.info("console %s: dial refused: %s", service.short(grant_id), type(exc).__name__)
            reason = str(exc) if isinstance(exc, ConsoleError) else "No console for this machine"
            await _give_back(db, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_REFUSED, reason[:120], subprotocol)
            return
        try:
            upstream = await _upstream_connect(target)
        except TimeoutError:
            await _give_back(db, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_TIMEOUT, "The hypervisor did not answer in time", subprotocol)
            return
        except Exception as exc:  # noqa: BLE001 — never put the exception text in a log or reason: it may carry the ticket URL
            logger.info("console %s: hypervisor connect failed: %s", service.short(grant_id), type(exc).__name__)
            await _give_back(db, grant_id, holder)
            await _refuse(ws, CLOSE_UPSTREAM_REFUSED, "The hypervisor refused the console", subprotocol)
            return

        await ws.accept(subprotocol=subprotocol)
        code, reason = await _pump(ws, upstream, db, grant_id, holder, who.expires_at)
        await _give_back(db, grant_id, holder)
        with contextlib.suppress(RuntimeError):  # already closed by the browser
            await ws.close(code=code, reason=reason)
    finally:
        # Any way out, a cancelled task included (the shield lets these finish): the hypervisor
        # connection is closed, and the claim given back (idempotent; only its holder can clear it).
        await asyncio.shield(_clean_up(upstream, db, grant_id, holder))


async def _pump(ws: WebSocket, upstream: Any, db: Session, grant_id: uuid.UUID, holder: str,
                token_expires_at: float | None) -> tuple[int, str]:
    """Move bytes both ways until either side closes; returns what to close the browser
    with. The hypervisor connection is closed on the way out, whichever side ended."""
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

    async def to_upstream() -> tuple[int, str]:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return CLOSE_NORMAL, ""
            data = message.get("bytes")
            await upstream.send(data if data is not None else message.get("text") or "")

    async def to_browser() -> tuple[int, str]:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)
        except ConnectionClosedError:
            return CLOSE_UPSTREAM_LOST, "The connection to the machine was lost"
        except ConnectionClosedOK:
            pass
        return CLOSE_NORMAL, "The console was closed"

    async def keep_claim() -> tuple[int, str]:
        deadline = None if token_expires_at is None else token_expires_at - time.time()
        waited = 0.0
        while deadline is None or waited < deadline:
            step = service.RENEW_SECONDS if deadline is None else min(service.RENEW_SECONDS, deadline - waited)
            await asyncio.sleep(max(step, 0))
            waited += step
            if not await run_in_threadpool(service.renew, db, grant_id, holder):
                return CLOSE_BUSY, "This console was opened in another window"
        return CLOSE_TOKEN_EXPIRED, "Your sign-in expired; reopen the console"

    tasks = [asyncio.ensure_future(f()) for f in (to_upstream, to_browser, keep_claim)]
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
