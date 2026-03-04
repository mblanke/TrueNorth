"""Channel registry — maps channel type names to channel instances."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import NotificationChannel

logger = logging.getLogger(__name__)

_registry: dict[str, type[NotificationChannel]] = {}


def register_channel(name: str, cls: type[NotificationChannel]) -> None:
    """Register a channel class under the given name."""
    _registry[name] = cls
    logger.debug("Registered notification channel: %s -> %s", name, cls.__name__)


def get_channel(name: str) -> NotificationChannel:
    """Instantiate and return a channel by its registered name.

    Raises ``KeyError`` if the channel name is not registered.
    """
    cls = _registry[name]
    return cls()


def list_channels() -> list[str]:
    """Return the names of all registered channels."""
    return list(_registry.keys())


# -- Default registrations ------------------------------------------------
def _register_defaults() -> None:
    from .smtp import SMTPChannel
    from .webhook import WebhookChannel
    from .in_app import InAppChannel

    register_channel("email", SMTPChannel)
    register_channel("webhook", WebhookChannel)
    register_channel("in_app", InAppChannel)


_register_defaults()