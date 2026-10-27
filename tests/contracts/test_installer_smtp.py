"""The app's email relay is installable (#306; install/README.md "Email (SMTP)").

The API sends roll-call onboarding emails, reminders, seat-forfeit notices and calendar
invites through app/notifications/smtp.py, which reads SMTP_*. These hold that the
installer's tn_smtp_* (and vault_smtp_password) reach the env file, compose's api service
and the Helm chart; that an empty tn_smtp_host is allowed (email off); that preflight
refuses a set host with an unusable port, sender or credential pair; and that alert email
defaults to the same relay so a site sets it once, still overridable.
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
CHART = ROOT / "infra/k8s/helm/truenorth-range"
CONFIG_ROLE = INSTALL / "roles/tn_config"
ENV_TEMPLATE = CONFIG_ROLE / "templates/env.production.j2"
PREFLIGHT = INSTALL / "roles/tn_preflight/tasks/main.yml"
GROUP_VARS = INSTALL / "inventory/group_vars/all/main.yml"
# A made-up value for the test only; never a real credential.
PASSWORD = "fake-relay-pw-for-the-test"

SET = {
    "tn_smtp_host": "smtp.corp.example",
    "tn_smtp_port": 587,
    "tn_smtp_security": "starttls",
    "tn_smtp_username": "truenorth-relay",
    "tn_smtp_from_address": "truenorth@corp.example",
    "vault_smtp_password": PASSWORD,
}
UNSET = {
    "tn_smtp_host": "",
    "tn_smtp_port": 587,
    "tn_smtp_security": "starttls",
    "tn_smtp_username": "",
    "tn_smtp_from_address": "",
    "vault_smtp_password": "",
}


def _jinja() -> jinja2.Environment:
    """Jinja as Ansible's template module runs it (trim_blocks), with the filters used here."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.update(
        bool=lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on"),
        to_json=json.dumps,
    )
    env.tests.update(
        search=lambda v, p: re.search(p, str(v)) is not None,
        match=lambda v, p: re.match(p, str(v)) is not None,
    )
    return env


def _expr(text: str, **ctx):
    text = str(text).strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    return _jinja().compile_expression(text.strip())(**ctx)


def _value(text, **ctx):
    """An Ansible variable value: a template string rendered, anything else as is."""
    if isinstance(text, str) and "{{" in text:
        return _jinja().from_string(text).render(**ctx)
    return text


