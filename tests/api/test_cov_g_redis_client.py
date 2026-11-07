"""Round-3 coverage for app/redis_client.py (whole-slice rank: 0% before this file).

The Sentinel-aware Redis helper is imported by nothing the slice's tests touch, so its
59 statements were all unexecuted. These tests show the real configuration surface —
host parsing, the standard vs Sentinel client choice, credential/db plumbing into the
connection pools and the Celery broker URL — by pointing the module's own env-derived
settings at throwaway names and inspecting the clients it builds. Nothing here connects:
redis clients are lazy, so building one exercises the factory without a live Redis.
"""

from __future__ import annotations

import logging

import pytest
import redis
import redis.asyncio as aioredis
from app import redis_client as rc

LOGGER = "truenorth.redis"


@pytest.fixture
def standalone(monkeypatch):
    """No sentinels configured: the factory must fall back to a plain client."""
    monkeypatch.setattr(rc, "SENTINEL_HOSTS", "")
    return monkeypatch


# -- sentinel host parsing ---------------------------------------------------------------------


def test_no_sentinels_parses_to_an_empty_list():
    assert rc._parse_sentinel_hosts() == []


def test_sentinel_hosts_parse_with_and_without_ports():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rc, "SENTINEL_HOSTS", " s1.example:26380 , s2.example ")
        assert rc._parse_sentinel_hosts() == [("s1.example", 26380), ("s2.example", 26379)]


# -- the standard (non-Sentinel) client --------------------------------------------------------


def test_the_standard_client_keeps_the_url_decode_and_timeout_settings(standalone, monkeypatch):
    monkeypatch.setattr(rc, "REDIS_URL", "redis://cache.internal:6380/7")

    client = rc.get_redis_client()

    assert isinstance(client, redis.Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["host"] == "cache.internal" and kwargs["port"] == 6380 and kwargs["db"] == 7
    assert kwargs["decode_responses"] is True
    assert kwargs["retry_on_timeout"] is True and kwargs["socket_connect_timeout"] == 2

    raw = rc.get_redis_client(decode_responses=False)
    assert raw.connection_pool.connection_kwargs["decode_responses"] is False


def test_an_async_standard_client_defaults_to_bytes(standalone, monkeypatch):
    monkeypatch.setattr(rc, "REDIS_URL", "redis://cache.internal:6380/2")

    client = rc.get_async_redis_client()

    assert isinstance(client, aioredis.Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["decode_responses"] is False and kwargs["db"] == 2


# -- the Sentinel client -----------------------------------------------------------------------


def test_configured_sentinels_build_a_master_client_with_the_realm_credentials(
    monkeypatch, caplog
):
    monkeypatch.setattr(rc, "SENTINEL_HOSTS", "s1.example:26380,s2.example")
    monkeypatch.setattr(rc, "SENTINEL_MASTER", "range-master")
    monkeypatch.setattr(rc, "SENTINEL_PASSWORD", "sentinel-secret")
    monkeypatch.setattr(rc, "REDIS_PASSWORD", "data-secret")
    monkeypatch.setattr(rc, "REDIS_DB", 3)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        client = rc.get_redis_client()

    assert isinstance(client, redis.Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["password"] == "data-secret" and kwargs["db"] == 3
    assert kwargs["decode_responses"] is True
    # the sentinels themselves authenticate with the sentinel password, not the data one
    assert client.connection_pool.service_name == "range-master"
    sentinel_conns = client.connection_pool.sentinel_manager.sentinels
    assert {
        (s.connection_pool.connection_kwargs["host"], s.connection_pool.connection_kwargs["port"])
        for s in sentinel_conns
    } == {("s1.example", 26380), ("s2.example", 26379)}
    assert all(s.connection_pool.connection_kwargs["password"] == "sentinel-secret" for s in sentinel_conns)
    assert any("range-master" in r.message and "sentinels=2" in r.message for r in caplog.records), caplog.text


def test_an_async_sentinel_client_is_built_the_same_way(monkeypatch):
    monkeypatch.setattr(rc, "SENTINEL_HOSTS", "s1.example,s2.example:26381")
    monkeypatch.setattr(rc, "SENTINEL_MASTER", "range-master")
    monkeypatch.setattr(rc, "REDIS_DB", 5)

    client = rc.get_async_redis_client()

    assert isinstance(client, aioredis.Redis)
    assert client.connection_pool.service_name == "range-master"
    assert client.connection_pool.connection_kwargs["db"] == 5
    sentinel_conns = client.connection_pool.sentinel_manager.sentinels
    assert {
        (s.connection_pool.connection_kwargs["host"], s.connection_pool.connection_kwargs["port"])
        for s in sentinel_conns
    } == {("s1.example", 26379), ("s2.example", 26381)}
    # no sentinel password configured: the sentinels connect without one
    assert all(s.connection_pool.connection_kwargs["password"] is None for s in sentinel_conns)


# -- the Celery broker URL ---------------------------------------------------------------------


def test_without_sentinels_the_broker_url_is_the_plain_redis_url(standalone, monkeypatch):
    monkeypatch.setattr(rc, "REDIS_URL", "redis://cache.internal:6380/7")
    assert rc.get_sentinel_broker_url() == "redis://cache.internal:6380/7"


def test_with_sentinels_the_broker_url_lists_them_behind_the_master_db(monkeypatch):
    monkeypatch.setattr(rc, "SENTINEL_HOSTS", "s1.example:26380, s2.example")
    monkeypatch.setattr(rc, "REDIS_PASSWORD", "broker-secret")
    monkeypatch.setattr(rc, "REDIS_DB", 3)

    assert rc.get_sentinel_broker_url() == "sentinel://:broker-secret@s1.example:26380;s2.example:26379/3"

    monkeypatch.setattr(rc, "REDIS_PASSWORD", "")
    assert rc.get_sentinel_broker_url() == "sentinel://s1.example:26380;s2.example:26379/3"
