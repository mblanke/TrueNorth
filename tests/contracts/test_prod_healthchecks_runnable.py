"""Every health check in compose.prod.yml can run inside its image.

The installer's stack-up waits for every container to be healthy, so a probe whose
command the image does not contain is "unhealthy" forever and stops the install. That
happened on staging (2026-10-09): redis_exporter is a static binary with no wget.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra/platform/docker/compose.prod.yml"

# Images that are a single static binary: no shell, no wget/curl, nothing to probe with.
NO_SHELL_IMAGES = ("oliver006/redis_exporter",)


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def test_shell_less_images_have_no_in_container_probe() -> None:
    services = yaml.load(COMPOSE.read_text(), Loader=_Loader)["services"]  # noqa: S506
    offenders = []
    for name, svc in services.items():
        image = str(svc.get("image", ""))
        if not image.startswith(NO_SHELL_IMAGES):
            continue
        check = svc.get("healthcheck") or {}
        if not check.get("disable") and check.get("test") not in (None, ["NONE"]):
            offenders.append(f"{name}: {check.get('test')}")
    assert not offenders, offenders


def test_single_node_opensearch_keeps_no_replicas() -> None:
    env = yaml.load(COMPOSE.read_text(), Loader=_Loader)["services"]["opensearch"]["environment"]  # noqa: S506
    assert "discovery.type=single-node" in env
    # Otherwise every plugin-created index is yellow on one node (staging, 2026-10-09).
    assert "cluster.default_number_of_replicas=0" in env
    assert "plugins.index_state_management.history.number_of_replicas=0" in env
