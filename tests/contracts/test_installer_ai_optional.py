"""Installer contracts for tn_ai_enabled, both modes.

On (the default): preflight requires tn_ai_base_url and probes it, the env file carries the
endpoint and key, and 99-validate checks the configured models. Off: no AI assert or probe, a
clear "AI disabled" line, OPENAI_BASE_URL / OPENAI_API_KEY rendered empty (so the orchestrator
reports ai_configured=false and answers 503), 99-validate reports "AI : disabled" and requires
the orchestrator to agree, ARC² only with a model of its own, and acceptance runs with the
ai-disabled skip profile.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
PREFLIGHT = yaml.safe_load((INSTALL / "roles/tn_preflight/tasks/main.yml").read_text())
VALIDATE = yaml.safe_load((INSTALL / "playbooks/99-validate.yml").read_text())[0]
ACCEPTANCE = yaml.safe_load((INSTALL / "playbooks/acceptance.yml").read_text())[0]
ENV_J2 = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
COMPOSE = yaml.safe_load((ROOT / "infra/platform/docker/compose.prod.yml").read_text())
GROUP_VARS = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
ENABLED = "tn_ai_enabled | default(true) | bool"
DISABLED = "not (tn_ai_enabled | default(true) | bool)"


def _bool(v):
    """Ansible's bool filter, for the values these templates feed it."""
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1", "on")


def _jinja() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    env.filters["bool"] = _bool
    env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
    return env


def _task(tasks: list[dict], name: str) -> dict:
    [task] = [t for t in tasks if t.get("name") == name]
    return task


def _when(task: dict) -> list[str]:
    w = task.get("when", [])
    return [w] if isinstance(w, str) else list(w)


# ── defaults ───────────────────────────────────────────────────────────


def test_ai_is_on_by_default_so_a_forgotten_url_still_fails():
    assert GROUP_VARS["tn_ai_enabled"] is True
    assert GROUP_VARS["tn_ai_base_url"] == ""
    url = _task(PREFLIGHT, "Preflight — the AI endpoint is configured")
    assert url["ansible.builtin.assert"]["that"] == "tn_ai_base_url | default('') | length > 0"
    assert _when(url) == [ENABLED]
    assert "tn_ai_enabled: false" in url["ansible.builtin.assert"]["fail_msg"]


# ── preflight ──────────────────────────────────────────────────────────


def test_preflight_skips_the_ai_probe_when_disabled_and_says_so():
    for name in ("Preflight — AI inference endpoint reachable", "Preflight — report AI endpoint"):
        assert _when(_task(PREFLIGHT, name)) == [ENABLED], name
    off = _task(PREFLIGHT, "Preflight — AI disabled")
    assert _when(off) == [DISABLED]
    assert "AI disabled (tn_ai_enabled=false): AI features will show 'not configured'" in off["ansible.builtin.debug"]["msg"]
    summary = _task(PREFLIGHT, "Preflight — summary")["ansible.builtin.debug"]["msg"]
    assert any(line.startswith("ai              :") and "disabled (tn_ai_enabled=false" in line for line in summary)


ARC2 = _task(PREFLIGHT, "Preflight — ARC² has its own model when AI is disabled")


@pytest.mark.parametrize(
    ("given", "passes"),
    [
        ({}, False),
        ({"tn_arc2_anthropic_base_url": "https://gw.example"}, True),
        ({"tn_arc2_local_url": "https://gw.example:8443"}, True),
        ({"vault_arc2_claude_oauth_token": "sk-ant-oat01-x"}, True),
        ({"vault_arc2_anthropic_api_key": "sk-ant-api03-x"}, True),
        ({"vault_arc2_claude_oauth_token": "CHANGE_ME", "vault_arc2_anthropic_api_key": "  "}, False),
    ],
)
def test_arc2_with_ai_disabled_needs_its_own_endpoint_or_credential(given, passes):
    assert _when(ARC2) == [DISABLED, "tn_arc2_enabled | default(false) | bool"]
    env = _jinja()
    hostvars = {
        "tn_arc2_anthropic_base_url": "",
        "tn_arc2_local_url": "",
        "vault_arc2_claude_oauth_token": "",
        "vault_arc2_anthropic_api_key": "",
        **given,
    }
    for k, v in ARC2["vars"].items():  # task vars, in order, as Ansible templates them
        hostvars[k] = env.from_string(v).render(**hostvars).strip()
    that = env.from_string("{{ " + ARC2["ansible.builtin.assert"]["that"] + " }}").render(**hostvars)
    assert that == str(passes)
    msg = ARC2["ansible.builtin.assert"]["fail_msg"]
    assert "tn_arc2_enabled: false" in msg and "{{ vault_arc2" not in msg  # names variables, never values


