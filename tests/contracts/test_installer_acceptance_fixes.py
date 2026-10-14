"""Installer contracts for the staging acceptance fixes (v1.2.0-rc7 on tn-staging).

* AI: the orchestrator's per-task models (AI_HEAVY/GENERAL/CODE_MODEL) are rendered from
  tn_ai_*_model, each defaulting to tn_ai_default_model; unrendered, compose's LiteLLM aliases
  (agent, coder-fast) reached a router without them and every answer was a 502.
  99-validate lists the endpoint's models from inside the orchestrator (its key never leaves
  the container) and fails when a configured model is missing.
* Audit: 99-validate runs the signed audit export once (python -m worker.audit_export_now),
  so deep health is current right after an install or upgrade.
* Acceptance: playbooks/acceptance.yml runs the full suite for an operator with -K, the
  Keycloak admin credential from the host's secret file in the environment only.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
VALIDATE = yaml.safe_load((INSTALL / "playbooks/99-validate.yml").read_text())[0]
ACCEPTANCE = yaml.safe_load((INSTALL / "playbooks/acceptance.yml").read_text())[0]
ENV_J2 = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
COMPOSE = yaml.safe_load((ROOT / "infra/platform/docker/compose.prod.yml").read_text())
TASK_MODELS = ("AI_HEAVY_MODEL", "AI_GENERAL_MODEL", "AI_CODE_MODEL")


def _task(play: dict, prefix: str) -> dict:
    [task] = [t for t in play["tasks"] if t["name"].startswith(prefix)]
    return task


# ── AI task models ─────────────────────────────────────────────────────


def test_every_model_the_orchestrator_reads_is_rendered():
    """Any *_MODEL compose gives the orchestrator a default for must come from the installer."""
    env = COMPOSE["services"]["ai-orchestrator"]["environment"]
    with_default = [k for k, v in env.items() if k.endswith("_MODEL") and ":-" in str(v)]
    assert set(TASK_MODELS) <= set(with_default)
    rendered = set(re.findall(r"^([A-Z_]+_MODEL)=", ENV_J2, re.M))
    assert set(with_default) <= rendered, set(with_default) - rendered


def test_task_models_default_to_the_default_model():
    group_vars = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    for name in ("tn_ai_heavy_model", "tn_ai_general_model", "tn_ai_code_model"):
        assert group_vars[name] == "{{ tn_ai_default_model }}", name
        env = {"tn_ai_heavy_model": "AI_HEAVY_MODEL", "tn_ai_general_model": "AI_GENERAL_MODEL",
               "tn_ai_code_model": "AI_CODE_MODEL"}[name]
        assert f"{env}={{{{ {name} | default(tn_ai_default_model) }}}}" in ENV_J2
    assert "OPENAI_DEFAULT_MODEL={{ tn_ai_default_model }}" in ENV_J2


def test_staging_sets_only_the_default_model():
    """Staging names one chat model; the task models follow it (no agent / coder-fast)."""
    text = (INSTALL / "inventory/staging.yml").read_text()
    assert "tn_ai_default_model: sovereign-chat" in text
    assert not re.search(r"tn_ai_(heavy|general|code)_model", text)


class _Models(http.server.BaseHTTPRequestHandler):
    status = 200
    ids: list[str] = []
    seen_auth: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server's name
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        body = json.dumps({"data": [{"id": i, "object": "model"} for i in self.ids]}).encode()
        self.send_response(self.status if self.path == "/api/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def models_endpoint():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Models.seen_auth = []
    yield server, f"http://127.0.0.1:{server.server_address[1]}/api"
    server.shutdown()


def _model_check(base: str, **env) -> dict:
    script = _task(VALIDATE, "Validate — AI orchestrator's models exist")["ansible.builtin.command"]["stdin"]
    full = {"PATH": os.environ.get("PATH", ""), "OPENAI_BASE_URL": base, **env}
    out = subprocess.run([sys.executable, "-I", "-"], input=script, capture_output=True, text=True, env=full,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert "sk-secret" not in out.stdout + out.stderr  # the key never leaves the container
    return json.loads(out.stdout)


def test_the_model_check_finds_a_missing_task_model(models_endpoint):
    """Staging rc7: default model sovereign-chat, task models compose's agent / coder-fast."""
    _, base = models_endpoint
    _Models.status, _Models.ids = 200, ["sovereign-chat", "sovereign-embed"]
    got = _model_check(base, OPENAI_API_KEY="sk-secret", OPENAI_DEFAULT_MODEL="sovereign-chat",
                       AI_HEAVY_MODEL="agent", AI_GENERAL_MODEL="agent", AI_CODE_MODEL="coder-fast")
    assert got["missing"] == ["agent", "coder-fast"]
    assert _Models.seen_auth == ["Bearer sk-secret"]


