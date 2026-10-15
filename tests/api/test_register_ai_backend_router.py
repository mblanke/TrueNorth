"""scripts/register_ai_backend_inprocess.py against the real /ai-config router.

The contract tests (tests/contracts/test_installer_register_ai_backend.py) use a fake API;
this runs ``register()`` through the API's own routes, schemas and guards, with the AI
endpoint stubbed by respx (TN-MGMT01's vLLM on a GB10): the payload is accepted, the key is
sealed and kept unless asked, routes are made once, discovery records one node from the
base URL with no invented GPU, and a re-run duplicates nothing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import respx
from app.models import AIBackendConfig, AIFleetNode, AIModelRoute
from app.secretbox import unseal
from httpx import Response

ROOT = Path(__file__).resolve().parents[2]
GB10 = "http://192.168.1.88:8000/v1"
ARGS = dict(name="Platform AI", base_url=GB10, api_key="first-key", models=["agent", "gpt-oss-120b", "agent"])
MODELS = {"data": [{"id": "gpt-oss-120b"}, {"id": "agent"}]}


def _script():
    spec = importlib.util.spec_from_file_location(
        "register_ai_backend_inprocess", ROOT / "scripts/register_ai_backend_inprocess.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Api:
    def __init__(self, client):
        self.client = client

    def _call(self, method, path, json=None):
        r = self.client.request(method, path, json=json)
        return r.status_code, (r.json() if r.content else None)

    def get(self, path, **kw):
        return self._call("GET", path)

    def post(self, path, json=None, **kw):
        return self._call("POST", path, json)

    def patch(self, path, json=None, **kw):
        return self._call("PATCH", path, json)


@pytest.fixture
def api(client):
    return _Api(client)


@respx.mock
def test_registers_once_routes_and_discovers(api, db_session, capsys):
    respx.get(f"{GB10}/models").mock(return_value=Response(200, json=MODELS))
    mod = _script()

    assert mod.register(api, **ARGS) == 0
    (b,) = db_session.query(AIBackendConfig).all()
    assert (b.name, b.backend_type, b.base_url, b.is_primary) == ("Platform AI", "openai", GB10, True)
    assert b.api_key_encrypted != "first-key" and unseal(b.api_key_encrypted) == "first-key"
    assert sorted(r.model_pattern for r in db_session.query(AIModelRoute).all()) == ["agent", "gpt-oss-120b"]
    (node,) = db_session.query(AIFleetNode).all()
    assert (node.node_name, node.url, node.status) == ("192.168.1.88", GB10, "online")
    assert node.gpu_model is None and node.gpu_vram_gb is None
    assert json.loads(node.loaded_models) == ["gpt-oss-120b", "agent"]
    out = capsys.readouterr().out
    assert "created 'Platform AI'" in out and "ai discover: ok 192.168.1.88 online (2 models" in out
    assert "first-key" not in out and "WARNING" not in out

    # Re-run with a rotated vault key: nothing duplicated, the operator's key kept.
    assert mod.register(api, **{**ARGS, "api_key": "vault-rotated"}) == 0
    db_session.expire_all()
    assert db_session.query(AIBackendConfig).count() == 1
    assert db_session.query(AIModelRoute).count() == 2 and db_session.query(AIFleetNode).count() == 1
    assert unseal(db_session.query(AIBackendConfig).one().api_key_encrypted) == "first-key"
    assert "unchanged 'Platform AI'" in capsys.readouterr().out

    assert mod.register(api, **{**ARGS, "api_key": "vault-rotated"}, update_key=True) == 0
    db_session.expire_all()
    assert unseal(db_session.query(AIBackendConfig).one().api_key_encrypted) == "vault-rotated"
    out = capsys.readouterr().out
    assert "updated 'Platform AI' (api_key)" in out and "vault-rotated" not in out


@respx.mock
def test_an_unreachable_endpoint_still_registers(api, db_session, capsys):
    respx.get(f"{GB10}/models").mock(side_effect=httpx.ConnectError("refused"))
    assert _script().register(api, **ARGS) == 0
    assert db_session.query(AIBackendConfig).count() == 1
    assert db_session.query(AIFleetNode).one().status == "offline"
    assert "is offline" in capsys.readouterr().out
