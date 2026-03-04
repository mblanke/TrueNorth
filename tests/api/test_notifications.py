"""Tests for the TrueNorth Range notification system."""

import pytest
from app.notifications import (
    Notification,
    NotificationChannel,
    NotificationLevel,
    NotificationService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notification(**overrides) -> Notification:
    defaults = dict(
        title="Range Ready",
        message="Your range r-123 is now available.",
        level="info",
        channels=[NotificationChannel.IN_APP],
        recipient_user_id="user-1",
        recipient_tenant_id="tenant-1",
    )
    defaults.update(overrides)
    return Notification(**defaults)


def _service(**kw) -> NotificationService:
    return NotificationService(**kw)


# ---------------------------------------------------------------------------
# Notification dataclass
# ---------------------------------------------------------------------------

class TestNotification:
    def test_defaults(self):
        n = _make_notification()
        assert n.id  # UUID
        assert n.created_at
        assert n.data == {}

    def test_to_dict(self):
        n = _make_notification()
        d = n.to_dict()
        assert d["title"] == "Range Ready"
        assert d["channels"] == ["in_app"]

    def test_multiple_channels(self):
        n = _make_notification(
            channels=[NotificationChannel.WEBSOCKET, NotificationChannel.EMAIL]
        )
        assert len(n.channels) == 2


class TestNotificationLevel:
    def test_all_levels(self):
        assert NotificationLevel.INFO.value == "info"
        assert NotificationLevel.WARNING.value == "warning"
        assert NotificationLevel.ERROR.value == "error"
        assert NotificationLevel.SUCCESS.value == "success"


# ---------------------------------------------------------------------------
# NotificationService.send
# ---------------------------------------------------------------------------

class TestSendNotification:
    @pytest.mark.asyncio
    async def test_send_websocket(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.WEBSOCKET])
        result = await svc.send(n)
        assert result["websocket"] == "sent"

    @pytest.mark.asyncio
    async def test_send_email_no_smtp(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.EMAIL])
        result = await svc.send(n)
        assert result["email"] == "sent"  # logs warning but doesn't raise

    @pytest.mark.asyncio
    async def test_send_email_with_smtp_config(self):
        svc = _service(smtp_config={"host": "smtp.test.local", "port": 587})
        n = _make_notification(channels=[NotificationChannel.EMAIL])
        result = await svc.send(n)
        assert result["email"] == "sent"

    @pytest.mark.asyncio
    async def test_send_webhook(self):
        svc = _service(webhook_urls=["https://hooks.example.com/abc"])
        n = _make_notification(channels=[NotificationChannel.WEBHOOK])
        result = await svc.send(n)
        assert result["webhook"] == "sent"

    @pytest.mark.asyncio
    async def test_send_webhook_with_extra_url(self):
        svc = _service()
        n = _make_notification(
            channels=[NotificationChannel.WEBHOOK],
            data={"webhook_url": "https://custom.hook/x"},
        )
        result = await svc.send(n)
        assert result["webhook"] == "sent"

    @pytest.mark.asyncio
    async def test_send_in_app(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.IN_APP])
        result = await svc.send(n)
        assert result["in_app"] == "sent"

    @pytest.mark.asyncio
    async def test_send_multi_channel(self):
        svc = _service()
        n = _make_notification(
            channels=[
                NotificationChannel.WEBSOCKET,
                NotificationChannel.IN_APP,
            ]
        )
        result = await svc.send(n)
        assert result["websocket"] == "sent"
        assert result["in_app"] == "sent"

    @pytest.mark.asyncio
    async def test_send_to_role(self):
        svc = _service()
        n = _make_notification(
            recipient_user_id=None,
            recipient_role="admin",
            channels=[NotificationChannel.WEBSOCKET],
        )
        result = await svc.send(n)
        assert result["websocket"] == "sent"


# ---------------------------------------------------------------------------
# In-app storage and retrieval
# ---------------------------------------------------------------------------

class TestInAppNotifications:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.IN_APP])
        await svc.send(n)
        items = await svc.get_user_notifications("user-1")
        assert len(items) == 1
        assert items[0]["title"] == "Range Ready"
        assert items[0]["read"] is False

    @pytest.mark.asyncio
    async def test_multiple_notifications(self):
        svc = _service()
        for i in range(5):
            n = _make_notification(
                title=f"Notif {i}",
                channels=[NotificationChannel.IN_APP],
            )
            await svc.send(n)
        items = await svc.get_user_notifications("user-1")
        assert len(items) == 5
        # Most recent first
        assert items[0]["title"] == "Notif 4"

    @pytest.mark.asyncio
    async def test_limit(self):
        svc = _service()
        for i in range(10):
            await svc.send(
                _make_notification(title=f"N-{i}", channels=[NotificationChannel.IN_APP])
            )
        items = await svc.get_user_notifications("user-1", limit=3)
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_mark_read(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.IN_APP])
        await svc.send(n)
        found = await svc.mark_read(n.id, "user-1")
        assert found is True
        items = await svc.get_user_notifications("user-1")
        assert items[0]["read"] is True

    @pytest.mark.asyncio
    async def test_mark_read_not_found(self):
        svc = _service()
        found = await svc.mark_read("nonexistent", "user-1")
        assert found is False

    @pytest.mark.asyncio
    async def test_mark_all_read(self):
        svc = _service()
        for _ in range(3):
            await svc.send(
                _make_notification(channels=[NotificationChannel.IN_APP])
            )
        count = await svc.mark_all_read("user-1")
        assert count == 3
        assert await svc.get_unread_count("user-1") == 0

    @pytest.mark.asyncio
    async def test_unread_count(self):
        svc = _service()
        for _ in range(4):
            await svc.send(
                _make_notification(channels=[NotificationChannel.IN_APP])
            )
        assert await svc.get_unread_count("user-1") == 4
        items = await svc.get_user_notifications("user-1")
        await svc.mark_read(items[0]["id"], "user-1")
        assert await svc.get_unread_count("user-1") == 3

    @pytest.mark.asyncio
    async def test_unread_count_empty(self):
        svc = _service()
        assert await svc.get_unread_count("user-999") == 0

    @pytest.mark.asyncio
    async def test_delete_notification(self):
        svc = _service()
        n = _make_notification(channels=[NotificationChannel.IN_APP])
        await svc.send(n)
        deleted = await svc.delete_notification(n.id, "user-1")
        assert deleted is True
        items = await svc.get_user_notifications("user-1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_in_app_skips_no_user_id(self):
        svc = _service()
        n = _make_notification(
            recipient_user_id=None,
            channels=[NotificationChannel.IN_APP],
        )
        result = await svc.send(n)
        assert result["in_app"] == "sent"  # does not raise