"""Production runtime rules for compose.prod.yml, the worker and the installer (WP-C).

Static checks, no Docker: the bring-up that proved them is recorded in the commit
messages. What each guards:

* Redis is the Celery broker and must never evict (allkeys-lru dropped queued tasks).
* Exactly one Celery beat sends the periodic tasks (before, nothing ran them).
* Every task has a time limit below the broker's visibility timeout, and each worker's
  stop_grace_period outlasts the longest hard limit of its queues.
* Containers: read-only root, no capabilities, no privilege escalation, except where
  a service's block says why.
* Images: the TrueNorth ones by ${TN_IMAGE_*} (release-manifest.json digests), the rest
  pinned by digest; nothing built from compose.prod.yml.
* Fail-fast settings: PROVISIONER_BACKEND required, vCenter TLS verified by default,
  JSON logs.
* Alerts are routed (Alertmanager), and the default route is visibly a null receiver.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from worker.celery_app import JsonLogFormatter
from worker.celery_app import app as worker_app
from worker.contracts import TASKS
from worker.fencing import TASK_TIME_LIMIT

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"
WORKER = ROOT / "control-plane/worker"
DIGEST = re.compile(r"^[^\s@$]+@sha256:[0-9a-f]{64}$")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _compose(name: str = "compose.prod.yml") -> dict:
    return yaml.load((DOCKER / name).read_text(), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


PROD = _compose()
SERVICES: dict = PROD["services"]


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _command(service: dict) -> str:
    cmd = service.get("command") or ""
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


def _seconds(value: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)([hms])", str(value)):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


# ── 1. Redis: the broker never evicts ─────────────────────────────────
@pytest.mark.parametrize("compose", ["compose.prod.yml", "compose.dev.yml"])
def test_redis_broker_never_evicts(compose):
    cmd = _command(_compose(compose)["services"]["redis"])
    assert "--maxmemory-policy noeviction" in cmd, f"{compose}: redis must run noeviction (it is the Celery broker)"
    assert "allkeys" not in cmd and "volatile" not in cmd


# ── 2. One beat ────────────────────────────────────────────────────────
def test_exactly_one_beat_sends_the_periodic_tasks():
    beats = [name for name, svc in SERVICES.items() if re.search(r"\bcelery\b.*\bbeat\b", _command(svc))]
    assert beats == ["beat"], f"exactly one beat service, found {beats}"
    beat = SERVICES["beat"]
    assert beat["image"] == SERVICES["worker-scenario"]["image"], "beat runs the worker image (same app, same schedule)"
    assert beat.get("deploy", {}).get("replicas", 1) == 1
    assert beat["environment"] == SERVICES["worker-scenario"]["environment"]
    assert "--schedule=/tmp/" in _command(beat), "the schedule file lives on the tmpfs (read-only root)"
    assert "/tmp" in " ".join(beat.get("tmpfs") or [])


def test_beat_schedule_names_contracted_tasks_that_expire():
    schedule = worker_app.conf.beat_schedule
    assert {e["task"] for e in schedule.values()} == {
        "worker.tasks.health_check_ranges",
        "worker.tasks.collect_range_metrics",
    }
    contracted = {c.qualified_name for c in TASKS.values()}
    for name, entry in schedule.items():
        assert entry["task"] in contracted, name
        # A run nobody picked up is dropped before the next is due: no backlog of stale
        # checks while the workers are down (noeviction would keep them all).
        assert 0 < entry["options"]["expires"] <= entry["schedule"], name


# ── 3. Time limits and grace periods ──────────────────────────────────
def _celery_conf(**env) -> subprocess.CompletedProcess:
    code = (
        "import json; from worker.celery_app import app, VISIBILITY_TIMEOUT as V; c = app.conf; "
        "print(json.dumps([c.task_soft_time_limit, c.task_time_limit, V, "
        "c.broker_transport_options['visibility_timeout'], c.task_annotations]))"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=WORKER, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(WORKER), **env},
    )


def test_every_task_has_limits_below_the_visibility_timeout():
    r = _celery_conf()
    assert r.returncode == 0, r.stderr[-1500:]
    soft, hard, visibility, transport, annotations = json.loads(r.stdout.strip().splitlines()[-1])
    assert 0 < soft < hard < visibility == transport
    # The range tasks keep their own limits (worker/fencing.py), still below visibility.
    for task in ("provision_range", "destroy_range", "stop_range", "start_range", "snapshot_range",
                 "restore_snapshot", "deploy_noise_agents", "reconcile_lab_vms"):
        a = annotations[f"worker.tasks.{task}"]
        assert 0 < a["soft_time_limit"] < a["time_limit"] < visibility, task


def test_limits_come_from_the_environment_and_bad_ones_refuse_to_start():
    r = _celery_conf(CELERY_TASK_SOFT_TIME_LIMIT="600", CELERY_TASK_TIME_LIMIT="700")
    assert r.returncode == 0, r.stderr[-1500:]
    assert json.loads(r.stdout.strip().splitlines()[-1])[:2] == [600, 700]
    for soft, hard in (("700", "600"), ("600", "3600"), ("0", "100")):
        r = _celery_conf(CELERY_TASK_SOFT_TIME_LIMIT=soft, CELERY_TASK_TIME_LIMIT=hard)
        assert r.returncode != 0 and "visibility timeout" in r.stderr, (soft, hard)


def test_compose_passes_the_limits_and_workers_outlast_them():
    env = _env(SERVICES["worker-scenario"])
    soft = int(re.search(r":-(\d+)\}", env["CELERY_TASK_SOFT_TIME_LIMIT"]).group(1))
    hard = int(re.search(r":-(\d+)\}", env["CELERY_TASK_TIME_LIMIT"]).group(1))
    assert soft < hard
    # worker-provision runs provision/destroy (range tasks); the others the default limits.
    expected = {"worker-provision": TASK_TIME_LIMIT, "worker-scenario": hard, "worker-telemetry": hard}
    for name, longest in expected.items():
        grace = _seconds(SERVICES[name].get("stop_grace_period", "10s"))
        assert grace >= longest + 30, f"{name}: stop_grace_period {grace}s < hard limit {longest}s + headroom"


# ── 4. Container hardening ────────────────────────────────────────────
# Services allowed a writable root filesystem, and why (each block in compose says so).
WRITABLE_ROOT = {
    "keycloak": "`start` runs the Quarkus build into /opt/keycloak",
    "opensearch": "writes config/opensearch.keystore at every start",
}
# Capabilities a service may add back, beyond none.
CAP_ADD_ALLOWED = {
    "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"},
    "redis": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"},
    "minio": {"DAC_OVERRIDE", "FOWNER"},
    "nginx": {"NET_BIND_SERVICE", "CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"},
}
PRIVILEGED = {"cadvisor"}  # reads every container's cgroups; privileged grants all caps anyway


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_is_hardened(name):
    svc = SERVICES[name]
    assert "no-new-privileges:true" in (svc.get("security_opt") or []), f"{name}: no-new-privileges"
    if name in WRITABLE_ROOT:
        assert svc.get("read_only") is False, name
    else:
        assert svc.get("read_only") is True, f"{name}: read_only root filesystem"
    if name in PRIVILEGED:
        assert svc.get("privileged") is True
        return
    assert not svc.get("privileged"), name
    assert svc.get("cap_drop") == ["ALL"], f"{name}: cap_drop [ALL]"
    extra = set(svc.get("cap_add") or [])
    assert extra <= CAP_ADD_ALLOWED.get(name, set()), f"{name}: cap_add {sorted(extra)} not allowed"


def test_the_exceptions_are_still_needed():
    """An exception whose service no longer exists is stale; drop it."""
    assert set(WRITABLE_ROOT) | set(CAP_ADD_ALLOWED) | PRIVILEGED <= set(SERVICES)


# ── 5. Images: by digest, nothing built ───────────────────────────────
TN_IMAGES = {
    "api": "TN_IMAGE_API",
    "worker-provision": "TN_IMAGE_WORKER",
    "worker-scenario": "TN_IMAGE_WORKER",
    "worker-telemetry": "TN_IMAGE_WORKER",
    "beat": "TN_IMAGE_WORKER",
    "flower": "TN_IMAGE_WORKER",
    "ai-orchestrator": "TN_IMAGE_AI_ORCHESTRATOR",
    "web": "TN_IMAGE_WEB",
}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_image_is_pinned(name):
    svc = SERVICES[name]
    assert "build" not in svc, f"{name}: compose.prod.yml builds nothing (compose.build.yml does)"
    image = svc["image"]
    if name in TN_IMAGES:
        # Required, no default: a missing ref fails `config`, never silently pulls a tag.
        assert re.fullmatch(r"\$\{" + TN_IMAGES[name] + r":\?[^}]+\}", image), f"{name}: {image}"
    else:
        assert DIGEST.match(image), f"{name}: third-party image must be pinned by digest: {image}"


def test_compose_build_builds_exactly_the_application_images():
    build = _compose("compose.build.yml")["services"]
    assert set(build) == set(TN_IMAGES)
    for name, svc in build.items():
        assert set(svc) == {"build"}, f"compose.build.yml:{name} only adds build:"


def test_release_publishes_every_image_compose_runs():
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    published = {s["name"] for s in release["jobs"]["image"]["strategy"]["matrix"]["service"]}
    services = yaml.safe_load((INSTALL / "roles/tn_release/defaults/main.yml").read_text())["tn_release_services"]
    assert set(services) <= published
    assert set(services.values()) == set(TN_IMAGES.values())


def test_the_installer_renders_every_image_var():
    tmpl = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert "{% for service, var in tn_release_services.items() %}" in tmpl
    assert "{{ var }}={{ tn_images[service] }}" in tmpl
    example = (DOCKER / ".env.production.example").read_text()
    for var in set(TN_IMAGES.values()):
        assert re.search(rf"^{var}=\S+@sha256:", example, re.M), var


def test_the_installer_pulls_and_checks_digests_never_builds_by_default():
    main = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert main["tn_image_source"] == "release"
    compose = (INSTALL / "roles/tn_compose/tasks/main.yml").read_text()
    assert "compose.build.yml" in compose and "tn_image_source == 'build'" in compose
    assert "every image is present at the digest compose names" in compose
    for f in ("roles/tn_migrate/tasks/main.yml", "roles/tn_keycloak/handlers/main.yml"):
        assert "pull: never" in (INSTALL / f).read_text(), f


def test_nginx_runs_the_stock_image_with_the_edge_config_mounted():
    vols = " ".join(SERVICES["nginx"]["volumes"])
    for src, dst in (("../nginx/nginx.conf", "/etc/nginx/nginx.conf:ro"),
                     ("../nginx/conf.d", "/etc/nginx/conf.d:ro"),
                     ("../nginx/snippets", "/etc/nginx/snippets:ro")):
        assert f"{src}:{dst}" in vols
        assert (DOCKER / src).exists(), src
    # infra/platform/nginx/Dockerfile copies exactly these three; keep them in step.
    copies = re.findall(r"^COPY\s+(\S+)", (ROOT / "infra/platform/nginx/Dockerfile").read_text(), re.M)
    assert {c.rstrip("/") for c in copies} == {"nginx.conf", "conf.d", "snippets"}


# ── 6. Fail-fast settings ─────────────────────────────────────────────
@pytest.mark.parametrize("name", ["api", "worker-provision", "worker-scenario", "worker-telemetry", "beat"])
def test_provisioner_backend_is_required(name):
    value = _env(SERVICES[name])["PROVISIONER_BACKEND"]
    assert re.fullmatch(r"\$\{PROVISIONER_BACKEND:\?[^}]+\}", value), f"{name}: no silent default ({value})"


def test_vcenter_tls_is_verified_unless_the_operator_says_otherwise():
    assert _env(SERVICES["worker-provision"])["VSPHERE_VERIFY_SSL"] == "${VSPHERE_VERIFY_SSL:-true}"
    tmpl = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert "VSPHERE_VERIFY_SSL={{ tn_vsphere_verify_ssl | lower }}" in tmpl


@pytest.mark.parametrize("name", ["api", "worker-provision", "worker-scenario", "worker-telemetry", "beat"])
def test_logs_are_json_lines(name):
    assert _env(SERVICES[name])["LOG_FORMAT"] == "${LOG_FORMAT:-json}"


def test_the_installer_writes_json_logs():
    assert "\nLOG_FORMAT=json\n" in (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()


def test_worker_json_log_lines():
    record = logging.LogRecord("worker.tasks", logging.INFO, __file__, 1, "built %s", ("r1",), None)
    record.task_id = "abc"
    line = json.loads(JsonLogFormatter().format(record))
    assert line["message"] == "built r1" and line["level"] == "INFO" and line["task_id"] == "abc"


# ── 7. Alerting ───────────────────────────────────────────────────────
ALERTMANAGER = DOCKER / "monitoring/alertmanager"


def test_prometheus_sends_alerts_to_alertmanager():
    prom = yaml.safe_load((DOCKER / "monitoring/prometheus/prometheus.yml").read_text())
    targets = [t for am in prom["alerting"]["alertmanagers"] for sc in am["static_configs"] for t in sc["targets"]]
    assert targets == ["alertmanager:9093"]
    am = SERVICES["alertmanager"]
    assert DIGEST.match(am["image"])
    assert set(SERVICES["prometheus"]["networks"]) & set(am["networks"]), "prometheus must reach alertmanager"
    assert "tn-egress" in am["networks"], "the webhook receiver is off-host"


def test_both_alertmanager_configs_route_the_same_way():
    default = yaml.safe_load((ALERTMANAGER / "alertmanager.yml").read_text())
    webhook = yaml.safe_load((ALERTMANAGER / "alertmanager.webhook.yml").read_text())

    def shape(cfg, receiver):
        route = yaml.safe_dump(cfg["route"]).replace(f"receiver: {receiver}", "receiver: X")
        return route, cfg["inhibit_rules"]

    assert shape(default, "'null'") == shape(webhook, "webhook")
    # The default is visibly a null receiver: no notifier at all.
    assert default["receivers"] == [{"name": "null"}]
    [hook] = webhook["receivers"][0]["webhook_configs"]
    assert hook["url_file"] == "/etc/alertmanager/secrets/webhook_url", "the URL stays out of the environment"
    assert (ALERTMANAGER / "secrets-dev/webhook_url").is_file()


def test_every_alert_rule_has_a_routed_severity():
    rules = yaml.safe_load((DOCKER / "monitoring/prometheus/alerts.yml").read_text())
    alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
    assert len(alerts) >= 19
    for rule in alerts:
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]


def test_the_installer_selects_the_webhook_config_only_with_a_url():
    tmpl = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert "ALERTMANAGER_CONFIG={{ 'alertmanager.webhook.yml' if vault_alertmanager_webhook_url" in tmpl
    tasks = (INSTALL / "roles/tn_config/tasks/main.yml").read_text()
    assert "ALERTS GO NOWHERE" in tasks
