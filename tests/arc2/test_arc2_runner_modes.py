"""The runner's model backends (tools/arc2/runner.py ModelConfig): ARC2_MODE.

subscription runs Claude; local runs only an Anthropic-compatible gateway (ARC2_LOCAL_*)
and its jobs can reach nothing else; subscription_with_local_fallback re-runs on the
gateway only when Claude itself was unavailable. Unset, the older ARC2_FALLBACK* settings
keep working as before. A fake ``claude`` records the environment it was given, so these
tests spend nothing and need no network.
"""

from __future__ import annotations

import http.server
import json
import stat
import threading
from pathlib import Path

import pytest
from arc2 import runner

FAKE = r"""#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
calls = os.environ["FAKE_CALLS"]
keep = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SUBAGENT_MODEL", "ARC2_LOCAL_TOKEN")
with open(calls, "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "env": {k: os.environ.get(k) for k in keep}}) + "\n")
def emit(e): print(json.dumps(e), flush=True)
mode = os.environ.get("FAKE_MODE", "ok")
on_claude = not os.environ.get("ANTHROPIC_BASE_URL")
if on_claude and mode == "claude-unavailable":
    emit({"type": "result", "is_error": True, "result": "API Error: 529 overloaded"}); sys.exit(1)
if mode == "refused":
    emit({"type": "result", "is_error": True, "result": "merge rejected: STOP"}); sys.exit(1)
emit({"type": "result", "is_error": False, "result": "Outline ready.", "num_turns": 2})
"""

GATEWAY = "https://llm-gw.example.org"
CLEAR = ("ARC2_MODE", "ARC2_LOCAL_URL", "ARC2_LOCAL_MODEL", "ARC2_LOCAL_TOKEN", "ARC2_FALLBACK", "ARC2_FALLBACK_URL",
         "ARC2_FALLBACK_MODEL", "ARC2_EGRESS_ALLOW", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
         "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ARC2_AUTO_ACCEPT_GATES")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in CLEAR:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    return exe, calls


def gateway_env(monkeypatch, mode: str, url: str = GATEWAY, token: str = "gw-secret") -> None:
    monkeypatch.setenv("ARC2_MODE", mode)
    monkeypatch.setenv("ARC2_LOCAL_URL", url)
    monkeypatch.setenv("ARC2_LOCAL_MODEL", "qwen3-coder")
    if token:
        monkeypatch.setenv("ARC2_LOCAL_TOKEN", token)


def run_one(tmp_path: Path, exe: Path) -> dict:
    runs = tmp_path / "runs"
    queue, _ = runner.dirs(runs)
    job = {"id": "job123456", "action": "start", "slug": "arc2-modes", "text": "a course",
           "created_at": "2026-10-09T12:00:00Z", "requested_by": "dev-admin"}
    (queue / f"20261009T120000-{job['id']}.json").write_text(json.dumps(job))
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--once"]) == 0
    [rec] = [json.loads(p.read_text()) for p in (runs / "_jobs").glob("*.json")]
    return rec