def test_the_model_check_passes_when_every_model_is_listed(models_endpoint):
    _, base = models_endpoint
    _Models.status, _Models.ids = 200, ["sovereign-chat"]
    got = _model_check(base, OPENAI_API_KEY="", **dict.fromkeys(("OPENAI_DEFAULT_MODEL", *TASK_MODELS),
                                                                  "sovereign-chat"))
    assert got["missing"] == [] and got["models"] == ["sovereign-chat"]
    assert _Models.seen_auth == [""]  # an unauthenticated endpoint: no header at all


def test_an_unlistable_endpoint_is_reported_not_failed(models_endpoint):
    _, base = models_endpoint
    _Models.status = 401
    got = _model_check(base, OPENAI_API_KEY="sk-secret", AI_HEAVY_MODEL="x")
    assert got["error"].startswith("HTTP 401") and "missing" not in got


def test_validate_fails_on_a_missing_model_after_the_report():
    names = [t["name"] for t in VALIDATE["tasks"]]
    check = names.index("Validate — the AI orchestrator's models exist on its endpoint")
    assert names.index("Validate — REPORT") < check
    task = VALIDATE["tasks"][check]["ansible.builtin.assert"]
    assert "tn_ai_models.missing" in task["that"] and "tn_ai_models.error is defined" in task["that"]
    run = _task(VALIDATE, "Validate — AI orchestrator's models exist")["ansible.builtin.command"]["cmd"]
    assert "exec -T ai-orchestrator python -" in run


# ── audit export, once, at install ─────────────────────────────────────


def test_validate_runs_the_audit_export_once_and_fails_after_the_report_if_it_cannot():
    names = [t["name"] for t in VALIDATE["tasks"]]
    run = _task(VALIDATE, "Validate — run the signed audit export once")
    assert "exec -T worker-scenario python -m worker.audit_export_now" in run["ansible.builtin.command"]["cmd"]
    assert names.index("Validate — run the signed audit export once") < names.index("Validate — REPORT")
    gate = names.index("Validate — the signed audit export ran")
    assert names.index("Validate — REPORT") < gate
    assert VALIDATE["tasks"][gate]["ansible.builtin.assert"]["that"] == "tn_audit_export_now.rc | default(1) == 0"
    assert (ROOT / "control-plane/worker/worker/audit_export_now.py").is_file()


# ── playbooks/acceptance.yml ───────────────────────────────────────────


def test_acceptance_playbook_is_operator_run_and_not_in_site():
    assert ACCEPTANCE["become"] is True and "acceptance" in ACCEPTANCE["tags"]
    assert "acceptance.yml" not in (INSTALL / "site.yml").read_text()
    out = _task(ACCEPTANCE, "Acceptance — output directory")["ansible.builtin.file"]
    assert (out["owner"], out["mode"]) == ("10001", "0700")
    reports = _task(ACCEPTANCE, "Acceptance — report directory")["ansible.builtin.file"]
    assert (reports["owner"], reports["mode"]) == ("root", "0750")


def test_the_keycloak_credential_is_never_on_a_command_line_or_logged():
    slurp = _task(ACCEPTANCE, "Acceptance — Keycloak admin credential")
    assert slurp["no_log"] is True
    assert slurp["ansible.builtin.slurp"]["src"] == "{{ tn_config_dir }}/secrets/keycloak_admin_password"
    run = _task(ACCEPTANCE, "Acceptance — run the suite")
    assert run["no_log"] is True
    argv = run["ansible.builtin.command"]["argv"]
    assert "'-e', 'KEYCLOAK_ADMIN_PASSWORD'," in argv  # by name: docker compose takes it from its env
    assert "tn_acceptance_kc" not in argv and "vault_" not in argv
    assert "tn_acceptance_kc.content | b64decode" in run["environment"]["KEYCLOAK_ADMIN_PASSWORD"]
    for t in ACCEPTANCE["tasks"]:
        if t is not run and t is not slurp:
            assert "tn_acceptance_kc" not in json.dumps(t), t["name"]


def test_the_suite_runs_as_the_runbook_says_and_the_play_fails_on_a_fail():
    argv = _task(ACCEPTANCE, "Acceptance — run the suite")["ansible.builtin.command"]["argv"]
    for piece in ("'/srcapp/scripts/staging_acceptance.py'", "'--admin', tn_bootstrap_admin_upn",
                  "'--host-config', '/hostcfg'", "tn_config_dir ~ ':/hostcfg:ro'", "'--json', '/out/acceptance-'",
                  "'--strict'", "'--vsphere-url', tn_acceptance_vsphere_url"):
        assert piece in argv, piece
    names = [t["name"] for t in ACCEPTANCE["tasks"]]
    gate = names.index("Acceptance — every check passed")
    assert gate == len(names) - 1  # after the reports are copied
    assert names.index("Acceptance — reports (timestamped, and latest)") < gate
    assert ACCEPTANCE["tasks"][gate]["ansible.builtin.assert"]["that"] == "tn_acceptance_run.rc | default(2) == 0"
    runbook = (ROOT / "docs/runbooks/staging-acceptance.md").read_text()
    assert "playbooks/acceptance.yml -K" in runbook and "acceptance-reports/latest.txt" in runbook
