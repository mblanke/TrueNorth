"""Contract: CI pulls Docker Hub images through mirror.gcr.io, not anonymous Docker Hub.

GitHub-hosted runners share egress addresses, so anonymous pulls from registry-1.docker.io
hit Docker Hub's rate limit ("toomanyrequests") and intermittent 500s; on PR #124 that
failed build-docker, integration, e2e, load-smoke, moodle, greyspace, helm-kind and the
backup drill. Every pull path is routed through Google's public Docker Hub cache, with no
credentials:

* the runner's Docker daemon: ``.github/actions/dockerhub-mirror`` (daemon.json
  ``registry-mirrors``) in every hosted job that runs docker, compose, buildx or kind,
  before the first such step;
* ``services:`` containers, which start before any step: named on mirror.gcr.io;
* BuildKit's docker-container builder, which ignores daemon.json: every
  ``docker/setup-buildx-action`` sets ``buildkitd-config-inline`` with the mirror;
* kind's node containerd (helm-kind.yml): ``config_path`` plus a docker.io hosts.toml.

A new job that runs docker without the mirror fails here. docs/release.md "Docker Hub
pulls in CI".
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
ACTION_DIR = ROOT / ".github/actions/dockerhub-mirror"
ACTION = "./.github/actions/dockerhub-mirror"
MIRROR_HOST = "mirror.gcr.io"
DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}

# A command that makes the Docker daemon (or kind) pull or start something. Paths such as
# infra/platform/docker/compose.yml do not match.
_DOCKER_CMD = re.compile(
    r"\bdocker\s+(?:run|pull|compose|build|buildx|create|load)\b|\bkind\s+(?:create|load)\b"
)
# Actions that drive the daemon or BuildKit themselves.
_DOCKER_ACTIONS = ("docker/setup-buildx-action", "docker/build-push-action", "helm/kind-action")
_SCRIPT = re.compile(r"[\w./-]+\.sh\b")


def _workflows() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text()) for p in sorted(WORKFLOWS.glob("*.y*ml"))}


def _jobs():
    for wf, doc in _workflows().items():
        for name, job in (doc.get("jobs") or {}).items():
            yield wf, name, job


def _hosted(job: dict) -> bool:
    runs_on = job.get("runs-on")
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    return "self-hosted" not in labels


def _env_text(env: dict | None) -> str:
    return "\n".join(str(v) for v in (env or {}).values())


def _step_runs_docker(step: dict, job_env: str) -> bool:
    uses = step.get("uses") or ""
    if uses.startswith(_DOCKER_ACTIONS):
        return True
    run = step.get("run")
    if not run:
        return False
    text = "\n".join([run, _env_text(step.get("env")), job_env])
    # Scripts the step runs (one level: itest.sh, drill.sh, smoke.sh, ...).
    for rel in _SCRIPT.findall(run):
        script = ROOT / rel.removeprefix("./")
        if script.is_file():
            text += "\n" + script.read_text()
    return bool(_DOCKER_CMD.search(text))


def _docker_steps(job: dict) -> list[int]:
    job_env = _env_text(job.get("env"))
    return [i for i, s in enumerate(job.get("steps") or []) if _step_runs_docker(s, job_env)]


def _mirror_steps(job: dict) -> list[int]:
    return [i for i, s in enumerate(job.get("steps") or []) if s.get("uses") == ACTION]


def _registry_host(image: str) -> str:
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


DOCKER_JOBS = sorted(
    (wf, name) for wf, name, job in _jobs() if _hosted(job) and _docker_steps(job) and not job.get("services")
)


def test_the_detector_finds_the_jobs_that_pull_from_docker_hub():
    """The scan is not vacuous: the jobs PR #124 lost to the rate limit are all found."""
    expected = {
        ("ci.yml", "build-docker"),
        ("ci.yml", "integration"),
        ("ci.yml", "e2e"),
        ("ci.yml", "load-smoke"),
        ("ci.yml", "moodle"),
        ("ci.yml", "greyspace"),
        ("ci.yml", "vsphere-sim"),
        ("helm-kind.yml", "helm-kind"),
        ("backup-drill.yml", "drill"),
        ("release.yml", "image"),
    }
    assert expected <= set(DOCKER_JOBS)