def calls(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# ── Choosing the mode ──────────────────────────────────────────────────
def test_unset_keeps_the_legacy_ollama_fallback(monkeypatch):
    m = runner.ModelConfig.from_env()
    assert (m.mode, m.legacy, m.primary) == ("subscription_with_local_fallback", True, None)
    assert m.fallback.label == "ollama:qwen3.6:35b-a3b" and m.fallback.url == "http://127.0.0.1:11434"
    assert m.egress_hosts() == ("api.anthropic.com",) and m.local_ports() == (11434,)
    monkeypatch.setenv("ARC2_FALLBACK", "off")
    assert runner.ModelConfig.from_env().mode == "subscription"
    assert runner.ModelConfig.from_env().fallback is None


def test_an_explicit_mode_ignores_the_legacy_settings(monkeypatch):
    monkeypatch.setenv("ARC2_MODE", "subscription")
    monkeypatch.setenv("ARC2_FALLBACK_MODEL", "qwen3:8b")
    m = runner.ModelConfig.from_env()
    assert (m.mode, m.primary, m.fallback, m.local_ports()) == ("subscription", None, None, ())


@pytest.mark.parametrize(("env", "message"), [
    ({"ARC2_MODE": "cloud"}, "use one of"),
    ({"ARC2_MODE": "local"}, "needs ARC2_LOCAL_URL and ARC2_LOCAL_MODEL"),
    ({"ARC2_MODE": "local", "ARC2_LOCAL_URL": GATEWAY}, "needs ARC2_LOCAL_URL and ARC2_LOCAL_MODEL"),
    ({"ARC2_MODE": "local", "ARC2_LOCAL_URL": "http://llm-gw.example.org", "ARC2_LOCAL_MODEL": "m",
      "ARC2_LOCAL_TOKEN": "t"}, "must be https"),
    ({"ARC2_MODE": "subscription_with_local_fallback", "ARC2_LOCAL_URL": GATEWAY, "ARC2_LOCAL_MODEL": "m"},
     "set ARC2_LOCAL_TOKEN"),
    ({"ARC2_MODE": "local", "ARC2_LOCAL_URL": "ftp://x", "ARC2_LOCAL_MODEL": "m"}, r"an http\(s\) URL"),
])
def test_a_mode_it_cannot_run_is_refused(monkeypatch, env, message):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(runner.ModelConfigError, match=message):
        runner.ModelConfig.from_env()


def test_the_runner_does_not_start_on_a_bad_mode(tmp_path, fake_claude, monkeypatch, capsys):
    exe, calls_file = fake_claude
    monkeypatch.setenv("ARC2_MODE", "local")
    assert runner.main(["--runs", str(tmp_path / "runs"), "--claude", str(exe), "--once"]) == 2
    assert "not starting: ARC2_MODE=local needs ARC2_LOCAL_URL" in capsys.readouterr().err
    assert not calls_file.exists()


def test_a_gateway_on_this_host_needs_no_tls_or_token(monkeypatch):
    gateway_env(monkeypatch, "local", url="http://127.0.0.1:4000", token="")
    m = runner.ModelConfig.from_env()
    assert m.primary.loopback and m.local_ports() == (4000,)
    assert m.egress_hosts() == (), "nothing through the proxy: the gateway is reached directly"


# ── The environment each mode gives a job ──────────────────────────────
def test_subscription_runs_claude_with_its_own_credential(tmp_path, fake_claude, monkeypatch):
    exe, calls_file = fake_claude
    monkeypatch.setenv("ARC2_MODE", "subscription")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-sub")
    rec = run_one(tmp_path, exe)
    assert (rec["state"], rec["engine"], rec["mode"]) == ("done", "claude", "subscription")
    [call] = calls(calls_file)
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-sub"
    assert call["env"]["ANTHROPIC_BASE_URL"] is None and "--model" not in call["args"]


def test_local_runs_only_the_gateway_with_its_token_and_no_anthropic_credential(tmp_path, fake_claude, monkeypatch):
    exe, calls_file = fake_claude
    gateway_env(monkeypatch, "local")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-sub")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-key")
    rec = run_one(tmp_path, exe)
    assert (rec["state"], rec["engine"], rec["mode"]) == ("done", "local:qwen3-coder", "local")
    [call] = calls(calls_file)
    env = call["env"]
    assert env["ANTHROPIC_BASE_URL"] == GATEWAY and env["ANTHROPIC_AUTH_TOKEN"] == "gw-secret"
    assert env["ANTHROPIC_API_KEY"] == "" and env["CLAUDE_CODE_OAUTH_TOKEN"] is None
    assert env["ANTHROPIC_MODEL"] == env["CLAUDE_CODE_SUBAGENT_MODEL"] == "qwen3-coder"
    assert env["ARC2_LOCAL_TOKEN"] is None, "the token reaches the job only as Claude Code's own setting"
    assert call["args"][call["args"].index("--model") + 1] == "qwen3-coder"


class _PassThrough(runner.Confinement):
    """A confinement that runs the command as is: the confined environment path, no OS sandbox."""
    name = "test"

    def available(self):
        return True

    def wrap(self, cmd, jail):
        return cmd


