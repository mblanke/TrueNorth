"""Alert delivery by email (install/README.md "Alerting"; docs/operations.md "Alert delivery").

Off (the default), Alertmanager keeps the shipped null receiver and the installer says, in
30-config and again in 99-validate, that alerts are NOT delivered. On
(tn_alert_email_enabled), 30-config renders an email receiver: criticals at once, warnings
batched, resolutions mailed; the SMTP password only in a 0600 file
(smtp_auth_password_file), never in the config; and preflight refuses a CHANGE_ME
credential or a missing recipient, sender or relay. playbooks/alert-test.yml (tag
tn-alert-test) fires a synthetic alert to prove delivery.

The rendered configs are checked with `amtool check-config` from the alertmanager image
compose.prod.yml pins, through docker (amtool on PATH also works); with neither, that test
skips.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
DOCKER = ROOT / "infra/platform/docker"
SHIPPED = DOCKER / "monitoring/alertmanager"
CONFIG_ROLE = INSTALL / "roles/tn_config"
TEMPLATE = CONFIG_ROLE / "templates/alertmanager.email.yml.j2"
PASSWORD = "s3cr3t-smtp-pa55word-for-the-test"
USERNAME = "alerts@relay.example.org"

ON = {
    "tn_alert_email_enabled": True,
    "tn_alert_email_to": "ops@example.org, oncall@example.org",
    "tn_alert_email_from": "truenorth@example.org",
    "tn_alert_smtp_host": "smtp.example.org",
    "tn_alert_smtp_port": 587,
    "tn_alert_smtp_require_tls": True,
    "tn_domain_fqdn": "tn-staging.corp.tnrange.lab",
    "tn_config_dir": "/srv/truenorth/config",
    "tn_alert_smtp_username": USERNAME,
    "tn_alert_smtp_password": PASSWORD,
    "vault_alertmanager_webhook_url": "",
}
OFF = {**ON, "tn_alert_email_enabled": False}


def _code(text: str) -> str:
    """The text without its comment lines (a variable named in a comment is not a use)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _jinja() -> jinja2.Environment:
    """Jinja as Ansible's template module runs it (trim_blocks), with the filters used here."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.update(
        bool=lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on"),
        to_json=json.dumps,
    )
    env.tests.update(search=lambda v, p: re.search(p, str(v)) is not None)
    return env


def _expr(text: str, **ctx):
    """Evaluate an Ansible `{{ ... }}` value (or a bare `when:` expression)."""
    text = text.strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    return _jinja().compile_expression(text.strip())(**ctx)


def _render_email(**over) -> str:
    return _jinja().from_string(TEMPLATE.read_text()).render(**{**ON, **over})


def _email(**over) -> dict:
    return yaml.safe_load(_render_email(**over))


def _tasks(path: Path) -> list[dict]:
    return _yaml(path)


def _task(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _env_block(**ctx) -> dict[str, str]:
    """The Alertmanager lines of the rendered env file."""
    text = (CONFIG_ROLE / "templates/env.production.j2").read_text()
    start = text.index("# Alertmanager:")
    end = text.index("\n", text.index("ALERTMANAGER_SECRETS_DIR=", start))
    out = _jinja().from_string(text[start:end]).render(**ctx)
    return dict(line.split("=", 1) for line in out.splitlines() if line and not line.startswith("#"))


def _mount_source(env: dict[str, str]) -> Path:
    """Where compose.prod.yml takes /etc/alertmanager/alertmanager.yml from, given the env file."""
    [vol] = [
        v
        for v in _yaml(DOCKER / "compose.prod.yml")["services"]["alertmanager"]["volumes"]
        if v.endswith(":/etc/alertmanager/alertmanager.yml:ro")
    ]
    src = vol.rsplit(":", 2)[0]
    src = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), src)
    return Path(src) if src.startswith("/") else DOCKER / src


# ── Defaults: off, except staging ─────────────────────────────────────
def test_alert_email_is_off_by_default_and_on_staging_until_smtp_is_known():
    gv = _yaml(INSTALL / "inventory/group_vars/all/main.yml")
    assert gv["tn_alert_email_enabled"] is False
    assert gv["tn_alert_email_to"] == ""
    # The relay, sender and credentials default to the app's tn_smtp_* (#306; their
    # rendering is tests/contracts/test_installer_smtp.py), which default to empty.
    assert gv["tn_alert_smtp_host"] == "{{ tn_smtp_host }}" and gv["tn_smtp_host"] == ""
    assert gv["tn_alert_email_from"] == "{{ tn_smtp_from_address }}" and gv["tn_smtp_from_address"] == ""
    assert gv["tn_alert_smtp_port"] == "{{ tn_smtp_port }}" and gv["tn_smtp_port"] == 587
    assert "tn_smtp_security" in gv["tn_alert_smtp_require_tls"] and gv["tn_smtp_security"] == "starttls"
    staging = _yaml(INSTALL / "inventory/staging.yml")["all"]["children"]["platform"]["hosts"]["tn-staging"]
    # Off on staging until the owner has SMTP details; a one-line flip, the rest pre-written.
    assert staging["tn_alert_email_enabled"] is False
    text = (INSTALL / "inventory/staging.yml").read_text()
    for name in ("tn_alert_email_to", "tn_smtp_host", "tn_smtp_from_address"):
        assert f"# {name}:" in text
    # The alert credentials are an override now: commented out, so the defaults apply.
    vault = _yaml(INSTALL / "inventory/group_vars/all/vault.yml.example")
    assert "vault_alert_smtp_username" not in vault and "vault_alert_smtp_password" not in vault
    assert "vault_alert_smtp_username" in gv["tn_alert_smtp_username"]
    assert "tn_smtp_username" in gv["tn_alert_smtp_username"]


# ── Off: the null receiver, and a warning ─────────────────────────────
def test_off_selects_the_shipped_null_receiver():
    env = _env_block(**OFF)
    assert env["ALERTMANAGER_CONFIG"] == "alertmanager.yml"
    assert "ALERTMANAGER_CONFIG_DIR" not in env
    src = _mount_source(env)
    assert src == SHIPPED / "alertmanager.yml"
    cfg = _yaml(src)
    assert cfg["receivers"] == [{"name": "null"}] and cfg["route"]["receiver"] == "null"
    # A webhook alone still selects the shipped webhook config.
    assert _env_block(**{**OFF, "vault_alertmanager_webhook_url": "https://hook"})["ALERTMANAGER_CONFIG"] == (
        "alertmanager.webhook.yml"
    )


def test_off_warns_in_config_and_in_validate():
    tasks = CONFIG_ROLE / "tasks/main.yml"
    warn = _task(tasks, "Config — ALERTS GO NOWHERE")
    assert all(_expr(w, **OFF) for w in warn["when"])
    assert not all(_expr(w, **ON) for w in warn["when"])

    validate = _yaml(INSTALL / "playbooks/99-validate.yml")[0]["tasks"]
    fact = next(t for t in validate if t["name"] == "Validate — alert delivery")
    delivery = _expr(fact["ansible.builtin.set_fact"]["tn_alert_delivery"], **OFF)
    assert delivery == "NOWHERE (null receiver)"
    on = _expr(fact["ansible.builtin.set_fact"]["tn_alert_delivery"], **ON)
    assert on.startswith("email to ops@example.org") and PASSWORD not in on
    warning = next(t for t in validate if t["name"] == "Validate — WARNING, alerts are not delivered")
    assert "ALERTS ARE NOT DELIVERED" in warning["ansible.builtin.debug"]["msg"]
    assert _expr(warning["when"], tn_alert_delivery=delivery)
    assert not _expr(warning["when"], tn_alert_delivery=on)
    report = next(t for t in validate if t["name"] == "Validate — REPORT")
    assert any("{{ tn_alert_delivery }}" in line for line in report["ansible.builtin.debug"]["msg"])


def test_off_removes_the_email_files():
    rm = _task(CONFIG_ROLE / "tasks/main.yml", "Config — no alert email files while it is off (or without SMTP auth)")
    assert rm["ansible.builtin.file"]["state"] == "absent"
    assert _expr(rm["loop"], **OFF) == ["alertmanager.email.yml", "smtp_password"]
    assert _expr(rm["loop"], **ON) == []
    assert _expr(rm["loop"], **{**ON, "tn_alert_smtp_username": "", "tn_alert_smtp_password": ""}) == [
        "smtp_password"
    ]


# ── On: an email receiver, the password in a file ─────────────────────
def test_on_selects_the_rendered_email_config():
    env = _env_block(**ON)
    assert env["ALERTMANAGER_CONFIG_DIR"] == "/srv/truenorth/config/alertmanager"
    assert env["ALERTMANAGER_CONFIG"] == "alertmanager.email.yml"
    assert env["ALERTMANAGER_SECRETS_DIR"] == env["ALERTMANAGER_CONFIG_DIR"]
    assert _mount_source(env) == Path("/srv/truenorth/config/alertmanager/alertmanager.email.yml")
    write = _task(CONFIG_ROLE / "tasks/main.yml", "Config — Alertmanager email receiver")
    assert write["ansible.builtin.template"]["dest"] == "{{ tn_config_dir }}/alertmanager/alertmanager.email.yml"
    # compose mounts ALERTMANAGER_SECRETS_DIR where the config reads the password file.
    vols = _yaml(DOCKER / "compose.prod.yml")["services"]["alertmanager"]["volumes"]
    assert any(v.startswith("${ALERTMANAGER_SECRETS_DIR") and v.endswith(":/etc/alertmanager/secrets:ro") for v in vols)


def test_on_routes_criticals_at_once_and_batches_warnings():
    cfg = _email()
    route = cfg["route"]
    assert route["receiver"] == "email"
    [critical] = route["routes"]
    assert critical["matchers"] == ['severity="critical"'] and critical["receiver"] == "email"
    assert critical["group_wait"] == "0s"
    assert critical["repeat_interval"] == "1h"
    # Warnings (the default route) wait to batch, and repeat less often than criticals.
    assert route["group_wait"] == "5m" and route["group_interval"] == "15m" and route["repeat_interval"] == "12h"
    # Same inhibition as the shipped configs.
    assert cfg["inhibit_rules"] == _yaml(SHIPPED / "alertmanager.yml")["inhibit_rules"]
    [receiver] = cfg["receivers"]
    [email] = receiver["email_configs"]
    assert email["send_resolved"] is True
    assert email["to"] == "ops@example.org, oncall@example.org"
    assert email["headers"]["Subject"].startswith("[TrueNorth tn-staging.corp.tnrange.lab] {{ template ")
    assert "webhook_configs" not in receiver


def test_on_keeps_the_password_out_of_the_config():
    text = _render_email()
    assert PASSWORD not in text
    g = yaml.safe_load(text)["global"]
    assert g["smtp_smarthost"] == "smtp.example.org:587"
    assert g["smtp_from"] == "truenorth@example.org"
    assert g["smtp_require_tls"] is True
    assert g["smtp_auth_username"] == USERNAME
    assert g["smtp_auth_password_file"] == "/etc/alertmanager/secrets/smtp_password"
    assert "smtp_auth_password" not in g and "smtp_auth_secret" not in g
    # No credentials: no auth at all (an unauthenticated relay), and still no password.
    bare = _email(tn_alert_smtp_username="", tn_alert_smtp_password="")["global"]
    assert not any(k.startswith("smtp_auth") for k in bare)


def test_on_with_a_webhook_sends_to_both():
    [receiver] = _email(vault_alertmanager_webhook_url="https://hook.example.org/x")["receivers"]
    assert receiver["email_configs"] and receiver["webhook_configs"][0]["url_file"] == (
        "/etc/alertmanager/secrets/webhook_url"
    )
    assert "hook.example.org" not in _render_email(vault_alertmanager_webhook_url="https://hook.example.org/x")


def test_the_password_file_is_0600_and_nothing_is_logged():
    tasks = CONFIG_ROLE / "tasks/main.yml"
    pw = _task(tasks, "Config — Alertmanager SMTP password")
    copy = pw["ansible.builtin.copy"]
    assert copy["dest"] == "{{ tn_config_dir }}/alertmanager/smtp_password"
    assert copy["mode"] == "0600" and copy["owner"] == copy["group"] == "65534"
    assert "tn_alert_smtp_password" in copy["content"]
    assert pw["no_log"] is True
    cfg = _task(tasks, "Config — Alertmanager email receiver")
    assert cfg["no_log"] is True and cfg["ansible.builtin.template"]["mode"] == "0400"
    for task in (pw, cfg):
        assert task["notify"] == "Restart Alertmanager"
    handler = _yaml(CONFIG_ROLE / "handlers/main.yml")[0]
    assert handler["name"] == "Restart Alertmanager" and "docker restart tn-alertmanager" in str(handler)
    # The password is read by no other task in the installer.
    users = [
        p
        for p in [*INSTALL.rglob("*.yml"), *INSTALL.rglob("*.j2")]
        if "tn_alert_smtp_password" in _code(p.read_text())
    ]
    assert sorted(p.relative_to(INSTALL).as_posix() for p in users) == [
        "inventory/group_vars/all/main.yml",  # its definition: the vault's, else the app's (#306)
        "roles/tn_config/tasks/main.yml",
        "roles/tn_preflight/tasks/main.yml",
    ]
    for name in ("tn_alert_smtp_password", "vault_alert_smtp_password"):
        assert name not in TEMPLATE.read_text()
        assert name not in (CONFIG_ROLE / "templates/env.production.j2").read_text()


# ── Preflight ─────────────────────────────────────────────────────────
PREFLIGHT = INSTALL / "roles/tn_preflight/tasks/main.yml"


def _complete(**ctx) -> tuple[bool, str]:
    task = _task(PREFLIGHT, "Preflight — alert email is complete")
    assert _expr(task["when"], **ctx), "the check runs whenever alert email is on"
    scope = {**ctx}
    for name, value in task["vars"].items():
        scope[name] = _expr(value, **scope)
    that = task["ansible.builtin.assert"]["that"]
    ok = all(_expr(c, **scope) for c in that)
    msg = _jinja().from_string(task["ansible.builtin.assert"]["fail_msg"]).render(**scope)
    return ok, msg


@pytest.mark.parametrize(
    ("case", "over", "ok", "named"),
    [
        ("complete", {}, True, None),
        ("unauthenticated relay", {"tn_alert_smtp_username": "", "tn_alert_smtp_password": ""}, True, None),
        ("placeholder username", {"tn_alert_smtp_username": "CHANGE_ME"}, False, "CHANGE_ME"),
        ("placeholder password", {"tn_alert_smtp_password": "CHANGE_ME"}, False, "CHANGE_ME"),
        (
            "both placeholders (the vault template)",
            {"tn_alert_smtp_username": "CHANGE_ME", "tn_alert_smtp_password": "CHANGE_ME"},
            False,
            "CHANGE_ME",
        ),
        ("username without password", {"tn_alert_smtp_password": ""}, False, "Set both"),
        ("no recipient", {"tn_alert_email_to": ""}, False, "tn_alert_email_to"),
        ("no sender", {"tn_alert_email_from": " "}, False, "tn_alert_email_from"),
        ("no relay", {"tn_alert_smtp_host": ""}, False, "tn_alert_smtp_host"),
    ],
)
def test_preflight_refuses_an_incomplete_alert_email(case, over, ok, named):
    held, msg = _complete(**{**ON, **over})
    assert held is ok, case
    if named:
        assert named in msg, case
    assert PASSWORD not in msg and USERNAME not in msg


def test_30_config_rechecks_before_rendering():
    """30-config runs on its own too; it must not render a receiver with no relay."""
    task = _task(CONFIG_ROLE / "tasks/main.yml", "Config — alert email settings are usable")
    assert _expr(task["when"], **ON) and not _expr(task["when"], **OFF)

    def holds(**over):
        return all(_expr(c, **{**ON, **over}) for c in task["ansible.builtin.assert"]["that"])

    assert holds()
    assert holds(tn_alert_smtp_username="", tn_alert_smtp_password="")
    assert not holds(tn_alert_smtp_host="")
    assert not holds(tn_alert_email_to=" ")
    assert not holds(tn_alert_smtp_password="CHANGE_ME")
    names = [t.get("name") for t in _tasks(CONFIG_ROLE / "tasks/main.yml")]
    assert names.index("Config — alert email settings are usable") < names.index("Config — Alertmanager email receiver")


def test_preflight_ignores_the_smtp_placeholders_while_alert_email_is_off():
    task = _task(PREFLIGHT, "Preflight — no vault value is still a CHANGE_ME placeholder")
    ctx = {"tn_ldap_enabled": True, "tn_uses_vcenter": True}
    skip_off = _expr(task["vars"]["_skip"], **ctx, tn_alert_email_enabled=False)
    skip_on = _expr(task["vars"]["_skip"], **ctx, tn_alert_email_enabled=True)
    assert {"vault_alert_smtp_username", "vault_alert_smtp_password"} <= set(skip_off)
    assert not {"vault_alert_smtp_username", "vault_alert_smtp_password"} & set(skip_on)
    # An inventory from before this change (no tn_alert_email_enabled) counts as off.
    assert "vault_alert_smtp_password" in _expr(task["vars"]["_skip"], **ctx)


# ── The test alert ────────────────────────────────────────────────────
def test_alert_test_playbook_fires_a_synthetic_critical_and_is_a_site_tag():
    [play] = _yaml(INSTALL / "playbooks/alert-test.yml")
    assert "tn-alert-test" in play["tags"]
    fire = next(t for t in play["tasks"] if "ansible.builtin.shell" in t)
    cmd = fire["ansible.builtin.shell"]["cmd"]
    assert "docker exec tn-alertmanager" in cmd and "amtool --alertmanager.url=http://localhost:9093 alert add" in cmd
    assert "TrueNorthAlertTest" in cmd and "severity=critical" in cmd and "--end=" in cmd
    assert 'run="$(date +%s)"' in cmd, "a fresh label each run, or a repeat within 5 minutes mails nothing"
    # Per-request counters: notifications_total moves before the outcome is known, so a
    # refused message once read as sent.
    assert "alertmanager_notification_requests$1_total{" in cmd and "count _failed" in cmd
    assert "alertmanager_notifications$1" not in cmd
    guard = next(t for t in play["tasks"] if t["name"] == "Alert test — there is a receiver to test")
    assert not _expr(guard["ansible.builtin.assert"]["that"], **OFF)
    assert _expr(guard["ansible.builtin.assert"]["that"], **ON)
    site = _yaml(INSTALL / "site.yml")
    entry = next(e for e in site if e["import_playbook"] == "playbooks/alert-test.yml")
    assert entry["tags"] == ["never", "tn-alert-test"], "never part of a normal install"
    # The manual command in the docs is the same command.
    readme = (INSTALL / "README.md").read_text()
    assert "playbooks/alert-test.yml" in readme and "amtool --alertmanager.url=http://localhost:9093" in readme
    assert "alert add TrueNorthAlertTest" in readme
    ops = (ROOT / "docs/operations.md").read_text()
    assert "### Alert delivery" in ops and "playbooks/alert-test.yml" in ops


# ── amtool check-config on what is rendered and shipped ───────────────
def _alertmanager_image() -> str:
    return _yaml(DOCKER / "compose.prod.yml")["services"]["alertmanager"]["image"]


def _amtool_check(directory: Path, *names: str) -> subprocess.CompletedProcess:
    if shutil.which("amtool"):
        cmd = ["amtool", "check-config", *(str(directory / n) for n in names)]
    else:
        if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            pytest.skip("needs amtool on PATH, or a working docker for the pinned prom/alertmanager image")
        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{directory}:/c:ro",
            "--entrypoint",
            "amtool",
            _alertmanager_image(),
            "check-config",
            *(f"/c/{n}" for n in names),
        ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def test_amtool_accepts_the_rendered_and_shipped_configs(tmp_path):
    variants = {
        "email.yml": {},
        "email-noauth.yml": {"tn_alert_smtp_username": "", "tn_alert_smtp_password": ""},
        "email-webhook.yml": {"vault_alertmanager_webhook_url": "https://hook.example.org/x"},
        "email-465.yml": {"tn_alert_smtp_port": 465, "tn_alert_smtp_require_tls": False},
    }
    for name, over in variants.items():
        (tmp_path / name).write_text(_render_email(**over))
    for name in ("alertmanager.yml", "alertmanager.webhook.yml"):
        shutil.copy(SHIPPED / name, tmp_path / name)
    names = [*variants, "alertmanager.yml", "alertmanager.webhook.yml"]
    tmp_path.chmod(0o755)
    for n in names:
        (tmp_path / n).chmod(0o644)
    run = _amtool_check(tmp_path, *names)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert out.count("SUCCESS") == len(names), out
