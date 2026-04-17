"""TrueNorth Range — Redis connection helper with Sentinel HA support.

Provides a unified ``get_redis_client()`` that returns either:
- A standard Redis client (for dev / single-node)
- A Sentinel-backed client (for HA production)

Configuration (environment variables):
    REDIS_URL               Standard Redis URL (default: redis://localhost:6379/0)
    REDIS_SENTINEL_HOSTS    Comma-separated sentinel addresses (e.g. "host1:26379,host2:26379")
    REDIS_SENTINEL_MASTER   Sentinel master name (default: truenorth-master)
    REDIS_SENTINEL_PASSWORD Sentinel auth password (optional)
    REDIS_PASSWORD          Redis auth password (optional)
    REDIS_DB               Database number (default: 0)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import redis
import redis.asyncio as aioredis

logger = logging.getLogger("truenorth.redis")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SENTINEL_HOSTS = os.getenv("REDIS_SENTINEL_HOSTS", "")
SENTINEL_MASTER = os.getenv("REDIS_SENTINEL_MASTER", "truenorth-master")
SENTINEL_PASSWORD = os.getenv("REDIS_SENTINEL_PASSWORD", "")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", "0"))


def _parse_sentinel_hosts() -> list[tuple[str, int]]:
    """Parse comma-separated host:port pairs."""
    if not SENTINEL_HOSTS:
        return []
    hosts = []
    for entry in SENTINEL_HOSTS.split(","):
        entry = entry.strip()
        if ":" in entry:
            host, port = entry.rsplit(":", 1)
            hosts.append((host.strip(), int(port)))
        else:
            hosts.append((entry, 26379))
    return hosts


def get_redis_client(decode_responses: bool = True) -> redis.Redis:
    """Create a synchronous Redis client (Sentinel-aware if configured)."""
    sentinel_hosts = _parse_sentinel_hosts()

    if sentinel_hosts:
        sentinel_kwargs: dict[str, Any] = {}
        if SENTINEL_PASSWORD:
            sentinel_kwargs["password"] = SENTINEL_PASSWORD

        sentinel = redis.Sentinel(
            sentinel_hosts,
            socket_timeout=2.0,
            sentinel_kwargs=sentinel_kwargs,
        )
        client = sentinel.master_for(
            SENTINEL_MASTER,
            db=REDIS_DB,
            password=REDIS_PASSWORD or None,
            decode_responses=decode_responses,
        )
        logger.info(
            "Redis Sentinel client created: master=%s sentinels=%d",
            SENTINEL_MASTER,
            len(sentinel_hosts),
        )
        return client

    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=decode_responses,
        socket_connect_timeout=2,
        retry_on_timeout=True,
    )
    logger.info("Redis standard client created: %s", REDIS_URL)
    return client


def get_async_redis_client(decode_responses: bool = False) -> aioredis.Redis:
    """Create an async Redis client (Sentinel-aware if configured)."""
    sentinel_hosts = _parse_sentinel_hosts()

    if sentinel_hosts:
        sentinel_kwargs: dict[str, Any] = {}
        if SENTINEL_PASSWORD:
            sentinel_kwargs["password"] = SENTINEL_PASSWORD

        sentinel = aioredis.Sentinel(
            sentinel_hosts,
            socket_timeout=2.0,
            sentinel_kwargs=sentinel_kwargs,
        )
        client = sentinel.master_for(
            SENTINEL_MASTER,
            db=REDIS_DB,
            password=REDIS_PASSWORD or None,
            decode_responses=decode_responses,
        )
        logger.info(
            "Async Redis Sentinel client created: master=%s sentinels=%d",
            SENTINEL_MASTER,
            len(sentinel_hosts),
        )
        return client

    client = aioredis.from_url(
        REDIS_URL,
        decode_responses=decode_responses,
        socket_connect_timeout=2,
        retry_on_timeout=True,
    )
    logger.info("Async Redis standard client created: %s", REDIS_URL)
    return client


def get_sentinel_broker_url() -> str:
    """Return a Celery-compatible broker URL for Sentinel mode.

    Celery supports ``sentinel://:password@host1:port1;host2:port2/db``
    format for Sentinel failover.
    """
    sentinel_hosts = _parse_sentinel_hosts()
    if not sentinel_hosts:
        return REDIS_URL

    hosts_str = ";".join(f"{h}:{p}" for h, p in sentinel_hosts)
    password_part = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
    url = f"sentinel://{password_part}{hosts_str}/{REDIS_DB}"
    logger.info("Celery Sentinel broker URL: sentinel://.../%d", REDIS_DB)
    return url