def test_a_confined_local_job_gets_the_gateway_and_never_the_subscription_token(tmp_path, fake_claude, monkeypatch):
    exe, calls_file = fake_claude
    gateway_env(monkeypatch, "local")
    token_file = tmp_path / "oauth-token"
    token_file.write_text("sk-ant-oat01-from-file\n")
    monkeypatch.setenv("ARC2_OAUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ARC2_JOB_ENV", "FAKE_CALLS")
    runs = tmp_path / "runs"
    _, jobs = runner.dirs(runs)
    record = {"id": "job123456", "action": "start", "slug": "arc2-modes", "text": "a course"}
    rec = runner.run_job(record, jobs / "job123456.json", str(exe), 60, runner.ModelConfig.from_env(),
                         _PassThrough(), runs)
    assert (rec["state"], rec["engine"], rec["confinement"]) == ("done", "local:qwen3-coder", "test")
    [call] = calls(calls_file)
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None
    assert call["env"]["ANTHROPIC_AUTH_TOKEN"] == "gw-secret" and call["env"]["ARC2_LOCAL_TOKEN"] is None


def test_fallback_mode_reruns_on_the_gateway_when_claude_is_unavailable(tmp_path, fake_claude, monkeypatch):
    exe, calls_file = fake_claude
    gateway_env(monkeypatch, "subscription_with_local_fallback")
    monkeypatch.setenv("FAKE_MODE", "claude-unavailable")
    monkeypatch.setattr(runner.LocalModel, "reachable", lambda self, timeout=3.0: True)
    rec = run_one(tmp_path, exe)
    assert (rec["state"], rec["engine"]) == ("done", "local:qwen3-coder")
    assert "529 overloaded" in rec["fallback_from"]
    first, second = calls(calls_file)
    assert first["env"]["ANTHROPIC_BASE_URL"] is None
    assert second["env"]["ANTHROPIC_BASE_URL"] == GATEWAY and second["env"]["ANTHROPIC_AUTH_TOKEN"] == "gw-secret"


@pytest.mark.parametrize("mode", ["refused", "ok"])
def test_fallback_mode_does_not_fall_back_for_anything_else(tmp_path, fake_claude, monkeypatch, mode):
    exe, calls_file = fake_claude
    gateway_env(monkeypatch, "subscription_with_local_fallback")
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setattr(runner.LocalModel, "reachable", lambda self, timeout=3.0: True)
    rec = run_one(tmp_path, exe)
    assert rec["engine"] == "claude" and "fallback_from" not in rec
    assert len(calls(calls_file)) == 1


def test_local_mode_never_falls_back_to_claude(tmp_path, fake_claude, monkeypatch):
    exe, calls_file = fake_claude
    gateway_env(monkeypatch, "local")
    monkeypatch.setenv("FAKE_MODE", "refused")
    rec = run_one(tmp_path, exe)
    assert (rec["state"], rec["engine"]) == ("failed", "local:qwen3-coder")
    assert all(c["env"]["ANTHROPIC_BASE_URL"] == GATEWAY for c in calls(calls_file))


# ── Egress per mode ─────────────────────────────────────────────────────
class _Sandboxed:
    name = "seatbelt"


@pytest.mark.parametrize(("mode", "egress_allow", "hosts", "ports"), [
    ("subscription", None, ("api.anthropic.com",), (443,)),
    ("subscription", "gw.corp.example", ("gw.corp.example",), (443,)),
    ("local", None, ("llm-gw.example.org",), (443,)),
    ("local", "api.anthropic.com", ("llm-gw.example.org",), (443,)),  # never Anthropic in local mode
    ("subscription_with_local_fallback", None, ("api.anthropic.com", "llm-gw.example.org"), (443,)),
])
def test_the_egress_proxy_allows_what_the_mode_needs(monkeypatch, mode, egress_allow, hosts, ports):
    if mode != "subscription":
        gateway_env(monkeypatch, mode)
    else:
        monkeypatch.setenv("ARC2_MODE", mode)
    if egress_allow:
        monkeypatch.setenv("ARC2_EGRESS_ALLOW", egress_allow)
    models = runner.ModelConfig.from_env()
    egress, forwards = runner.start_egress(_Sandboxed(), models)
    try:
        assert egress.allow == hosts and egress.ports == ports and forwards == []
    finally:
        egress.close()


