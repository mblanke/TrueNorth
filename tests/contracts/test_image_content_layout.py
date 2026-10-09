"""The api and worker images carry the content they read, at the path their code looks in.

compose.prod.yml used to mount content/catalogue and content/mitre from the checkout into
/app/content; no image contained them, so under Helm (no checkout) GET /software-catalogue,
deploy-time software installs and ATT&CK id checks had nothing to read. The Dockerfiles now
COPY both directories from a named ``content`` context.

For each reader this executes the module as the image lays it out (``__file__`` under /app,
so ``parents[N]`` is short), maps the path it looks in (``_CONTAINER_COPY``) through the
Dockerfile's ``COPY --from=content <src> <dest>`` lines back to the repository file the
image will hold, and loads that file with the module's own loader. It also checks the
checkout fallback still resolves when /app/content does not exist. No Docker needed;
tests/contracts/test_worker_image_contexts.py checks every build site passes the context.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "control-plane/api"
WORKER = ROOT / "control-plane/worker"


def _content_copies(build_dir: Path) -> list[tuple[PurePosixPath, Path]]:
    """(destination in the image, source in the repository) for every COPY --from=content."""
    text = (build_dir / "Dockerfile").read_text()
    return [
        (PurePosixPath(dest), ROOT / "content" / src)
        for src, dest in re.findall(r"^COPY\s+--from=content\s+(\S+)\s+(\S+)\s*$", text, re.M)
    ]


def _repo_file_in_image(build_dir: Path, image_path: PurePosixPath) -> Path | None:
    """The repository file the image holds at ``image_path``, or None if no COPY puts one there."""
    for dest, src in _content_copies(build_dir):
        if image_path == dest or dest in image_path.parents:
            return src / image_path.relative_to(dest)
    return None


def _exec_at(source: Path, image_file: str, package: str, monkeypatch) -> types.ModuleType:
    name = f"{package}._image_layout_{source.stem}"
    mod = types.ModuleType(name)
    mod.__file__ = image_file
    mod.__package__ = package
    monkeypatch.setitem(sys.modules, name, mod)  # dataclasses look the module up
    exec(compile(source.read_text(), image_file, "exec"), mod.__dict__)  # noqa: S102 - our own module
    return mod


def _software_api(mod, path: Path) -> int:
    return len(mod._load(str(path), path.stat().st_mtime).software)


def _software_worker(mod, path: Path) -> int:
    return len(mod.load(path).entries)


def _attack(mod, path: Path, monkeypatch) -> int:
    monkeypatch.setenv("TN_ATTACK_CATALOGUE", str(path))
    mod.load.cache_clear()
    return len(mod.load().techniques)


READERS = [
    # (image build dir, module source, its path in the image, package, loader)
    (API, API / "app/routers/software_catalogue.py", "/app/app/routers/software_catalogue.py", "app.routers", "software_api"),
    (API, API / "app/attack_catalogue.py", "/app/app/attack_catalogue.py", "app", "attack"),
    (WORKER, WORKER / "worker/software_catalogue.py", "/app/worker/software_catalogue.py", "worker", "software_worker"),
    (WORKER, WORKER / "worker/attack_catalogue.py", "/app/worker/attack_catalogue.py", "worker", "attack"),
]
IDS = [f"{b.name}:{s.name}" for b, s, *_ in READERS]


def _load(kind: str, mod, path: Path, monkeypatch) -> int:
    if kind == "attack":
        return _attack(mod, path, monkeypatch)
    return {"software_api": _software_api, "software_worker": _software_worker}[kind](mod, path)


@pytest.mark.parametrize(("build_dir", "source", "image_file", "package", "kind"), READERS, ids=IDS)
def test_the_image_holds_the_file_each_reader_looks_for(build_dir, source, image_file, package, kind, monkeypatch):
    mod = _exec_at(source, image_file, package, monkeypatch)
    wanted = PurePosixPath(str(mod._CONTAINER_COPY))
    assert wanted.is_absolute() and wanted.parts[:3] == ("/", "app", "content"), wanted
    repo_file = _repo_file_in_image(build_dir, wanted)
    assert repo_file is not None, f"{build_dir.name}/Dockerfile copies nothing to {wanted}"
    assert repo_file.is_file(), f"{wanted} in the image would come from {repo_file}, which does not exist"
    assert _load(kind, mod, repo_file, monkeypatch) > 0, f"{repo_file} loads empty"


@pytest.mark.parametrize(("build_dir", "source", "image_file", "package", "kind"), READERS, ids=IDS)
def test_a_checkout_still_finds_the_repository_copy(build_dir, source, image_file, package, kind, monkeypatch):
    for var in ("TN_SOFTWARE_CATALOGUE", "TN_ATTACK_CATALOGUE"):
        monkeypatch.delenv(var, raising=False)
    mod = _exec_at(source, str(source), package, monkeypatch)
    if Path(str(mod._CONTAINER_COPY)).is_file():
        pytest.skip("running inside an image layout; the checkout fallback is not reachable here")
    path = mod.catalogue_path()
    assert path == mod._REPO_COPY and path.is_file(), path
    assert _load(kind, mod, path, monkeypatch) > 0


def test_prod_compose_no_longer_mounts_the_content_over_the_image():
    """One source per deployment: compose runs the image's copy, exactly as Helm does."""
    prod = (ROOT / "infra/platform/docker/compose.prod.yml").read_text()
    mounts = [ln for ln in prod.splitlines() if re.match(r"\s*-\s*\.\./\.\./\.\./content/", ln)]
    assert not mounts, mounts
