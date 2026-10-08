"""The production settings the API and AI orchestrator insist on reach them.

TN_ENV=production (control-plane/api/app/settings.py, ai-orchestrator/app/main.py)
refuses to start without CSRF_SECRET / AI_SERVICE_TOKEN (>= 32 characters) and
KEYCLOAK_AUDIENCE. These hold compose.prod.yml, the installer's env file and secrets,
the Keycloak audience mapper that makes KEYCLOAK_AUDIENCE safe, and Prometheus' tokens.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def _services() -> dict:
    return yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]  # noqa: S506


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return dict(item.split("=", 1) for item in env) if isinstance(env, list) else env


def test_compose_passes_the_production_settings():
    s = _services()
    api = _env(s["api"])
    assert api["TN_ENV"] == "${TN_ENV:-production}"
    for key in ("TN_VERSION", "AI_SERVICE_TOKEN", "TRUSTED_PROXY_CIDRS", "METRICS_SCRAPE_TOKEN",
                "SCHEDULER_FEED_BASE_URL", "KEYCLOAK_AUDIENCE", "CSRF_SECRET"):
        assert key in api, key
    for name in ("worker-provision", "worker-scenario", "worker-telemetry", "ai-orchestrator"):
        env = _env(s[name])
        assert "AI_SERVICE_TOKEN" in env and env["TN_ENV"] == "${TN_ENV:-production}", name
    assert "/health/ready" in " ".join(s["api"]["healthcheck"]["test"])
    assert any(v.endswith(":/etc/prometheus/secrets:ro") for v in s["prometheus"]["volumes"])


def test_prometheus_sends_both_bearer_tokens():
    prom = yaml.safe_load((DOCKER / "monitoring/prometheus/prometheus.yml").read_text())
    jobs = {j["job_name"]: j for j in prom["scrape_configs"]}
    assert jobs["truenorth-api"]["authorization"]["credentials_file"].endswith("/metrics_scrape_token")
    assert jobs["ai-orchestrator"]["authorization"]["credentials_file"].endswith("/ai_service_token")
    for name in ("metrics_scrape_token", "ai_service_token"):
        assert (DOCKER / "monitoring/prometheus/secrets-dev" / name).is_file()


def test_installer_generates_and_renders_them():
    secrets = yaml.safe_load((INSTALL / "roles/tn_config/defaults/main.yml").read_text())["tn_generated_secrets"]
    assert {"csrf_secret", "secrets_key", "ai_service_token", "metrics_scrape_token"} <= {s["name"] for s in secrets}
    # 48 random bytes, cut to 32: never shorter than the production floor.
    assert "head -c 48 /dev/urandom" in (INSTALL / "roles/tn_config/tasks/secrets.yml").read_text()
    env = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    for line in ("TN_ENV=production", "TN_VERSION={{ tn_app_git_version }}",
                 "KEYCLOAK_AUDIENCE={{ tn_keycloak_api_client }}",
                 "AI_SERVICE_TOKEN={{ tn_secrets['ai_service_token'] }}",
                 "METRICS_SCRAPE_TOKEN={{ tn_secrets['metrics_scrape_token'] }}",
                 "TRUSTED_PROXY_CIDRS={{ tn_trusted_proxy_cidrs }}",
                 "SCHEDULER_FEED_BASE_URL=https://{{ tn_domain_fqdn }}",
                 "CSRF_SECRET={{ tn_secrets['csrf_secret'] }}"):
        assert line in env, line


def test_keycloak_puts_the_api_in_the_audience():
    mappers = yaml.safe_load((INSTALL / "roles/tn_keycloak/defaults/main.yml").read_text())["tn_kc_protocol_mappers"]
    aud = next(m for m in mappers if m["protocolMapper"] == "oidc-audience-mapper")
    assert aud["config"]["included.client.audience"] == "{{ tn_keycloak_api_client }}"
    assert aud["config"]["access.token.claim"] == "true"
    tasks = (INSTALL / "roles/tn_keycloak/tasks/main.yml").read_text()
    assert "add missing identity scope mappers" in tasks  # existing installs get it too
