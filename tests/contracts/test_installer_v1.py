"""Installer v1.0.0 contracts (install/README.md; docs/release.md; docs/runbooks/).

Each test pins one fix from the pre-release review, so it cannot quietly regress:
the installer/app compatibility marker, secrets out of process arguments, the API's
least-privilege Keycloak identity, no committed AI endpoint defaults, the secret
generation and drift rules, signed releases verified by the installer, and the backup
escrow the install requires.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"


def _prod() -> dict:
    return yaml.safe_load((DOCKER / "compose.prod.yml").read_text())


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return dict(item.split("=", 1) for item in env) if isinstance(env, list) else env


def _cmd(service: dict) -> str:
    cmd = service.get("command") or ""
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


# ── 1. Installer and app agree on the interface ───────────────────────
def test_compose_and_installer_declare_the_same_compat_level():
    mine = (INSTALL / "COMPAT").read_text().strip()
    assert mine.isdigit()
    assert str(_prod()["x-truenorth-installer-compat"]) == mine


def test_the_app_version_defaults_to_the_installers_own_commit():
    gv = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert gv["tn_app_git_version"] == "", "a pinned SHA goes stale; default to the installer's commit"
    release = (INSTALL / "roles/tn_release/tasks/main.yml").read_text()
    assert "rev-parse, HEAD" in release
    source = (INSTALL / "roles/tn_app_source/tasks/main.yml").read_text()
    assert "x-truenorth-installer-compat" in source
    assert "merge-base --is-ancestor" in source and "tn_allow_downgrade" in source


# ── 7. No secret is a process argument ────────────────────────────────
def test_redis_password_is_not_an_argument():
    redis = _prod()["services"]["redis"]
    cmd = _cmd(redis)
    assert "--requirepass" not in cmd and "${REDIS_PASSWORD}" not in cmd
    assert "-a" not in redis["healthcheck"]["test"]
    assert "REDISCLI_AUTH" in _env(redis)


def test_flower_takes_its_secrets_from_the_environment():
    flower = _prod()["services"]["flower"]
    cmd = _cmd(flower)
    assert "--broker_api" not in cmd and "--basic_auth" not in cmd
    env = _env(flower)
    assert "FLOWER_BROKER_API" in env and "FLOWER_BASIC_AUTH" in env


def test_migration_url_is_not_on_the_command_line():
    text = (INSTALL / "roles/tn_migrate/tasks/main.yml").read_text()
    tasks = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "-e DATABASE_URL=" not in tasks
    assert re.search(r"-e DATABASE_URL\s", tasks)


def test_ldap_bind_password_goes_through_a_file():
    tls = (INSTALL / "roles/tn_tls/tasks/main.yml").read_text()
    assert "- -w\n" not in tls and "- -y\n" in tls


# ── 9. The API never holds the master admin ───────────────────────────
def test_api_uses_a_service_account_not_the_master_admin():
    api = _env(_prod()["services"]["api"])
    assert "KEYCLOAK_ADMIN_PASSWORD" not in api and "KEYCLOAK_ADMIN_USER" not in api
    assert "KEYCLOAK_ADMIN_CLIENT_ID" in api and "KEYCLOAK_ADMIN_CLIENT_SECRET" in api
    defaults = yaml.safe_load((INSTALL / "roles/tn_keycloak/defaults/main.yml").read_text())
    assert defaults["tn_kc_api_admin_roles"] == ["view-realm", "manage-users"]
    assert defaults["tn_kc_client_settings"]["truenorth-api-admin"]["serviceAccountsEnabled"] is True
    router = (ROOT / "control-plane/api/app/routers/ad_sync.py").read_text()
    assert "client_credentials" in router and "KEYCLOAK_ADMIN_PASSWORD" not in router


def test_issuer_is_required_in_production():
    assert _env(_prod()["services"]["api"])["KEYCLOAK_ISSUER"].startswith("${KEYCLOAK_ISSUER:?")


# ── 14. No committed AI endpoint or key ───────────────────────────────
def test_ai_endpoint_and_key_have_no_defaults():
    ai = _env(_prod()["services"]["ai-orchestrator"])
    assert ai["OPENAI_BASE_URL"].startswith("${OPENAI_BASE_URL:?")
    assert ai["OPENAI_API_KEY"].startswith("${OPENAI_API_KEY?")
    dev = _env(yaml.safe_load((DOCKER / "compose.dev.yml").read_text())["services"]["ai-orchestrator"])
    assert dev["OPENAI_API_KEY"] == "${OPENAI_API_KEY:-}"
    gv = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert gv["tn_ai_base_url"] == ""


# ── 5, 11. Secrets: never empty, never short, never silently drifted ──
def test_secret_generation_does_not_trust_an_empty_file():
    tasks = yaml.safe_load((INSTALL / "roles/tn_config/tasks/secrets.yml").read_text())
    persist = tasks[0]
    assert "creates" not in persist["ansible.builtin.shell"], "creates: skipped zero-byte secrets"
    assert '[ -s "$f" ]' in persist["ansible.builtin.shell"]["cmd"]
    text = (INSTALL / "roles/tn_config/tasks/secrets.yml").read_text()
    assert "tn_secret_min_length" in text and "DRIFT" in text


def test_first_start_secrets_are_marked_and_rotatable():
    secrets = yaml.safe_load((INSTALL / "roles/tn_config/defaults/main.yml").read_text())["tn_generated_secrets"]
    first = {s["name"] for s in secrets if s.get("first_start")}
    assert {"postgres_password", "keycloak_db_password", "lrs_db_password", "keycloak_admin_password",
            "opensearch_admin_password", "opensearch_dashboards_password"} <= first
    play = yaml.safe_load((INSTALL / "playbooks/rotate-secret.yml").read_text())[0]
    assert first <= set(play["vars"]["tn_rotatable"])


# ── 2. Ownership changes run as root ──────────────────────────────────
def test_tasks_that_hand_files_to_other_uids_become_root():
    for path in ("roles/tn_tls/tasks/opensearch.yml", "roles/tn_config/tasks/main.yml"):
        for task in _flatten(yaml.safe_load((INSTALL / path).read_text())):
            module = next((v for k, v in task.items() if k.startswith("ansible.builtin.")
                           or k.startswith("community.")), None)
            if isinstance(module, dict) and str(module.get("owner", "")) in {"0", "1000", "65534"}:
                assert task.get("become") or task.get("_parent_become"), f"{path}: {task.get('name')}"


def _flatten(tasks, parent_become=False):
    for t in tasks or []:
        become = bool(t.get("become")) or parent_become
        if "block" in t:
            yield from _flatten(t["block"], become)
        else:
            yield {**t, "_parent_become": become}


# ── 8. Signed releases, verified before trust ─────────────────────────
def test_release_signs_and_the_installer_verifies():
    wf = (ROOT / ".github/workflows/release.yml").read_text()
    for needle in ("id-token: write", "cosign sign --yes", "cosign attest", "sign-blob",
                   "release-manifest.json.sigstore.json", "merge-base --is-ancestor", "provenance: mode=max"):
        assert needle in wf, needle
    release = yaml.safe_load((INSTALL / "roles/tn_release/defaults/main.yml").read_text())
    assert release["tn_release_verify_signature"] is True
    assert "release.yml@refs/tags/" in release["tn_release_cert_identity"]
    pinned = re.search(r'COSIGN_SHA256: "([0-9a-f]{64})"', wf).group(1)
    assert release["tn_cosign_sha256"]["linux-amd64"] == pinned
    assert re.search(r'COSIGN_VERSION: "([^"]+)"', wf).group(1) == release["tn_cosign_version"]


# ── 4. Backups: escrow required, secrets escrowed, restorable ─────────
def test_install_requires_a_backup_escrow():
    config = (INSTALL / "roles/tn_config/tasks/main.yml").read_text()
    assert "a backup escrow is configured" in config and "SECRETS_DIR=" in config
    backup = (ROOT / "scripts/backup/backup.sh").read_text()
    assert "secrets.tar.enc" in backup and "curl -sfk" not in backup
    assert (ROOT / "scripts/backup/restore-secrets.sh").is_file()