@pytest.mark.parametrize(("workflow", "job_name"), DOCKER_JOBS, ids=[f"{w}:{j}" for w, j in DOCKER_JOBS])
def test_docker_jobs_configure_the_mirror_before_their_first_pull(workflow, job_name):
    job = _workflows()[workflow]["jobs"][job_name]
    steps = job["steps"]
    mirror = _mirror_steps(job)
    assert mirror, f"{workflow} {job_name} runs docker without `uses: {ACTION}`"
    assert mirror[0] < _docker_steps(job)[0], f"{workflow} {job_name}: the mirror step must come first"
    # A local action exists only after checkout.
    checkout = [i for i, s in enumerate(steps) if (s.get("uses") or "").startswith("actions/checkout@")]
    assert checkout and checkout[0] < mirror[0], f"{workflow} {job_name}: checkout before {ACTION}"


def test_service_containers_are_named_on_the_mirror():
    """`services:` start before the first step, so the daemon mirror cannot reach them."""
    seen = 0
    for wf, name, job in _jobs():
        for svc, spec in (job.get("services") or {}).items():
            image = spec["image"] if isinstance(spec, dict) else spec
            host = _registry_host(image)
            assert host not in DOCKER_HUB_HOSTS, f"{wf} {name} service {svc}: {image} pulls from Docker Hub"
            seen += 1
        # Its dockerd restart would stop the service containers.
        if job.get("services"):
            assert not _mirror_steps(job), f"{wf} {name}: {ACTION} restarts dockerd under its services"
    assert seen, "no service containers found; update this test"


def test_self_hosted_jobs_do_not_restart_the_hosts_docker():
    for wf, name, job in _jobs():
        if not _hosted(job):
            assert not _mirror_steps(job), f"{wf} {name}: a self-hosted runner's dockerd is shared"


def test_every_buildkit_builder_uses_the_mirror():
    found = 0
    for wf, name, job in _jobs():
        for step in job.get("steps") or []:
            if not (step.get("uses") or "").startswith("docker/setup-buildx-action@"):
                continue
            found += 1
            inline = (step.get("with") or {}).get("buildkitd-config-inline")
            assert inline, f"{wf} {name}: setup-buildx-action without buildkitd-config-inline"
            config = tomllib.loads(inline)
            assert config["registry"]["docker.io"]["mirrors"] == [MIRROR_HOST], f"{wf} {name}"
    assert found >= 3, "ci.yml build-docker, helm-kind and release.yml image build with buildx"


def test_the_action_merges_daemon_json_restarts_and_verifies():
    action = yaml.safe_load((ACTION_DIR / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["mirror"]["default"] == f"https://{MIRROR_HOST}"
    run = "\n".join(s.get("run", "") for s in action["runs"]["steps"])
    assert "/etc/docker/daemon.json" in run
    assert '"registry-mirrors"' in run and "jq" in run  # merged into the runner's own file
    assert "systemctl restart docker" in run
    assert ".RegistryConfig.Mirrors" in run and "exit 1" in run  # fails when not applied
    # dockerd sends a docker.io login to the mirror, which rejects it, and falls back to
    # Docker Hub; the runner image ships one (account githubactions).
    assert "docker logout" in run


def test_kind_nodes_pull_docker_hub_through_the_mirror():
    steps = _workflows()["helm-kind.yml"]["jobs"]["helm-kind"]["steps"]
    [kind_at] = [i for i, s in enumerate(steps) if (s.get("uses") or "").startswith("helm/kind-action@")]
    config_path = steps[kind_at]["with"]["config"]
    config = yaml.safe_load((ROOT / config_path).read_text())
    patches = "\n".join(config["containerdConfigPatches"])
    assert tomllib.loads(patches)["plugins"]["io.containerd.grpc.v1.cri"]["registry"]["config_path"] == (
        "/etc/containerd/certs.d"
    )
    hosts = [i for i, s in enumerate(steps) if "certs.d/docker.io/hosts.toml" in (s.get("run") or "")]
    smoke = [i for i, s in enumerate(steps) if "infra/k8s/kind/smoke.sh" in (s.get("run") or "")]
    assert hosts and smoke and kind_at < hosts[0] < smoke[0]
    run = steps[hosts[0]]["run"]
    assert f'[host."https://{MIRROR_HOST}"]' in run and 'server = "https://registry-1.docker.io"' in run
