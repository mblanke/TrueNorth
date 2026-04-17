"""TrueNorth Range notification system.

Re-exports the original service API and adds channel implementations.
"""

# Original notification service API (backward-compat)
from ._service import (
    Notification,
    NotificationChannel,
    NotificationLevel,
    NotificationService,
)
from .in_app import InAppChannel
from .registry import get_channel, register_channel

# New channel implementations
from .smtp import SMTPChannel
from .webhook import WebhookChannel

__all__ = [
    # Original
    "Notification",
    "NotificationChannel",
    "NotificationLevel",
    "NotificationService",
    # New channels
    "SMTPChannel",
    "WebhookChannel",
    "InAppChannel",
    "get_channel",
    "register_channel",
]
