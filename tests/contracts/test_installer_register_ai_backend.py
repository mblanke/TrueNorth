"""80-seed registers the installer's AI endpoint as an AI backend (TN-MGMT01, 2026-10-15).

The installer configured the orchestrator's endpoint (tn_ai_base_url, vault_openai_api_key,
tn_ai_*_model) but never registered it, so the AI settings page showed 0 backends.
scripts/register_ai_backend_inprocess.py registers it through the API in-process as the
bootstrap administrator; the key reaches it only through the environment.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
SCRIPT = ROOT / "scripts/register_ai_backend_inprocess.py"
SECRET = "sk-s3cret-vllm-key"
GB10 = "http://192.168.1.88:8000/v1"


def _script():
    spec = importlib.util.spec_from_file_location("register_ai_backend_inprocess", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeApi:
    """The /ai-config routes the script uses, over in-memory backends and routes."""

    def __init__(self, backends=None, routes=None, *, online: bool = True, served=("gpt-oss-120b", "agent"),
                 create_status: int = 201) -> None:
        self.backends = [dict(b) for b in (backends or [])]
        self.routes = [dict(r) for r in (routes or [])]
        self.online = online
        self.served = list(served)
        self.create_status = create_status
        self.calls: list[tuple] = []

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        if path == "/ai-config/backends":
            return 200, [dict(b) for b in self.backends]
        if path == "/ai-config/routes":
            return 200, [dict(r) for r in self.routes]
        raise AssertionError(path)

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        if path == "/ai-config/backends":
            if self.create_status != 201:
                return self.create_status, {"detail": "refused"}
            row = {k: v for k, v in json.items() if k != "api_key"}
            row.update(id=f"b{len(self.backends) + 1}", is_active=True)
            self.backends.append(row)
            return 201, dict(row)
        if path == "/ai-config/routes":
            row = dict(json, id=f"r{len(self.routes) + 1}")
            self.routes.append(row)
            return 201, dict(row)
        bid, action = path.split("/")[3:5]
        if action == "set-primary":
            for b in self.backends:
                b["is_primary"] = b["id"] == bid
            return 200, {"message": "set as primary"}
        if action == "discover":
            b = next(b for b in self.backends if b["id"] == bid)
            models = [{"name": m} for m in self.served] if self.online else []
            return 200, {"scanned": 1, "results": [{
                "node_name": "192.168.1.88", "url": b["base_url"], "online": self.online,
                "models": models, "model_count": len(models), "gpu_model": None, "gpu_vram_gb": None}]}
        raise AssertionError(path)

    def patch(self, path, json=None, **kw):
        self.calls.append(("PATCH", path, json))
        b = next(b for b in self.backends if b["id"] == path.rsplit("/", 1)[1])
        b.update({k: v for k, v in json.items() if k != "api_key"})
        return 200, dict(b)

    def writes(self, method=None):
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method)]


ARGS = dict(name="Platform AI", base_url=GB10, api_key=SECRET, models=["agent", "gpt-oss-120b,agent", "agent", ""])


def _existing(**over):
    row = dict(id="b1", name="Platform AI", backend_type="openai", base_url=GB10, is_primary=True, is_active=True)
    row.update(over)
    return row


def test_creates_the_backend_as_primary_with_deduplicated_routes(capsys) -> None:
    api = _FakeApi()
    assert _script().register(api, **ARGS) == 0
    (create,) = [c for c in api.writes("POST") if c[1] == "/ai-config/backends"]
    body = create[2]
    assert (body["name"], body["backend_type"], body["base_url"]) == ("Platform AI", "openai", GB10)
    assert body["is_primary"] is True and body["api_key"] == SECRET
    assert [r["model_pattern"] for r in api.routes] == ["agent", "gpt-oss-120b"]
    assert {r["backend_id"] for r in api.routes} == {"b1"}
    assert api.writes()[-1][1] == "/ai-config/backends/b1/discover"
    out = capsys.readouterr().out
    assert "ai backend: created 'Platform AI'" in out and "primary" in out
    assert "ai route: added 'agent'" in out and "ai route: added 'gpt-oss-120b'" in out
    assert "ai discover: ok 192.168.1.88 online (2 models" in out and "WARNING" not in out
    assert SECRET not in out


def test_an_empty_key_is_allowed_for_an_unauthenticated_endpoint() -> None:
    api = _FakeApi()
    assert _script().register(api, **{**ARGS, "api_key": ""}) == 0
    create = next(c for c in api.writes("POST") if c[1] == "/ai-config/backends")
    assert create[2]["api_key"] is None


def test_rerun_neither_duplicates_nor_patches(capsys) -> None:
    routes = [{"id": "r1", "model_pattern": "agent", "backend_id": "b1"},
              {"id": "r2", "model_pattern": "gpt-oss-120b", "backend_id": "b1"}]
    api = _FakeApi([_existing(base_url=GB10 + "/")], routes)  # a trailing slash is the same URL
    assert _script().register(api, **ARGS) == 0
    assert len(api.backends) == 1 and len(api.routes) == 2
    assert [c[1] for c in api.writes()] == ["/ai-config/backends/b1/discover"]
    out = capsys.readouterr().out
    assert "ai backend: unchanged 'Platform AI'" in out and "ai route: unchanged 'agent'" in out


def test_found_by_base_url_when_renamed_and_base_url_drift_is_patched(capsys) -> None:
    mod = _script()
    api = _FakeApi([_existing(name="GB10 vLLM")])
    assert mod.register(api, **ARGS) == 0
    assert len(api.backends) == 1 and not api.writes("PATCH")

    api = _FakeApi([_existing(base_url="http://old-llm:4000/v1")])
    assert mod.register(api, **ARGS) == 0
    (patch,) = api.writes("PATCH")
    assert patch[1] == "/ai-config/backends/b1" and patch[2] == {"base_url": GB10}
    assert "updated 'Platform AI' (base_url)" in capsys.readouterr().out


def test_key_set_once_and_replaced_only_when_asked(capsys) -> None:
    mod = _script()
    api = _FakeApi([_existing()])
    assert mod.register(api, **ARGS) == 0
    assert not api.writes("PATCH")
    api = _FakeApi([_existing()])
    assert mod.register(api, **ARGS, update_key=True) == 0
    (patch,) = api.writes("PATCH")
    assert patch[2] == {"api_key": SECRET}
    assert SECRET not in capsys.readouterr().out


def test_made_primary_only_when_no_backend_is() -> None:
    mod = _script()
    api = _FakeApi([_existing(is_primary=False)])
    assert mod.register(api, **ARGS) == 0
    assert ("POST", "/ai-config/backends/b1/set-primary", None) in api.calls

    other = _existing(id="b9", name="Home lab", base_url="http://atlas:4000/v1", is_primary=True)
    api = _FakeApi([other])
    assert mod.register(api, **ARGS) == 0
    create = next(c for c in api.writes("POST") if c[1] == "/ai-config/backends")
    assert create[2]["is_primary"] is False
    assert not any(c[1].endswith("/set-primary") for c in api.calls)


def test_a_route_to_another_backend_is_left_alone(capsys) -> None:
    api = _FakeApi([_existing()], [{"id": "r1", "model_pattern": "agent", "backend_id": "b9"}])
    assert _script().register(api, **ARGS) == 0
    assert [(r["model_pattern"], r["backend_id"]) for r in api.routes] == [("agent", "b9"), ("gpt-oss-120b", "b1")]
    assert "'agent' routes to another backend; left alone" in capsys.readouterr().out


def test_an_unreachable_endpoint_or_missing_model_warns_but_registers(capsys) -> None:
    mod = _script()
    api = _FakeApi(online=False)
    assert mod.register(api, **ARGS) == 0
    assert len(api.backends) == 1
    assert "ai discover: WARNING 192.168.1.88 (http://192.168.1.88:8000/v1) is offline" in capsys.readouterr().out

    api = _FakeApi(served=("gpt-oss-120b",))
    assert mod.register(api, **ARGS) == 0
    assert "ai discover: WARNING the endpoint does not list agent" in capsys.readouterr().out


def test_a_refused_create_fails() -> None:
    api = _FakeApi(create_status=422)
    assert _script().register(api, **ARGS) == 1
    assert not api.routes and not any(c[1].endswith("/discover") for c in api.calls)


def test_model_ids_are_deduplicated_in_order() -> None:
    assert _script().model_ids(["agent", " gpt-oss-120b , agent", "", "coder"]) == ["agent", "gpt-oss-120b", "coder"]


# ── The installer task ─────────────────────────────────────────────────────


def _task(name: str) -> dict:
    tasks = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_the_task_passes_the_key_only_through_the_environment_with_no_log() -> None:
    task = _task("Seed — register the AI endpoint as an AI backend")
    assert task["no_log"] is True
    assert task["environment"] == {"TN_AI_API_KEY": "{{ vault_openai_api_key | default('') }}"}
    argv = task["ansible.builtin.command"]["argv"]
    assert "'-e', 'TN_AI_API_KEY'," in argv  # by name, no value
    assert "vault_openai_api_key" not in argv.split("'--update-key'")[0]
    assert "TN_AI_API_KEY=" not in argv
    assert "scripts/register_ai_backend_inprocess.py" in argv and ":/srcapp:ro'" in argv and "'--no-deps'" in argv
    assert "'--admin', tn_bootstrap_admin_upn" in argv and "'--base-url', tn_ai_base_url" in argv
    for var in ("tn_ai_default_model", "tn_ai_heavy_model", "tn_ai_general_model", "tn_ai_code_model"):
        assert var in argv
    assert "| unique | join(',')" in argv
    assert "tn_ai_backend_update_key" in argv
    when = " ".join(task["when"].split())
    assert "tn_register_ai_backend | default(true) | bool" in when
    assert "tn_ai_enabled | default(true) | bool" in when and "tn_ai_base_url | default('') | length > 0" in when
    assert task["failed_when"] is False
    check = _task("Seed — the AI backend is registered")
    assert check["ansible.builtin.assert"]["that"] == "tn_ai_backend_reg.rc == 0"
    assert " ".join(check["when"].split()) == when


def test_toggle_defaults() -> None:
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert defaults["tn_register_ai_backend"] is True
    assert defaults["tn_ai_backend_name"] == "Platform AI"
    assert defaults["tn_ai_backend_update_key"] is False


def test_the_script_reads_only_its_key_variable_and_never_takes_it_as_an_argument() -> None:
    src = SCRIPT.read_text()
    assert re.findall(r"os\.(?:environ|getenv)\b[^\n]*", src) == ['os.environ.get(KEY_ENV, "")  # the only variable read; never argv, never printed']
    assert 'KEY_ENV = "TN_AI_API_KEY"' in src
    assert "--api-key" not in src and '"--key"' not in src
    assert "from _inprocess_api import connect" in src and "Authorization" not in src
