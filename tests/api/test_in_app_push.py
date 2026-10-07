"""An in-app notification is pushed to the recipient's own sockets, and nobody else's.

``InAppChannel.send`` called ``ws_manager.send_to_user(recipient, {...})`` with two
arguments; ``WebSocketManager.send_to_user`` takes (user_id, message_type, data). The
TypeError was caught and logged, so the push silently never happened (security review
of #32). Note: nothing in the API constructs InAppChannel with a WebSocket manager yet
(the notifications package is not wired in), so this fixes the channel for when it is.
"""

from __future__ import annotations

import pytest
from app.notifications.in_app import InAppChannel
from app.websocket_manager import WebSocketManager


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def accept(self, subprotocol=None):
        pass

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_the_recipient_gets_the_notification_and_nobody_else_does():
    m = WebSocketManager()
    mine, mine_too, theirs = FakeSocket(), FakeSocket(), FakeSocket()
    await m.connect(mine, "ranges", user_id="user-1", tenant_id="t1")
    await m.connect(mine_too, "ranges", user_id="user-1", tenant_id="t1")  # a second tab
    await m.connect(theirs, "ranges", user_id="user-2", tenant_id="t1")

    assert await InAppChannel(ws_manager=m).send("user-1", "Exercise starts", "In 10 minutes", {"exercise": "e1"})

    for sock in (mine, mine_too):
        (msg,) = sock.sent
        assert msg["type"] == "notification" and msg["channel"] == "user.user-1"
        data = msg["data"]
        assert (data["subject"], data["body"], data["metadata"]) == (
            "Exercise starts",
            "In 10 minutes",
            {"exercise": "e1"},
        )
        assert data["id"] and data["created_at"]
    assert theirs.sent == []


@pytest.mark.asyncio
async def test_no_manager_no_push_and_no_error():
    assert await InAppChannel().send("user-1", "s", "b") is True
