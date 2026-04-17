"""TrueNorth Range SDK — async-first Python client.

Quick start (async)::

    from truenorth_sdk import TrueNorthClient

    async with TrueNorthClient("https://range.example.com", api_key="k-...") as tn:
        ranges = await tn.list_ranges()

Quick start (sync)::

    from truenorth_sdk import TrueNorthClient

    with TrueNorthClient.Sync("https://range.example.com", api_key="k-...") as tn:
        ranges = tn.list_ranges()
"""

from .client import (
    AuthError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TimeoutError,
    TrueNorthClient,
    TrueNorthError,
    ValidationError,
)

__all__ = [
    "TrueNorthClient",
    "TrueNorthError",
    "AuthError",
    "NotFoundError",
    "ValidationError",
    "RateLimitError",
    "ConflictError",
    "ServerError",
    "TimeoutError",
]

__version__ = "1.0.0"
