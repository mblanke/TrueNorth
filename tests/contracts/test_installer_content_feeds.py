"""The content feeds' settings reach the api and the workers, and are off on a fresh
install (ADR 0014 decision 25, issue #287)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "install/inventory/group_vars/all/main.yml"
ENV = ROOT / "install/roles/tn_config/templates/env.production.j2"
COMPOSE = ROOT / "infra/platform/docker/compose.prod.yml"
EXAMPLE = ROOT / "infra/platform/docker/.env.production.example"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def _services() -> dict:
    return yaml.load(COMPOSE.read_text(), Loader=_Loader)["services"]  # noqa: S506


def test_feeds_are_off_on_a_fresh_install():
    defaults = yaml.safe_load(MAIN.read_text())
    assert defaults["tn_feeds_mode"] == "off"
    assert defaults["tn_feeds_http_proxy"] == "" and defaults["tn_feeds_bundle_public_key"] == ""
    assert defaults["tn_feeds_nvd_min_cvss"] == 9.0
    assert "vault_feeds_nvd_api_key" in defaults["tn_feeds_nvd_api_key"]
    assert re.search(r"^TN_FEEDS_MODE=off$", EXAMPLE.read_text(), re.M)


def test_the_env_file_renders_every_feeds_setting():
    env = ENV.read_text()
    for var, setting in (("TN_FEEDS_MODE", "tn_feeds_mode"), ("TN_FEEDS_HTTP_PROXY", "tn_feeds_http_proxy"),
                         ("TN_FEEDS_BUNDLE_PUBLIC_KEY", "tn_feeds_bundle_public_key"),
                         ("TN_FEEDS_NVD_API_KEY", "tn_feeds_nvd_api_key"),
                         ("TN_FEEDS_NVD_MIN_CVSS", "tn_feeds_nvd_min_cvss")):
        assert re.search(rf"^{var}='?{{{{ {setting} \|", env, re.M), var
    assert "TN_FEEDS_NVD_API_KEY='{{" in env  # single-quoted: never interpolated


def test_the_api_gets_the_mode_and_key_and_the_workers_the_pull_settings():
    s = _services()
    api = s["api"]["environment"]
    assert api["TN_FEEDS_MODE"] == "${TN_FEEDS_MODE:-off}"
    assert api["TN_FEEDS_BUNDLE_PUBLIC_KEY"] == "${TN_FEEDS_BUNDLE_PUBLIC_KEY:-}"
    assert "TN_FEEDS_NVD_API_KEY" not in api and "TN_FEEDS_HTTP_PROXY" not in api
    worker = s["worker-provision"]["environment"]
    for var in ("TN_FEEDS_MODE", "TN_FEEDS_HTTP_PROXY", "TN_FEEDS_NVD_API_KEY", "TN_FEEDS_NVD_MIN_CVSS"):
        assert var in worker, var
    assert worker["TN_FEEDS_MODE"] == "${TN_FEEDS_MODE:-off}"
    assert s["beat"]["environment"]["TN_FEEDS_MODE"] == "${TN_FEEDS_MODE:-off}"
    # the pull runs on worker-provision (queue destroy), the worker that can leave the host
    assert "tn-egress" in s["worker-provision"]["networks"]
    assert "destroy" in " ".join(s["worker-provision"]["command"].split())