def test_a_gateway_on_another_port_is_allowed_as_exactly_that_host_and_port(monkeypatch):
    """Staging's gateway is Tailscale Serve on :4443 (443 is taken on that host)."""
    gateway_env(monkeypatch, "local", url="https://atlas.tail8d54ec.ts.net:4443")
    m = runner.ModelConfig.from_env()
    assert m.egress_hosts() == ("atlas.tail8d54ec.ts.net:4443",) and m.egress_ports() == (443,)
    egress, _ = runner.start_egress(_Sandboxed(), m)
    try:
        assert egress.permits("atlas.tail8d54ec.ts.net", 4443)
        assert not egress.permits("atlas.tail8d54ec.ts.net", 443), "only the configured port"
        assert not egress.permits("api.anthropic.com", 443) and not egress.permits("api.anthropic.com", 4443)
    finally:
        egress.close()
    gateway_env(monkeypatch, "subscription_with_local_fallback", url="https://atlas.tail8d54ec.ts.net:4443")
    egress, _ = runner.start_egress(_Sandboxed(), runner.ModelConfig.from_env())
    try:
        assert egress.allow == ("api.anthropic.com", "atlas.tail8d54ec.ts.net:4443")
        assert egress.permits("api.anthropic.com", 443) and egress.permits("atlas.tail8d54ec.ts.net", 4443)
        assert not egress.permits("api.anthropic.com", 4443), "the gateway's port is not opened to other hosts"
    finally:
        egress.close()


def test_a_remote_gateway_on_any_port_must_still_be_https(monkeypatch):
    gateway_env(monkeypatch, "local", url="http://atlas.tail8d54ec.ts.net:4443")
    with pytest.raises(runner.ModelConfigError, match="must be https"):
        runner.ModelConfig.from_env()


def test_a_loopback_gateway_allows_nothing_through_the_proxy(monkeypatch):
    gateway_env(monkeypatch, "local", url="http://localhost:4000", token="")
    egress, _ = runner.start_egress(_Sandboxed(), runner.ModelConfig.from_env())
    try:
        assert egress.allow == ()
    finally:
        egress.close()


# ── Reachability: cheap, and not Ollama's /api/tags ─────────────────────
class _Gateway(http.server.BaseHTTPRequestHandler):
    models_status = 200
    head_status = 200
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append(("GET", self.path, self.headers.get("Authorization")))
        status = self.models_status if self.path == "/v1/models" else 404
        if status == 200 and self.headers.get("Authorization") != "Bearer gw-secret":
            status = 401
        self.send_response(status)
        self.end_headers()
        if status == 200:
            self.wfile.write(json.dumps({"data": [{"id": "qwen3-coder"}]}).encode())

    def do_HEAD(self):  # noqa: N802
        type(self).seen.append(("HEAD", self.path, self.headers.get("Authorization")))
        self.send_response(self.head_status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def gateway():
    _Gateway.models_status, _Gateway.head_status, _Gateway.seen = 200, 200, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_a_gateway_is_reachable_through_its_model_list_with_the_token(gateway):
    assert runner.LocalModel(gateway, "qwen3-coder", "gw-secret", kind="gateway").reachable()
    assert _Gateway.seen == [("GET", "/v1/models", "Bearer gw-secret")]
    assert not runner.LocalModel(gateway, "qwen3-coder", "wrong", kind="gateway").reachable()


def test_a_gateway_without_a_model_list_is_checked_with_a_head(gateway):
    _Gateway.models_status = 404
    assert runner.LocalModel(gateway, "m", "gw-secret", kind="gateway").reachable()
    assert [s[0] for s in _Gateway.seen] == ["GET", "HEAD"]
    _Gateway.head_status = 503
    assert not runner.LocalModel(gateway, "m", "gw-secret", kind="gateway").reachable()


def test_an_unreachable_gateway_is_not_reachable():
    assert not runner.LocalModel("http://127.0.0.1:9", "m", "t", kind="gateway").reachable(timeout=1)
