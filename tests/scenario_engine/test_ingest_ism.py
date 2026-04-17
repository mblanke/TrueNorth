"""Unit tests for OpenSearch ISM policy and index template setup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipeline.ingest import (
    INDEX_PREFIX,
    ISM_POLICY_BODY,
    ISM_POLICY_ID,
    _ensure_index_template,
    _ensure_ism_policy,
)


@pytest.fixture
def os_client():
    client = AsyncMock()
    client.transport = MagicMock()
    client.transport.perform_request = AsyncMock()
    return client


class TestEnsureIsmPolicy:
    @pytest.mark.asyncio
    async def test_creates_policy_when_missing(self, os_client):
        """Should PUT the ISM policy when GET returns 404."""
        os_client.transport.perform_request.side_effect = [
            Exception("Not Found"),  # GET fails
            {"_id": ISM_POLICY_ID},  # PUT succeeds
        ]
        await _ensure_ism_policy(os_client)
        calls = os_client.transport.perform_request.call_args_list
        assert calls[0].args == ("GET", f"/_plugins/_ism/policies/{ISM_POLICY_ID}")
        assert calls[1].args == ("PUT", f"/_plugins/_ism/policies/{ISM_POLICY_ID}")
        assert calls[1].kwargs["body"] == ISM_POLICY_BODY

    @pytest.mark.asyncio
    async def test_skips_when_policy_exists(self, os_client):
        """Should not PUT if GET succeeds (policy exists)."""
        os_client.transport.perform_request.return_value = {"_id": ISM_POLICY_ID}
        await _ensure_ism_policy(os_client)
        os_client.transport.perform_request.assert_called_once_with("GET", f"/_plugins/_ism/policies/{ISM_POLICY_ID}")

    @pytest.mark.asyncio
    async def test_continues_on_put_failure(self, os_client):
        """Should log warning and continue if PUT fails."""
        os_client.transport.perform_request.side_effect = [
            Exception("Not Found"),
            Exception("Connection refused"),
        ]
        # Should not raise
        await _ensure_ism_policy(os_client)


class TestEnsureIndexTemplate:
    @pytest.mark.asyncio
    async def test_creates_template(self, os_client):
        """Should PUT the index template."""
        os_client.transport.perform_request.return_value = {"acknowledged": True}
        await _ensure_index_template(os_client)
        call = os_client.transport.perform_request.call_args
        assert call.args[0] == "PUT"
        assert f"{INDEX_PREFIX}-template" in call.args[1]
        body = call.kwargs["body"]
        assert body["index_patterns"] == [f"{INDEX_PREFIX}-*"]
        assert "mappings" in body["template"]
        assert "timestamp" in body["template"]["mappings"]["properties"]

    @pytest.mark.asyncio
    async def test_continues_on_failure(self, os_client):
        """Should not raise if template creation fails."""
        os_client.transport.perform_request.side_effect = Exception("Connection refused")
        await _ensure_index_template(os_client)


class TestIsmPolicyStructure:
    def test_policy_has_four_states(self):
        states = ISM_POLICY_BODY["policy"]["states"]
        assert len(states) == 4
        assert [s["name"] for s in states] == ["hot", "warm", "cold", "delete"]

    def test_hot_transitions_to_warm_at_7d(self):
        hot = ISM_POLICY_BODY["policy"]["states"][0]
        assert hot["transitions"][0]["conditions"]["min_index_age"] == "7d"

    def test_warm_transitions_to_cold_at_30d(self):
        warm = ISM_POLICY_BODY["policy"]["states"][1]
        assert warm["transitions"][0]["conditions"]["min_index_age"] == "30d"

    def test_cold_transitions_to_delete_at_365d(self):
        cold = ISM_POLICY_BODY["policy"]["states"][2]
        assert cold["transitions"][0]["conditions"]["min_index_age"] == "365d"

    def test_delete_has_no_transitions(self):
        delete = ISM_POLICY_BODY["policy"]["states"][3]
        assert delete["transitions"] == []

    def test_ism_template_pattern(self):
        patterns = ISM_POLICY_BODY["policy"]["ism_template"]
        assert patterns[0]["index_patterns"] == [f"{INDEX_PREFIX}-*"]
