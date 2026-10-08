"""Every build of the worker image supplies what control-plane/worker/Dockerfile needs.

The Dockerfile copies ``scenario_engine`` from a named build context
(``COPY --from=scenario_engine ...``) and must keep /app on sys.path for the running
process: ``celery -A worker.celery_app`` puts the cwd on sys.path only while it imports the
app, so without ``ENV PYTHONPATH=/app`` every later lazy ``import scenario_engine`` failed
in the running worker. In CI's compose stack that recorded every inject as
"scenario-engine not importable" and silently left detection scoring off. Cheap checks,
no Docker needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "control-plane/worker"
DOCKERFILE = WORKER / "Dockerfile"
COMPOSE_FILES = sorted((ROOT / "infra/platform/docker").glob("compose*.yml"))
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"


def _stages(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.M | re.I)}


def required_contexts() -> set[str]:
    """Names used in ``COPY --from=<name>`` that are not build stages (so: named contexts)."""
    text = DOCKERFILE.read_text()
    froms = set(re.findall(r"^COPY\s+--from=(\S+)", text, re.M))
    return {f for f in froms if f not in _stages(text) and ":" not in f and "/" not in f}


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _worker_builds():
    """(compose file, service, build mapping) for every service built from control-plane/worker."""
    for f in COMPOSE_FILES:
        doc = yaml.load(f.read_text(), Loader=_ComposeLoader) or {}  # noqa: S506 — SafeLoader subclass
        for name, svc in (doc.get("services") or {}).items():
            build = (svc or {}).get("build")
            if isinstance(build, str):
                build = {"context": build}
            if not isinstance(build, dict) or "context" not in build:
                continue
            if (f.parent / build["context"]).resolve() == WORKER.resolve():
                yield f, name, build


WORKER_BUILDS = list(_worker_builds())


def test_the_dockerfile_still_needs_the_engine_context():
    assert required_contexts() == {"scenario_engine"}


def test_the_dockerfile_keeps_app_on_the_python_path():
    env = re.findall(r"^ENV\s+PYTHONPATH[= ](\S+)", DOCKERFILE.read_text(), re.M)
    assert env and "/app" in env[-1].split(":"), "worker Dockerfile must set ENV PYTHONPATH=/app"


def test_compose_builds_the_worker_somewhere():
    names = {(f.name, s) for f, s, _ in WORKER_BUILDS}
    assert ("compose.dev.yml", "worker-scenario") in names
    assert ("compose.prod.yml", "worker-scenario") in names


@pytest.mark.parametrize(("compose", "service", "build"), WORKER_BUILDS, ids=lambda v: getattr(v, "name", v))
def test_every_compose_worker_build_supplies_the_named_contexts(compose, service, build):
    extra = build.get("additional_contexts") or {}
    if isinstance(extra, list):  # "name=path" list form
        extra = dict(item.split("=", 1) for item in extra)
    for name in required_contexts():
        assert name in extra, f"{compose.name}:{service} build lacks additional_contexts.{name}"
        path = (compose.parent / extra[name]).resolve()
        assert (path / name / "__init__.py").is_file(), f"{compose.name}:{service} {name} -> {path} has no package"


def _matrix_worker(workflow: Path, job: str) -> dict:
    doc = yaml.safe_load(workflow.read_text())
    matrix = doc["jobs"][job]["strategy"]["matrix"]["service"]
    [worker] = [s for s in matrix if s.get("context") == "control-plane/worker"]
    return worker


def _given_contexts(worker: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in str(worker.get("build_contexts", "")).split(",") if "=" in item)


def test_ci_builds_the_worker_with_the_named_contexts():
    given = _given_contexts(_matrix_worker(CI, "build-docker"))
    assert required_contexts() <= set(given)


def test_release_builds_the_worker_with_the_named_contexts():
    """release.yml publishes the worker image; a missing context would fail the build at
    tag time, after CI was green."""
    given = _given_contexts(_matrix_worker(RELEASE, "image"))
    assert required_contexts() <= set(given)
    for name in required_contexts():
        assert (ROOT / given[name] / name / "__init__.py").is_file(), f"release.yml {name} -> {given[name]}"


def test_release_and_ci_build_the_same_images():
    """Every image CI builds and scans is the set release.yml publishes, from the same contexts."""

    def images(workflow: Path, job: str) -> dict[str, str]:
        doc = yaml.safe_load(workflow.read_text())
        return {s["name"]: s["context"] for s in doc["jobs"][job]["strategy"]["matrix"]["service"]}

    assert images(RELEASE, "image") == images(CI, "build-docker")