# ── 30-config ──────────────────────────────────────────────────────────


def _render_ai_block(**hostvars) -> dict[str, str]:
    start = ENV_J2.index("# ── AI orchestrator")
    block = ENV_J2[start : ENV_J2.index("# ── ARC² Course Studio", start)]
    out = _jinja().from_string(block).render(
        tn_ai_base_url="http://ai.example:4000/v1",
        vault_openai_api_key="sk-vault",
        tn_ai_default_model="chat",
        tn_ai_heavy_model="chat",
        tn_ai_general_model="chat",
        tn_ai_code_model="chat",
        tn_ai_embed_model="embed",
        **hostvars,
    )
    return dict(line.split("=", 1) for line in out.splitlines() if re.match(r"^[A-Z_]+=", line))


def test_enabled_renders_the_endpoint_and_key():
    env = _render_ai_block(tn_ai_enabled=True)
    assert (env["TN_AI_ENABLED"], env["OPENAI_BASE_URL"], env["OPENAI_API_KEY"]) == (
        "true", "http://ai.example:4000/v1", "sk-vault")
    assert _render_ai_block() == env  # unset is on


def test_disabled_renders_no_endpoint_and_no_key_even_with_one_in_the_vault():
    env = _render_ai_block(tn_ai_enabled=False)
    assert (env["TN_AI_ENABLED"], env["OPENAI_BASE_URL"], env["OPENAI_API_KEY"]) == ("false", "", "")
    assert "sk-vault" not in "".join(env.values())
    assert env["AI_MODEL_BACKEND"] == "openai"  # so ai_configured() is false, not the mock


def test_compose_accepts_an_empty_endpoint_and_gives_the_api_the_flag():
    orch = COMPOSE["services"]["ai-orchestrator"]["environment"]
    assert orch["OPENAI_BASE_URL"].startswith("${OPENAI_BASE_URL?") and ":-" not in orch["OPENAI_BASE_URL"]
    assert COMPOSE["services"]["api"]["environment"]["TN_AI_ENABLED"] == "${TN_AI_ENABLED:-true}"


# ── 99-validate ────────────────────────────────────────────────────────


def test_validate_checks_models_only_with_ai_on():
    for name in (
        "Validate — AI orchestrator's models exist on its endpoint",
        "Validate — parse the AI model check",
        "Validate — the AI orchestrator's models exist on its endpoint",
    ):
        assert _when(_task(VALIDATE["tasks"], name)) == [ENABLED], name
    warn = _task(VALIDATE["tasks"], "Validate — WARNING, the AI models could not be checked")
    assert _when(warn)[0] == ENABLED


def test_validate_with_ai_off_reports_disabled_and_requires_the_orchestrator_to_agree():
    names = [t["name"] for t in VALIDATE["tasks"]]
    probe = _task(VALIDATE["tasks"], "Validate — AI orchestrator reports AI not configured")
    assert _when(probe) == [DISABLED]
    assert "exec -T ai-orchestrator python -c" in probe["ansible.builtin.command"]["cmd"]
    assert "ai_configured" in probe["ansible.builtin.command"]["cmd"]
    gate = _task(VALIDATE["tasks"], "Validate — AI disabled, and the orchestrator agrees")
    assert _when(gate) == [DISABLED]
    assert "(tn_ai_off_health.stdout | default('') | trim) == 'false'" in gate["ansible.builtin.assert"]["that"]
    assert names.index("Validate — REPORT") < names.index("Validate — AI disabled, and the orchestrator agrees")
    report = _task(VALIDATE["tasks"], "Validate — REPORT")["ansible.builtin.debug"]["msg"]
    assert any("' AI            : disabled (tn_ai_enabled: false" in line for line in report)


# ── acceptance ─────────────────────────────────────────────────────────


def test_acceptance_uses_the_ai_disabled_profile_only_with_ai_off():
    profile = ACCEPTANCE["vars"]["tn_acceptance_skip_profile"]
    env = _jinja()
    assert env.from_string(profile).render(tn_ai_enabled=True) == "staging"
    assert env.from_string(profile).render() == "staging"
    assert env.from_string(profile).render(tn_ai_enabled=False) == "staging,ai-disabled"
    argv = _task(ACCEPTANCE["tasks"], "Acceptance — run the suite")["ansible.builtin.command"]["argv"]
    assert "['--absent', 'arc2'] if not (tn_ai_enabled | default(true) | bool)" in argv
