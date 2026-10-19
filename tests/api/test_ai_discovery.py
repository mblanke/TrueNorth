"""POST /ai-config/backends/{id}/discover probes registered fleet nodes only (TN-MGMT01, 2026-10-15).

Until then discovery probed a hard-coded home-lab node ("r7725", "2x NVIDIA H200 NVL",
288 GB). An OpenAI-compatible engine answers at the backend's base_url whatever the node's
host, so on a site with a vLLM server on a GB10 the AI page showed a fake, online H200
node. Now: registered nodes are probed at their own url; an OpenAI-compatible backend with
none gets one node derived from its base_url, GPU fields null ("not reported"); an
operator's GPU entries are never overwritten.
"""

from __future__ import annotations

import json

import httpx
import respx
from _shared import act_as, real_tenant, real_user
from app.models import AIBackendConfig, AIFleetNode, UserRole
from httpx import Response

GB10 = "http://192.168.1.88:8000/v1"
MODELS = {"data": [{"id": "gpt-oss-120b", "owned_by": "vllm"}, {"id": "agent", "owned_by": "vllm"}]}


def _backend(db, backend_type: str = "openai", base_url: str = GB10) -> AIBackendConfig:
    b = AIBackendConfig(name=f"{backend_type}-b", backend_type=backend_type, base_url=base_url,
                        is_primary=True, is_active=True, timeout_seconds=5)
    db.add(b)
    db.commit()
    return b


def _nodes(db, backend) -> list[AIFleetNode]:
    db.expire_all()
    return db.query(AIFleetNode).filter(AIFleetNode.backend_id == backend.id).order_by(AIFleetNode.node_name).all()


@respx.mock
def test_fresh_openai_backend_gets_one_node_from_its_base_url_and_no_invented_gpu(client, db_session):
    b = _backend(db_session)
    respx.get(f"{GB10}/models").mock(return_value=Response(200, json=MODELS))

    r = client.post(f"/ai-config/backends/{b.id}/discover")

    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 1
    (res,) = body["results"]
    assert (res["node_name"], res["url"], res["online"]) == ("192.168.1.88", GB10, True)
    assert res["gpu_model"] is None and res["gpu_vram_gb"] is None
    assert [m["name"] for m in res["models"]] == ["gpt-oss-120b", "agent"]
    (node,) = _nodes(db_session, b)
    assert (node.node_name, node.url, node.status) == ("192.168.1.88", GB10, "online")
    assert node.gpu_model is None and node.gpu_vram_gb is None
    assert "r7725" not in r.text and "H200" not in r.text

    # A re-run probes that node again; it does not add another.
    assert client.post(f"/ai-config/backends/{b.id}/discover").json()["scanned"] == 1
    assert len(_nodes(db_session, b)) == 1

    # Its models are listed for the AI page, from the node.
    listed = client.get("/ai-config/models").json()
    assert {m["name"] for m in listed["models"]} == {"gpt-oss-120b", "agent"}
    assert {m["node"] for m in listed["models"]} == {"192.168.1.88"}


@respx.mock
def test_unreachable_endpoint_is_an_offline_node_not_an_error(client, db_session):
    b = _backend(db_session)
    respx.get(f"{GB10}/models").mock(side_effect=httpx.ConnectError("refused"))
    r = client.post(f"/ai-config/backends/{b.id}/discover")
    assert r.status_code == 200
    assert r.json()["results"][0]["online"] is False
    (node,) = _nodes(db_session, b)
    assert node.status == "offline" and node.gpu_model is None
    assert client.get("/ai-config/models").json()["count"] == 0


def test_fresh_ollama_backend_probes_nothing_and_invents_nothing(client, db_session):
    b = _backend(db_session, "ollama", base_url="http://open-webui:3000")
    # A scoped router: a catch-all on respx's global router would outlive this test.
    with respx.mock(assert_all_called=False) as router:
        anything = router.route().mock(return_value=Response(200, json={}))
        r = client.post(f"/ai-config/backends/{b.id}/discover")
    assert r.status_code == 200
    assert r.json() == {"scanned": 0, "results": []}
    assert not anything.called
    assert _nodes(db_session, b) == []


@respx.mock
def test_registered_ollama_nodes_are_probed_at_their_own_url(client, db_session):
    b = _backend(db_session, "ollama", base_url="http://open-webui:3000")
    db_session.add_all([
        AIFleetNode(backend_id=b.id, node_name="gpu-a", url="http://10.0.0.5:11434", status="unknown"),
        AIFleetNode(backend_id=b.id, node_name="gpu-b", url="http://10.0.0.6:11500", status="online"),
    ])
    db_session.commit()
    respx.get("http://10.0.0.5:11434/api/version").mock(return_value=Response(200, json={"version": "0.6"}))
    respx.get("http://10.0.0.5:11434/api/tags").mock(return_value=Response(200, json={"models": [{"name": "qwen3:8b"}]}))
    respx.get("http://10.0.0.5:11434/api/ps").mock(return_value=Response(200, json={"models": []}))
    respx.get("http://10.0.0.6:11500/api/version").mock(side_effect=httpx.ConnectError("down"))

    r = client.post(f"/ai-config/backends/{b.id}/discover")

    assert r.status_code == 200 and r.json()["scanned"] == 2
    a, bnode = _nodes(db_session, b)
    assert (a.status, json.loads(a.loaded_models)) == ("online", ["qwen3:8b"])
    assert bnode.status == "offline"
    assert a.last_health_check is not None and bnode.last_health_check is not None


@respx.mock
def test_registered_openai_node_is_probed_and_no_base_url_node_is_added(client, db_session):
    b = _backend(db_session, "vllm", base_url="http://litellm:4000/v1")
    db_session.add(AIFleetNode(backend_id=b.id, node_name="gb10", url=GB10, status="unknown"))
    db_session.commit()
    node_route = respx.get(f"{GB10}/models").mock(return_value=Response(200, json=MODELS))
    base_route = respx.get("http://litellm:4000/v1/models").mock(return_value=Response(200, json=MODELS))

    r = client.post(f"/ai-config/backends/{b.id}/discover")

    assert r.status_code == 200 and r.json()["scanned"] == 1
    assert node_route.called and not base_route.called
    (node,) = _nodes(db_session, b)
    assert node.node_name == "gb10" and node.status == "online"


@respx.mock
def test_operator_gpu_fields_are_kept(client, db_session):
    b = _backend(db_session)
    db_session.add(AIFleetNode(backend_id=b.id, node_name="gb10", url=GB10, status="unknown",
                               gpu_model="NVIDIA GB10 Grace Blackwell", gpu_vram_gb=128))
    db_session.commit()
    respx.get(f"{GB10}/models").mock(return_value=Response(200, json=MODELS))

    res = client.post(f"/ai-config/backends/{b.id}/discover").json()["results"][0]

    assert (res["gpu_model"], res["gpu_vram_gb"]) == ("NVIDIA GB10 Grace Blackwell", 128)
    (node,) = _nodes(db_session, b)
    assert (node.gpu_model, node.gpu_vram_gb, node.status) == ("NVIDIA GB10 Grace Blackwell", 128, "online")


def test_discover_needs_ai_config_write(client, db_session):
    b = _backend(db_session)
    tenant = real_tenant(db_session, "ai-disc")
    act_as(real_user(db_session, UserRole.instructor, tenant.id))
    assert client.post(f"/ai-config/backends/{b.id}/discover").status_code == 403
    assert _nodes(db_session, b) == []
