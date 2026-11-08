"""The webhook notification channel (app/notifications/webhook.py).

Notifications can go out as a signed HTTP POST: JSON payload, HMAC-SHA256 signature in
``X-Signature-256``, up to three attempts with exponential backoff. The HTTP endpoint is
the external system here, so the tests intercept it with respx and check what is actually
put on the wire — the signed bytes, the header, the retry count — while the backoff base
is patched to a fraction of a second so the retries run at test speed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from app.notifications import webhook
from app.notifications.webhook import WebhookChannel

URL = "http://hooks.internal/truenorth"


def _channel(monkeypatch, secret: str = "shhh") -> WebhookChannel:
    monkeypatch.setenv("WEBHOOK_URL", URL)
    monkeypatch.setenv("WEBHOOK_SECRET", secret)
    monkeypatch.setattr(webhook, "_BACKOFF_BASE", 0.001)  # real backoff, at test speed
    return WebhookChannel()


# ── configuration and signing ──────────────────────────────────────────
def test_an_unconfigured_channel_reads_an_empty_url_and_a_default_timeout(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "")
    monkeypatch.delenv("WEBHOOK_TIMEOUT", raising=False)
    channel = WebhookChannel()
    assert channel.url == "" and channel.timeout == 10.0


def test_the_timeout_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", URL)
    monkeypatch.setenv("WEBHOOK_TIMEOUT", "2.5")
    assert WebhookChannel().timeout == 2.5


def test_the_signer_returns_none_without_a_secret_and_hmac_with_one(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", URL)
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    unsigned = WebhookChannel()
    assert unsigned._sign(b"payload") is None

    monkeypatch.setenv("WEBHOOK_SECRET", "shhh")
    sig = WebhookChannel()._sign(b"payload")
    assert sig == hmac.new(b"shhh", b"payload", hashlib.sha256).hexdigest()


# ── delivery over the wire ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unconfigured_channel_sends_nothing(monkeypatch, respx_mock):
    monkeypatch.setenv("WEBHOOK_URL", "")
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200))
    assert await WebhookChannel().send("a@b.c", "Subject", "Body") is False
    assert route.called is False


@pytest.mark.asyncio
async def test_the_payload_is_posted_signed_with_hmac_sha256(monkeypatch, respx_mock):
    channel = _channel(monkeypatch)
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200))

    assert await channel.send(
        "learner@example.test", "Lab ready", "Your lab is up.", metadata={"exercise": "EX-9"}
    ) is True

    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["recipient"] == "learner@example.test"
    assert payload["subject"] == "Lab ready"
    assert payload["body"] == "Your lab is up."
    assert payload["metadata"] == {"exercise": "EX-9"}
    assert isinstance(payload["timestamp"], float)
    expected = hmac.new(b"shhh", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Signature-256"] == f"sha256={expected}"
    assert request.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_without_a_secret_the_payload_goes_unsigned(monkeypatch, respx_mock):
    channel = _channel(monkeypatch, secret="")
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200))
    assert await channel.send("a@b.c", "Subject", "Body") is True
    assert "X-Signature-256" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_until_it_succeeds(monkeypatch, respx_mock):
    channel = _channel(monkeypatch)
    route = respx_mock.post(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(503), httpx.Response(200)]
    )
    assert await channel.send("a@b.c", "Subject", "Body") is True
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_after_the_last_attempt_the_send_fails(monkeypatch, respx_mock):
    channel = _channel(monkeypatch)
    route = respx_mock.post(URL).mock(return_value=httpx.Response(500))
    assert await channel.send("a@b.c", "Subject", "Body") is False
    assert route.call_count == 3  # _MAX_RETRIES, no more