def _env(**ctx) -> dict[str, str]:
    """The Email block of the rendered env file, as KEY -> value."""
    text = ENV_TEMPLATE.read_text()
    start = text.index("# ── Email (SMTP")
    end = text.index("# ── Trainee registration", start)
    out = _jinja().from_string(text[start:end]).render(**ctx)
    return dict(line.split("=", 1) for line in out.splitlines() if line and not line.startswith("#"))


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _task(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


# ── Defaults: off, documented ─────────────────────────────────────────
def test_smtp_is_off_by_default_and_documented():
    gv = yaml.safe_load(GROUP_VARS.read_text())
    assert gv["tn_smtp_host"] == "" and gv["tn_smtp_username"] == "" and gv["tn_smtp_from_address"] == ""
    assert gv["tn_smtp_port"] == 587 and gv["tn_smtp_security"] == "starttls"
    text = GROUP_VARS.read_text()
    block = text[text.index("# ── Email (SMTP"):text.index("tn_smtp_from_address:")]
    for word in ("starttls", "tls", "none", "vault_smtp_password", "banner"):
        assert word in block, word
    vault = yaml.safe_load((INSTALL / "inventory/group_vars/all/vault.yml.example").read_text())
    assert vault["vault_smtp_password"] == ""
    readme = (INSTALL / "README.md").read_text()
    assert "## Email (SMTP)" in readme
    section = readme[readme.index("## Email (SMTP)"):]
    section = section[: section.index("\n## ", 5)]
    for name in ("tn_smtp_host", "tn_smtp_port", "tn_smtp_security", "tn_smtp_username", "tn_smtp_from_address",
                 "vault_smtp_password", "tn_alert_smtp_host", "/email/status"):
        assert name in section, name


# ── The env file ─────────────────────────────────────────────────────
def test_env_renders_the_app_smtp_settings_when_set():
    env = _env(**SET)
    assert env["SMTP_HOST"] == "smtp.corp.example"
    assert env["SMTP_PORT"] == "587"
    assert env["SMTP_SECURITY"] == "starttls"
    assert env["SMTP_USER"] == "truenorth-relay"
    assert env["SMTP_FROM"] == "truenorth@corp.example"
    # Single-quoted: compose does not interpolate a $ in it.
    assert env["SMTP_PASSWORD"] == f"'{PASSWORD}'"


@pytest.mark.parametrize("security", ["starttls", "tls", "none"])
def test_env_carries_each_security_mode(security):
    env = _env(**{**SET, "tn_smtp_security": security, "tn_smtp_port": 465 if security == "tls" else 25})
    assert env["SMTP_SECURITY"] == security


def test_env_without_a_relay_renders_email_off_and_no_password():
    env = _env(**UNSET)
    assert env["SMTP_HOST"] == "" and env["SMTP_USER"] == "" and env["SMTP_PASSWORD"] == "" and env["SMTP_FROM"] == ""
    # Even a password left in the vault is not rendered while email is off.
    off = _env(**{**UNSET, "vault_smtp_password": PASSWORD})
    assert PASSWORD not in json.dumps(off)
    # An inventory from before this change (no tn_smtp_* at all) renders the same.
    assert _env()["SMTP_HOST"] == ""


def test_env_password_is_used_only_by_the_env_file_and_preflight():
    users = sorted(
        p.relative_to(INSTALL).as_posix()
        for p in [*INSTALL.rglob("*.yml"), *INSTALL.rglob("*.j2")]
        if "vault_smtp_password" in "\n".join(
            line for line in p.read_text().splitlines() if not line.lstrip().startswith("#")
        )
    )
    assert users == [
        "inventory/group_vars/all/main.yml",  # the alert password's default
        "roles/tn_config/templates/env.production.j2",
        "roles/tn_preflight/tasks/main.yml",
    ]


def test_compose_passes_smtp_to_the_api():
    """The api runs the class and scheduler clocks, which send the mail."""
    api = yaml.safe_load((DOCKER / "compose.prod.yml").read_text().replace("!reset", "").replace("!override", ""))[
        "services"
    ]["api"]["environment"]
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        assert api[key].startswith(f"${{{key}:-"), key
    example = (DOCKER / ".env.production.example").read_text()
    for key in ("SMTP_HOST=", "SMTP_PORT=", "SMTP_SECURITY=", "SMTP_USER=", "SMTP_PASSWORD=", "SMTP_FROM="):
        assert f"\n{key}" in example, key


# ── Preflight ─────────────────────────────────────────────────────────
def _preflight(**ctx) -> tuple[bool | None, str]:
    """(passed, message) for the SMTP check; passed is None when it does not run."""
    task = _task(PREFLIGHT, "Preflight — app email (SMTP) settings are usable")
    if not _expr(task["when"], **ctx):
        return None, ""
    scope = {**ctx}
    for name, value in task["vars"].items():
        scope[name] = _expr(value, **scope) if name == "_problems" else _value(value, **scope)
    ok = _expr(task["ansible.builtin.assert"]["that"], **scope)
    msg = _jinja().from_string(task["ansible.builtin.assert"]["fail_msg"]).render(**scope)
    return ok, msg


def test_preflight_allows_no_relay_and_says_email_is_off():
    assert _preflight(**UNSET) == (None, "")
    assert _preflight() == (None, "")  # an inventory without tn_smtp_*
    off = _task(PREFLIGHT, "Preflight — app email is off (nothing will be emailed)")
    assert _expr(off["when"], **UNSET) and not _expr(off["when"], **SET)
    assert "NOT sent" in off["ansible.builtin.debug"]["msg"]


@pytest.mark.parametrize(
    ("case", "over", "ok", "named"),
    [
        ("complete", {}, True, None),
        ("implicit TLS", {"tn_smtp_port": 465, "tn_smtp_security": "tls"}, True, None),
        ("unauthenticated relay", {"tn_smtp_username": "", "vault_smtp_password": ""}, True, None),
        ("port as a string", {"tn_smtp_port": "25"}, True, None),
        ("a host-only sender", {"tn_smtp_from_address": "truenorth@tn-mgmt01"}, True, None),
        ("port zero", {"tn_smtp_port": 0}, False, "tn_smtp_port"),
        ("port too high", {"tn_smtp_port": 70000}, False, "tn_smtp_port"),
        ("port not a number", {"tn_smtp_port": "smtp"}, False, "tn_smtp_port"),
        ("unknown security", {"tn_smtp_security": "ssl"}, False, "tn_smtp_security"),
        ("no sender", {"tn_smtp_from_address": ""}, False, "tn_smtp_from_address"),
        ("sender without @", {"tn_smtp_from_address": "truenorth"}, False, "tn_smtp_from_address"),
        ("sender with a display name", {"tn_smtp_from_address": "TN <tn@corp.example>"}, False, "tn_smtp_from_address"),
        ("sender with two @", {"tn_smtp_from_address": "a@b@corp.example"}, False, "tn_smtp_from_address"),
        ("host with a scheme", {"tn_smtp_host": "smtp://relay"}, False, "tn_smtp_host"),
        ("username without password", {"vault_smtp_password": ""}, False, "set both"),
        ("password without username", {"tn_smtp_username": ""}, False, "set both"),
        ("password with a quote", {"vault_smtp_password": "it's"}, False, "single quote"),
        ("sign-in in clear", {"tn_smtp_security": "none"}, False, "in clear"),
        ("no encryption, no sign-in", {"tn_smtp_security": "none", "tn_smtp_username": "", "vault_smtp_password": ""},
         True, None),
        ("placeholder password", {"vault_smtp_password": "CHANGE_ME"}, False, "CHANGE_ME"),
    ],
)
def test_preflight_validates_a_set_relay(case, over, ok, named):
    held, msg = _preflight(**{**SET, **over})
    assert held is ok, (case, msg)
    if named:
        assert named in msg, case
    assert PASSWORD not in msg and "it's" not in msg


# ── Alert email defaults to the same relay ────────────────────────────
ALERT_KEYS = (
    "tn_alert_email_from",
    "tn_alert_smtp_host",
    "tn_alert_smtp_port",
    "tn_alert_smtp_require_tls",
    "tn_alert_smtp_username",
    "tn_alert_smtp_password",
)


def _alert_defaults(**ctx) -> dict:
    gv = yaml.safe_load(GROUP_VARS.read_text())
    return {k: _value(gv[k], **ctx).strip() if isinstance(gv[k], str) else gv[k] for k in ALERT_KEYS}


def test_alert_email_defaults_to_the_app_relay():
    a = _alert_defaults(**SET)
    assert a["tn_alert_smtp_host"] == "smtp.corp.example"
    assert int(a["tn_alert_smtp_port"]) == 587
    assert a["tn_alert_email_from"] == "truenorth@corp.example"
    assert a["tn_alert_smtp_require_tls"] == "True"
    assert a["tn_alert_smtp_username"] == "truenorth-relay"
    assert a["tn_alert_smtp_password"] == PASSWORD
    assert _alert_defaults(**{**SET, "tn_smtp_security": "none"})["tn_alert_smtp_require_tls"] == "False"
    # Without vault_smtp_password at all (an old vault.yml), no auth rather than an error.
    no_pw = {k: v for k, v in SET.items() if k != "vault_smtp_password"}
    assert _alert_defaults(**no_pw)["tn_alert_smtp_password"] == ""


def test_the_rendered_alertmanager_config_uses_the_app_relay():
    resolved = _alert_defaults(**SET)
    text = (CONFIG_ROLE / "templates/alertmanager.email.yml.j2").read_text()
    ctx = {
        **SET,
        **resolved,
        "tn_alert_email_to": "ops@corp.example",
        "tn_domain_fqdn": "tn.corp.example",
        "vault_alertmanager_webhook_url": "",
    }
    out = _jinja().from_string(text).render(**ctx)
    g = yaml.safe_load(out)["global"]
    assert g["smtp_smarthost"] == "smtp.corp.example:587"
    assert g["smtp_from"] == "truenorth@corp.example"
    assert g["smtp_require_tls"] is True
    assert g["smtp_auth_username"] == "truenorth-relay"
    assert PASSWORD not in out


def test_alert_credentials_can_still_be_overridden():
    """vault_alert_smtp_* win wherever the vault sets them (whatever the file is called);
    tn_alert_* are ordinary inventory variables a host or group overrides."""
    other = {"vault_alert_smtp_username": "alerts-only", "vault_alert_smtp_password": "fake-alert-pw"}
    a = _alert_defaults(**SET, **other)
    assert (a["tn_alert_smtp_username"], a["tn_alert_smtp_password"]) == ("alerts-only", "fake-alert-pw")
    # Explicitly no sign-in for alerts, while the app signs in.
    a = _alert_defaults(**SET, vault_alert_smtp_username="", vault_alert_smtp_password="")
    assert (a["tn_alert_smtp_username"], a["tn_alert_smtp_password"]) == ("", "")
    # A half-set override does not borrow the app's password: preflight then says "Set both".
    a = _alert_defaults(**SET, vault_alert_smtp_username="alerts-only")
    assert (a["tn_alert_smtp_username"], a["tn_alert_smtp_password"]) == ("alerts-only", "")
    example = (INSTALL / "inventory/group_vars/all/vault.yml.example").read_text()
    assert "# vault_alert_smtp_username:" in example and "# vault_alert_smtp_password:" in example
    # The preflight check for alert email still names the alert variables.
    task = _task(PREFLIGHT, "Preflight — alert email is complete")
    assert "tn_alert_smtp_host" in task["vars"]["_missing"]


# ── Helm ─────────────────────────────────────────────────────────────
HELM = shutil.which("helm")
needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")
KIND_VALUES = ROOT / "infra/k8s/kind/values-kind.yaml"
HELM_SECRETS = {
    "databasePassword": "d" * 32,
    "redisPassword": "r" * 32,
    "minioAccessKey": "tn-access",
    "minioSecretKey": "m" * 32,
    "csrfSecret": "c" * 32,
    "secretsKey": "s" * 32,
    "aiServiceToken": "a" * 32,
    "metricsScrapeToken": "t" * 32,
    "keycloakMfaAdminPassword": "k" * 32,
    "keycloakAdminClientSecret": "x" * 32,
}


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    secrets = [a for k, v in HELM_SECRETS.items() for a in ("--set-string", f"secrets.{k}={v}")]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [HELM, "template", "tn", str(CHART), "-n", "truenorth", "-f", str(KIND_VALUES), *secrets, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _config_and_secret(*args: str) -> tuple[dict, dict]:
    import base64

    proc = _helm(*args)
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    sec = next(d for d in docs if d["kind"] == "Secret")["data"]
    return cm, {k: base64.b64decode(v).decode() for k, v in sec.items()}


@needs_helm
def test_helm_without_smtp_renders_email_off():
    cm, sec = _config_and_secret()
    assert cm["SMTP_HOST"] == ""
    assert "SMTP_PASSWORD" not in sec


@needs_helm
def test_helm_renders_smtp_with_the_password_only_in_the_secret():
    cm, sec = _config_and_secret(
        "--set", "config.smtp.host=smtp.corp.example",
        "--set", "config.smtp.port=465",
        "--set", "config.smtp.security=tls",
        "--set", "config.smtp.username=truenorth-relay",
        "--set", "config.smtp.fromAddress=truenorth@corp.example",
        "--set-string", f"secrets.smtpPassword={PASSWORD}",
    )
    assert cm["SMTP_HOST"] == "smtp.corp.example" and cm["SMTP_PORT"] == "465"
    assert cm["SMTP_SECURITY"] == "tls" and cm["SMTP_USER"] == "truenorth-relay"
    assert cm["SMTP_FROM"] == "truenorth@corp.example"
    assert PASSWORD not in json.dumps(cm)
    assert sec["SMTP_PASSWORD"] == PASSWORD


@needs_helm
@pytest.mark.parametrize(
    ("args", "named"),
    [
        (("--set", "config.smtp.fromAddress=nope"), "config.smtp.fromAddress"),
        (("--set", "config.smtp.fromAddress=a@corp.example", "--set", "config.smtp.security=ssl"), "config.smtp.security"),
        (("--set", "config.smtp.fromAddress=a@corp.example", "--set", "config.smtp.username=u"), "secrets.smtpPassword"),
        (("--set", "config.smtp.fromAddress=a@corp.example", "--set", "config.smtp.port=70000"), "config.smtp.port"),
        (
            ("--set", "config.smtp.fromAddress=a@corp.example", "--set", "config.smtp.username=u",
             "--set", "config.smtp.security=none", "--set-string", "secrets.smtpPassword=fake-x"),
            "in clear",
        ),
        (
            ("--set", "config.smtp.fromAddress=a@corp.example", "--set-string", "config.extraEnv.SMTP_FROM=x@corp.example"),
            "move them to config.smtp",
        ),
    ],
)
def test_helm_refuses_an_unusable_relay(args, named):
    proc = _helm("--set", "config.smtp.host=smtp.corp.example", *args)
    assert proc.returncode != 0
    assert named in proc.stderr


@needs_helm
def test_helm_keeps_a_release_that_set_smtp_through_extra_env():
    """The chart's advice before config.smtp: SMTP_* in config.extraEnv / secrets.extraEnv.
    Such a release renders each key once, with its own values."""
    proc = _helm(
        "--set-string", "config.extraEnv.SMTP_HOST=relay.old.example",
        "--set-string", "config.extraEnv.SMTP_FROM=tn@old.example",
        "--set-string", f"secrets.extraEnv.SMTP_PASSWORD={PASSWORD}",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("SMTP_HOST:") == 1
    cm, sec = _config_and_secret(
        "--set-string", "config.extraEnv.SMTP_HOST=relay.old.example",
        "--set-string", f"secrets.extraEnv.SMTP_PASSWORD={PASSWORD}",
    )
    assert cm["SMTP_HOST"] == "relay.old.example" and sec["SMTP_PASSWORD"] == PASSWORD


@needs_helm
def test_helm_refuses_the_password_twice():
    proc = _helm("--set-string", "secrets.smtpPassword=fake-a", "--set-string", "secrets.extraEnv.SMTP_PASSWORD=fake-b")
    assert proc.returncode != 0 and "secrets.smtpPassword" in proc.stderr
