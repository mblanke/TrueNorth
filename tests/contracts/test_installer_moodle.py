"""The installer's optional Moodle (install/roles/tn_moodle, playbooks/85-moodle.yml).

Off by default; on, it runs TrueNorth's Moodle image by digest from the release, keeps its
credentials off command lines, backs it up, and wires it to TrueNorth without manual
steps. Static checks, no Docker: the bring-up against the real image (compose up, edge,
signed describe_course from the tn-backend network, moodledata backup and restore) is
recorded in the commit message.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
DOCKER = ROOT / "infra/platform/docker"
ROLE = INSTALL / "roles/tn_moodle"
DIGEST = re.compile(r"^[^\s@$]+@sha256:[0-9a-f]{64}$")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


_ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, _s, node: loader.construct_mapping(node)
    if isinstance(node, yaml.MappingNode)
    else loader.construct_sequence(node)
    if isinstance(node, yaml.SequenceNode)
    else loader.construct_scalar(node),
)


def _compose(name: str) -> dict:
    return yaml.load((DOCKER / name).read_text(), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        if "name" in node:
            yield node
        for key in ("block", "rescue", "always"):
            if key in node:
                yield from _tasks(node[key])


MOODLE = _compose("compose.moodle-prod.yml")
PROD = _compose("compose.prod.yml")
TASKS = list(_tasks(_yaml(ROLE / "tasks/main.yml")))


def _task(name_start: str) -> dict:
    return next(t for t in TASKS if t["name"].startswith(name_start))


def _module(task: dict) -> dict | str:
    return next(v for k, v in task.items() if k.startswith(("ansible.builtin.", "community.")))


# ── Off by default; on only through 85-moodle ────────────────────────────────
def test_moodle_is_off_by_default():
    assert _yaml(INSTALL / "inventory/group_vars/all/main.yml")["tn_moodle_enabled"] is False


def test_site_runs_it_after_the_seed_and_before_the_smoke_test():
    imports = [p["import_playbook"] for p in _yaml(INSTALL / "site.yml")]
    i = imports.index("playbooks/85-moodle.yml")
    assert imports.index("playbooks/60-keycloak.yml") < imports.index("playbooks/80-seed.yml") < i
    assert i < imports.index("playbooks/95-smoke-test.yml")


def test_the_playbook_runs_the_role_only_when_enabled():
    [play] = _yaml(INSTALL / "playbooks/85-moodle.yml")
    [role] = [r for r in play["roles"] if r["role"] == "tn_moodle"]
    assert role["when"] == "tn_moodle_enabled | bool"
    assert play["pre_tasks"][0]["when"] == "not tn_moodle_enabled | bool"


def test_nothing_in_the_platform_stack_is_moodle():
    """The platform's compose file, env file and edge carry no Moodle service: the node is
    its own compose project, started only by the role."""
    assert not [s for s in PROD["services"] if "moodle" in s]
    env = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert "MOODLE" not in env
    assert "moodle" not in (ROOT / "infra/platform/nginx/conf.d/upstream.conf").read_text()


def test_backups_name_the_node_only_when_enabled():
    config = (INSTALL / "roles/tn_config/tasks/main.yml").read_text()
    block = re.search(r"\{% if tn_moodle_enabled \| default\(false\) \| bool %\}(.*?)\{% endif %\}", config, re.S)
    assert block, "backup.env must name the Moodle node only when tn_moodle_enabled"
    assert "MOODLE_COMPOSE_FILE={{ tn_moodle_compose_file }}" in block.group(1)
    assert "MOODLE_ENV_FILE={{ tn_moodle_env_file }}" in block.group(1)
    assert config.count("MOODLE_COMPOSE_FILE=") == 1


# ── Images by digest ─────────────────────────────────────────────────────────
def test_the_node_runs_released_images_by_digest():
    services = MOODLE["services"]
    assert set(services) == {"moodle", "moodle-edge"}
    assert services["moodle"]["image"].startswith("${TN_IMAGE_MOODLE:?")
    assert DIGEST.match(services["moodle-edge"]["image"])
    # The same nginx as the platform edge, so one bump moves both.
    assert services["moodle-edge"]["image"] == PROD["services"]["nginx"]["image"]
    assert not any("build" in s for s in services.values())


def test_the_moodle_image_base_is_pinned_by_digest():
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "infra/platform/moodle/Dockerfile").read_text(), re.M)
    assert froms and all(DIGEST.match(f) for f in froms), froms


def test_release_publishes_the_moodle_image_and_requires_it_in_the_manifest():
    wf = (ROOT / ".github/workflows/release.yml").read_text()
    matrix = yaml.safe_load(wf)["jobs"]["image"]["strategy"]["matrix"]["service"]
    assert {"name": "moodle", "context": "infra/platform/moodle"} in matrix
    services = re.search(r"services = (\[.*?\])", wf).group(1)
    assert '"moodle"' in services


def test_the_role_takes_the_image_from_the_manifest_by_digest():
    check = _module(_task("Moodle — the release publishes a Moodle image"))
    assert "tn_release_manifest.images.moodle is defined" in check["that"]
    assert any("@sha256:[0-9a-f]{64}" in cond for cond in check["that"])
    pick = _module(_task("Moodle — which image"))["tn_moodle_image_ref"]
    assert "tn_release_manifest.images.moodle.ref" in pick
    gate = _module(_task("Moodle — the image is present at the digest it names"))["cmd"]
    assert "RepoDigests" in gate


def test_the_platform_installer_does_not_require_a_moodle_image():
    """Releases before Moodle joined release.yml still install with Moodle off."""
    services = _yaml(INSTALL / "roles/tn_release/defaults/main.yml")["tn_release_services"]
    assert "moodle" not in services


# ── Secrets: persisted, escrowed, never on a command line ────────────────────
def test_moodle_credentials_never_reach_a_command_line():
    for task in TASKS:
        module = _module(task)
        argv = module if isinstance(module, str) else " ".join(
            str(module.get(k, "")) for k in ("cmd", "argv", "_raw_params")
        )
        assert "tn_moodle_creds" not in argv, task["name"]
        assert "moodle_db_password" not in argv and "moodle_admin_password" not in argv, task["name"]
    for name in ("Moodle — database and role", "Moodle — render its env file", "Moodle — the credential map"):
        assert _task(name).get("no_log") is True, name
    db = _module(_task("Moodle — database and role"))
    assert db["stdin"] == "{{ lookup('ansible.builtin.template', 'moodle-db.sql.j2') }}"
    sql = (ROLE / "templates/moodle-db.sql.j2").read_text()
    assert "PASSWORD '{{ tn_moodle_creds['moodle_db_password'] }}'" in sql
    assert "PASSWORD" not in db["cmd"]


def test_credentials_are_persisted_where_backups_escrow_them():
    persist = _task("Moodle — persist its credentials")
    assert "{{ tn_config_dir }}/secrets/{{ item.name }}" in _module(persist)["cmd"]
    assert _module(persist)["stdin"] == "{{ item.value | default('') }}"
    names = {s["name"] for s in _yaml(ROLE / "defaults/main.yml")["tn_moodle_secrets"]}
    assert names == {"moodle_db_password", "moodle_admin_password"}
    # backup.env escrows SECRETS_DIR={{ tn_config_dir }}/secrets.
    assert "SECRETS_DIR={{ tn_config_dir }}/secrets" in (INSTALL / "roles/tn_config/tasks/main.yml").read_text()


def test_the_env_file_is_private():
    env = _task("Moodle — render its env file")
    assert _module(env)["mode"] == "0600"
    assert _module(env)["dest"] == "{{ tn_moodle_env_file }}"


# ── Runtime: hardened, internal, its own origin ──────────────────────────────
def test_both_containers_drop_every_capability():
    for name, svc in MOODLE["services"].items():
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
    assert "cap_add" not in MOODLE["services"]["moodle"]
    assert MOODLE["services"]["moodle-edge"]["read_only"] is True
    assert "NET_BIND_SERVICE" not in MOODLE["services"]["moodle-edge"]["cap_add"]


def test_moodle_joins_the_internal_platform_network_and_publishes_nothing():
    moodle = MOODLE["services"]["moodle"]
    assert "ports" not in moodle
    assert moodle["networks"]["tn-backend"]["aliases"] == ["moodle-${MOODLE_NODE}"]
    assert MOODLE["networks"]["tn-backend"] == {"external": True, "name": "tn-backend"}
    assert PROD["networks"]["tn-backend"]["internal"] is True
    assert MOODLE["services"]["moodle-edge"]["ports"] == ["${MOODLE_PUBLIC_PORT}:8443"]
    assert MOODLE["services"]["moodle-edge"]["networks"] == ["edge"]


def test_tls_ends_at_the_edge_and_moodle_knows_it():
    env = MOODLE["services"]["moodle"]["environment"]
    assert env["SSLPROXY"] == "true" and env["MOODLE_PUBLIC_PORT"].startswith("${MOODLE_PUBLIC_PORT")
    assert env["DB_HOST"] == "postgres"
    # Nothing leaves the box by default (the image's own default relay is smtp.gmail.com).
    assert "gmail" not in str(env) and env["SMTP_HOST"] == "${MOODLE_SMTP_HOST:-localhost}"
    conf = (ROOT / "infra/platform/nginx/moodle-edge/moodle.conf").read_text()
    assert "listen 8443 ssl;" in conf and "proxy_set_header X-Forwarded-Proto https;" in conf
    assert "proxy_set_header Host              $http_host;" in conf
    assert "include /etc/nginx/snippets/security-headers-upstream.conf;" in conf
    mounts = MOODLE["services"]["moodle-edge"]["volumes"]
    assert "../nginx/conf.d/ssl-params.conf:/etc/nginx/tn/ssl-params.conf:ro" in mounts


def test_the_site_url_is_the_platform_host_on_its_own_port():
    defaults = _yaml(ROLE / "defaults/main.yml")
    assert defaults["tn_moodle_site_url"] == "https://{{ tn_domain_fqdn }}:{{ tn_moodle_port }}"
    assert _yaml(INSTALL / "inventory/group_vars/all/main.yml")["tn_moodle_port"] == 8443


# ── Wired to TrueNorth without manual steps, and proven ──────────────────────
def test_the_role_registers_the_node_and_checks_it_end_to_end():
    register = _module(_task("Moodle — register it as the tenant's platform"))
    assert "install_cli register" in register["cmd"]
    assert "http://moodle-{{ tn_moodle_node }}:8080" in register["cmd"]
    assert register["stdin"] == "{{ tn_moodle_registration.stdout }}"
    assert "install_cli check" in _module(_task("Moodle — TrueNorth can publish to it"))["cmd"]
    tenant = _module(_task("Moodle — the TrueNorth tenant it serves"))
    assert "install_cli tenant {{ tn_tenant_slug }}" in tenant["cmd"]
    assert "TN_TENANT_ID={{ tn_moodle_tenant_id }}" in (ROLE / "templates/moodle.env.j2").read_text()
    assert (ROOT / "control-plane/api/app/moodle_backends/install_cli.py").is_file()


def test_the_smoke_checks_the_login_page_and_the_handoff():
    login = _module(_task("Moodle — the login page answers through the edge"))
    assert login["url"] == "{{ tn_moodle_site_url }}/login/index.php?loginredirect=0"
    handoff = _task("Moodle — its sign-in sends people to TrueNorth")
    assert _module(handoff)["follow_redirects"] == "none"
    assert "tn_moodle_login_url" in handoff["failed_when"]


def test_registration_is_idempotent_in_its_report():
    task = _task("Moodle — register it as the tenant's platform")
    assert task["changed_when"] == "(tn_moodle_register.stdout | from_json).action != 'unchanged'"
