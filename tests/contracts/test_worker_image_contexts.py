"""Every build of the api and worker images supplies the named contexts their Dockerfiles need.

The worker Dockerfile copies ``scenario_engine`` from a named build context
(``COPY --from=scenario_engine ...``) and must keep /app on sys.path for the running
process: ``celery -A worker.celery_app`` puts the cwd on sys.path only while it imports the
app, so without ``ENV PYTHONPATH=/app`` every later lazy ``import scenario_engine`` failed
in the running worker. In CI's compose stack that recorded every inject as
"scenario-engine not importable" and silently left detection scoring off.

Both Dockerfiles also copy ``content/catalogue`` and ``content/mitre`` from a named
``content`` context, so the images carry what compose once mounted from the checkout (Helm
mounts no checkout). A build site without the context fails at build time, which for
release.yml means at tag time, after CI was green. Cheap checks, no Docker needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "control-plane/worker"
API = ROOT / "control-plane/api"
DOCKERFILE = WORKER / "Dockerfile"
IMAGES = {"api": API, "worker": WORKER}
COMPOSE_FILES = sorted((ROOT / "infra/platform/docker").glob("compose*.yml"))
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"
HELM_KIND = ROOT / ".github/workflows/helm-kind.yml"


def _stages(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.M | re.I)}


def context_copies(build_dir: Path = WORKER) -> list[tuple[str, str]]:
    """(context name, source path) of every ``COPY --from=<named context> <src> <dest>``."""
    text = (build_dir / "Dockerfile").read_text()
    stages = _stages(text)
    return [
        (name, src)
        for name, src in re.findall(r"^COPY\s+--from=(\S+)\s+(\S+)\s+\S+", text, re.M)
        if name not in stages and ":" not in name and "/" not in name
    ]


def required_contexts(build_dir: Path = WORKER) -> set[str]:
    """Names used in ``COPY --from=<name>`` that are not build stages (so: named contexts)."""
    return {name for name, _ in context_copies(build_dir)}


def assert_context_has_sources(build_dir: Path, name: str, path: Path, where: str) -> None:
    """``path`` (what a build site passes as context ``name``) holds every source copied from it."""
    for ctx, src in context_copies(build_dir):
        if ctx == name:
            assert (path / src).exists(), f"{where}: context {name} -> {path} has no {src}"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _builds(build_dir: Path):
    """(compose file, service, build mapping) for every service built from ``build_dir``."""
    for f in COMPOSE_FILES:
        doc = yaml.load(f.read_text(), Loader=_ComposeLoader) or {}  # noqa: S506 — SafeLoader subclass
        for name, svc in (doc.get("services") or {}).items():
            build = (svc or {}).get("build")
            if isinstance(build, str):
                build = {"context": build}
            if not isinstance(build, dict) or "context" not in build:
                continue
            if (f.parent / build["context"]).resolve() == build_dir.resolve():
                yield f, name, build


WORKER_BUILDS = list(_builds(WORKER))
IMAGE_BUILDS = [(image, *b) for image, build_dir in IMAGES.items() for b in _builds(build_dir)]


def test_the_dockerfiles_still_need_their_contexts():
    assert required_contexts(WORKER) == {"scenario_engine", "content"}
    assert required_contexts(API) == {"content"}
    for build_dir in IMAGES.values():
        copied = {src for name, src in context_copies(build_dir) if name == "content"}
        assert copied == {"catalogue", "mitre"}, build_dir


def test_the_dockerfile_keeps_app_on_the_python_path():
    env = re.findall(r"^ENV\s+PYTHONPATH[= ](\S+)", DOCKERFILE.read_text(), re.M)
    assert env and "/app" in env[-1].split(":"), "worker Dockerfile must set ENV PYTHONPATH=/app"


def test_compose_builds_the_worker_somewhere():
    names = {(f.name, s) for f, s, _ in WORKER_BUILDS}
    assert ("compose.dev.yml", "worker-scenario") in names
    # compose.prod.yml runs the released image by digest; compose.build.yml builds it.
    assert ("compose.build.yml", "worker-scenario") in names


@pytest.mark.parametrize(
    ("image", "compose", "service", "build"), IMAGE_BUILDS, ids=lambda v: getattr(v, "name", v)
)
def test_every_compose_build_supplies_the_named_contexts(image, compose, service, build):
    extra = build.get("additional_contexts") or {}
    if isinstance(extra, list):  # "name=path" list form
        extra = dict(item.split("=", 1) for item in extra)
    for name in required_contexts(IMAGES[image]):
        assert name in extra, f"{compose.name}:{service} build lacks additional_contexts.{name}"
        path = (compose.parent / extra[name]).resolve()
        assert_context_has_sources(IMAGES[image], name, path, f"{compose.name}:{service}")


def _matrix_service(workflow: Path, job: str, build_dir: Path) -> dict:
    doc = yaml.safe_load(workflow.read_text())
    matrix = doc["jobs"][job]["strategy"]["matrix"]["service"]
    [service] = [s for s in matrix if s.get("context") == str(build_dir.relative_to(ROOT))]
    return service


def _given_contexts(value: object) -> dict[str, str]:
    """build-push-action's ``build-contexts``: one name=path per line. It does not split on
    commas, so "a=x,b=y" is ONE context named a whose path is "x,b=y"."""
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    assert not any("," in line for line in lines), f"comma-separated build-contexts: {value!r}"
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.parametrize("image", sorted(IMAGES))
@pytest.mark.parametrize(("workflow", "job"), [(CI, "build-docker"), (RELEASE, "image")], ids=["ci", "release"])
def test_ci_and_release_build_with_the_named_contexts(workflow, job, image):
    """release.yml publishes these images; a missing context would fail the build at tag
    time, after CI was green."""
    build_dir = IMAGES[image]
    given = _given_contexts(_matrix_service(workflow, job, build_dir).get("build_contexts"))
    assert required_contexts(build_dir) <= set(given)
    for name in required_contexts(build_dir):
        assert_context_has_sources(build_dir, name, ROOT / given[name], f"{workflow.name} {image}")


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_helm_kind_builds_with_the_named_contexts(image):
    """helm-kind.yml builds the images itself (load: true, into kind)."""
    build_dir = IMAGES[image]
    steps = [s for job in yaml.safe_load(HELM_KIND.read_text())["jobs"].values() for s in job.get("steps", [])]
    [step] = [s for s in steps if (s.get("with") or {}).get("context") == str(build_dir.relative_to(ROOT))]
    given = _given_contexts(step["with"].get("build-contexts"))
    assert required_contexts(build_dir) <= set(given)
    for name in required_contexts(build_dir):
        assert_context_has_sources(build_dir, name, ROOT / given[name], f"helm-kind.yml {image}")


def test_the_makefile_builds_with_the_named_contexts():
    makefile = (ROOT / "Makefile").read_text()
    for image, build_dir in IMAGES.items():
        rel = str(build_dir.relative_to(ROOT))
        lines = [ln for ln in makefile.splitlines() if "docker build" in ln and f"{rel}/" in ln]
        assert lines, image
        for line in lines:
            for name in required_contexts(build_dir):
                assert f"--build-context {name}=" in line, f"Makefile: {line.strip()}"


def test_release_and_ci_build_the_same_images():
    """Every image CI builds and scans is the set release.yml publishes, from the same contexts."""

    def images(workflow: Path, job: str) -> dict[str, str]:
        doc = yaml.safe_load(workflow.read_text())
        return {s["name"]: s["context"] for s in doc["jobs"][job]["strategy"]["matrix"]["service"]}

    assert images(RELEASE, "image") == images(CI, "build-docker")
