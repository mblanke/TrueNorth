# Author: Marc-Andre Blanke
# Created: 2026-11-23T22:39:00Z
"""TN-DEPOT01 mirrors for what the greyspace-host build cannot reach (2026-11-23).

DESIGNED, NOT YET PROVEN ON SITE: greyspace-host builds with no internet. Docker's apt
repository and Alpine through apt-cacher-ng (HTTPS backends, plain-http clients, still no
CONNECT; their own tn_depot_acng_build_remaps key); the pinned images by digest through Nexus
docker-proxy (Docker Hub) and a new quay-proxy, re-tagged to the stack's exact reference
(containerd image store); prefetch warms all of it. None of the Docker connectors is
reachable from a range. (The site-proven security/Parrot remaps are PR #530's.)
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml
from app.greyspace import config, npc

ROOT = Path(__file__).resolve().parents[2]
DEPOT = ROOT / "install/lab/roles/tn_depot"
PREFETCH = ROOT / "install/lab/roles/tn_prefetch"
DEFAULTS = yaml.safe_load((DEPOT / "defaults/main.yml").read_text())
PF_DEFAULTS = yaml.safe_load((PREFETCH / "defaults/main.yml").read_text())
KIT = yaml.safe_load((ROOT / "install/lab/work-site/inventory/group_vars/all/main.yml").read_text())
SCRIPT = ROOT / "infra/vsphere/packer/files/linux/roles/greyspace-host.sh"
ROLE_SH = SCRIPT.read_text()
DOCKERFILES = sorted((ROOT / "greyspace/images").glob("*/Dockerfile"))


def _env() -> jinja2.Environment:
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True
    )  # Ansible's template module default
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
    return env


def _vars(**extra) -> dict:
    v = {**DEFAULTS, **KIT, "ansible_managed": "Ansible managed", **extra}
    v["tn_depot_range_ports"] = ast.literal_eval(_env().from_string(DEFAULTS["tn_depot_range_ports"]).render(**v))
    return v


def _acng(**extra) -> list[str]:
    """The rendered acng.conf, defaults' own Jinja resolved (Ansible renders them lazily)."""
    v = _vars(**extra)
    conf = _env().from_string((DEPOT / "templates/acng.conf.j2").read_text()).render(**v)
    conf = _env().from_string(conf).render(**v)
    return [ln.strip() for ln in conf.splitlines() if ln.strip()]


def _directive(lines: list[str], key: str) -> str:
    vals = [ln.split(":", 1)[1].strip() for ln in lines if ln.startswith(key + ":")]
    assert len(vals) == 1, f"{key} must be set exactly once, got {vals}"
    return vals[0]


def test_every_remap_has_its_comment_on_its_own_line():
    lines = _acng()
    for ln in lines:
        if ln.startswith("Remap-"):
            assert "#" not in ln, ln  # never a trailing comment after the backends
    for name in ("tndocker", "tnalpine"):
        i = next(i for i, ln in enumerate(lines) if ln.startswith(f"Remap-{name}:"))
        assert lines[i - 1].startswith("# "), name


def test_https_upstreams_are_remapped_never_tunnelled():
    lines = _acng()
    assert "Remap-tndocker: download.docker.com/linux/ubuntu ; https://download.docker.com/linux/ubuntu" in lines
    assert "Remap-tnalpine: dl-cdn.alpinelinux.org/alpine ; https://dl-cdn.alpinelinux.org/alpine" in lines
    # Still not an open proxy and still no CONNECT.
    assert _directive(lines, "PassThroughPattern") == "^$"
    assert _directive(lines, "ForceManaged") == "1"
    assert _directive(lines, "AllowUserPorts") == "80"


def test_the_docker_key_and_alpine_files_are_cacheable_file_types():
    lines = _acng()
    volatile = re.compile(_directive(lines, "VfilePatternEx"))
    permanent = re.compile(_directive(lines, "PfilePatternEx"))
    assert volatile.search("download.docker.com/linux/ubuntu/gpg")
    assert volatile.search("alpine/v3.20/main/x86_64/APKINDEX.tar.gz")
    assert volatile.search("pub/rocky/9/BaseOS/x86_64/os/repodata/repomd.xml")
    assert permanent.search("alpine/v3.20/main/x86_64/unbound-1.20.0-r0.apk")
    assert permanent.search("pub/rocky/9/BaseOS/x86_64/os/Packages/b/bash-5.1.8-9.el9.x86_64.rpm")
    assert not volatile.search("download.docker.com/linux/static/stable/x86_64/docker-27.0.0.tgz")


def test_no_docker_connector_is_reachable_from_a_range():
    docker_ports = {
        DEFAULTS["tn_depot_nexus_docker_proxy_port"],
        DEFAULTS["tn_depot_nexus_docker_hosted_port"],
        DEFAULTS["tn_depot_nexus_quay_proxy_port"],
    }
    assert docker_ports == {8082, 8083, 8084}
    v = _vars(tn_depot_upstream_online=False)
    assert v["tn_depot_range_ports"] == [8081, 3142]
    assert not docker_ports & set(v["tn_depot_range_ports"])
    nft = _env().from_string((DEPOT / "templates/depot-range-fw.nft.j2").read_text()).render(**v)
    for port in docker_ports:
        assert str(port) not in nft


def test_quay_proxy_is_a_blocked_by_default_proxy_on_its_own_connector():
    repos = {r["name"]: r for r in DEFAULTS["tn_depot_nexus_repos"]}
    quay = repos["quay-proxy"]
    assert (quay["format"], quay["type"], quay["proxy"]) == ("docker", "proxy", True)  # upstream.yml blocks it
    assert quay["body"]["proxy"]["remoteUrl"] == "{{ tn_depot_quay_upstream }}"
    assert DEFAULTS["tn_depot_quay_upstream"] == "https://quay.io"
    assert quay["body"]["docker"]["httpPort"] == "{{ tn_depot_nexus_quay_proxy_port }}"
    assert repos["docker-proxy"]["proxy"] is True


def test_anonymous_gets_no_docker_and_build_read_only_reads_the_proxies():
    assert not any("docker" in p for p in DEFAULTS["tn_depot_anonymous_privileges"])
    docker = [p for p in DEFAULTS["tn_depot_build_read_privileges"] if "docker" in p]
    assert docker == ["nx-repository-view-docker-docker-proxy-read", "nx-repository-view-docker-quay-proxy-read"]
    pf = DEFAULTS["tn_depot_prefetch_privileges"]
    assert "nx-repository-view-docker-docker-proxy-read" in pf and "nx-repository-view-docker-quay-proxy-read" in pf


# ── the image lists, one truth ─────────────────────────────────────────
def _script_pinned() -> list[str]:
    return re.findall(r'^\s+"([^"]+@sha256:[0-9a-f]{64})"$', ROLE_SH, re.M)


def _script_alpine() -> str:
    return re.search(r'^ALPINE_BASE="([^"]+)"$', ROLE_SH, re.M).group(1)


def test_the_alpine_base_is_every_local_images_from():
    froms = {next(ln.split()[1] for ln in d.read_text().splitlines() if ln.startswith("FROM ")) for d in DOCKERFILES}
    assert froms == {_script_alpine()}
    assert PF_DEFAULTS["tn_prefetch_greyspace_alpine_image"] == _script_alpine()


def test_prefetch_warms_exactly_what_greyspace_host_pulls():
    assert PF_DEFAULTS["tn_prefetch_registry_images"] == [*_script_pinned(), _script_alpine()]
    assert set(_script_pinned()) == {*config.IMAGES.values(), npc.IMAGE}


def test_prefetch_warms_the_scripts_apt_and_apk_packages():
    installs = re.findall(r"apt-get install -y ([^\n]+)", ROLE_SH)
    assert installs[0].split() == PF_DEFAULTS["tn_prefetch_greyspace_apt"]
    assert installs[1].split() == PF_DEFAULTS["tn_prefetch_greyspace_docker_apt"]
    apk = set()
    for d in DOCKERFILES:
        apk |= set(re.search(r"apk add --no-cache ([^\\\n]+)", d.read_text()).group(1).split())
    assert apk == set(PF_DEFAULTS["tn_prefetch_greyspace_apk"])
    fpr = re.search(r"^DOCKER_KEY_FPR=([0-9A-F]{40})$", ROLE_SH, re.M).group(1)
    assert PF_DEFAULTS["tn_prefetch_docker_key_fpr"] == fpr == "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"


def test_local_images_build_unchanged_without_a_mirror():
    for d in DOCKERFILES:
        text = d.read_text()
        assert "ARG APK_MIRROR=\n" in text, d  # empty default: the image's own https repositories
        assert 'if [ -n "$APK_MIRROR" ]; then sed -i' in text, d


# ── greyspace-host.sh ──────────────────────────────────────────────────
PROXY_CASES = [
    ("quay.io/frrouting/frr:10.1.1@sha256:" + "a" * 64, "d:8084/frrouting/frr@sha256:" + "a" * 64),
    ("coredns/coredns:1.11.3@sha256:" + "b" * 64, "d:8082/coredns/coredns@sha256:" + "b" * 64),
    ("nginx:1.27-alpine@sha256:" + "c" * 64, "d:8082/library/nginx@sha256:" + "c" * 64),
    ("docker.io/library/alpine:3.20@sha256:" + "d" * 64, "d:8082/library/alpine@sha256:" + "d" * 64),
    ("python@sha256:" + "e" * 64, "d:8082/library/python@sha256:" + "e" * 64),
]
PROXY_ERRORS = ["nginx:1.27-alpine", "ghcr.io/x/y:1@sha256:" + "f" * 64, "registry.k8s.io/pause:3.9@sha256:" + "0" * 64]


def _bash(snippet: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    return subprocess.run([bash, "-c", f'source "{SCRIPT}"; {snippet}'], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(("image", "want"), PROXY_CASES)
def test_script_maps_each_image_to_its_depot_proxy(image, want):
    r = _bash(f'tn_proxy_ref "{image}" d:8082 d:8084')
    assert (r.returncode, r.stdout.strip()) == (0, want), r.stderr
    tag = _bash(f'tn_strip_digest "{image}"').stdout.strip()
    assert tag == image.split("@")[0]


@pytest.mark.parametrize("image", PROXY_ERRORS)
def test_script_refuses_unpinned_or_unproxied_images(image):
    r = _bash(f'tn_proxy_ref "{image}" d:8082 d:8084')
    assert r.returncode != 0 and r.stdout == ""


def test_sourcing_the_script_runs_nothing():
    r = _bash("echo sourced")
    assert r.returncode == 0 and r.stdout.strip() == "sourced"


def test_daemon_json_names_registries_only_while_building():
    build = json.loads(_bash("tn_daemon_json 10.30.31.30:8082 10.30.31.30:8084").stdout)
    sealed = json.loads(_bash("tn_daemon_json").stdout)
    assert build["insecure-registries"] == ["10.30.31.30:8082", "10.30.31.30:8084"]
    assert "insecure-registries" not in sealed and "registry-mirrors" not in sealed
    for cfg in (build, sealed):
        assert cfg["features"] == {"containerd-snapshotter": True}  # digest references resolve offline
        assert cfg["log-opts"] == {"max-size": "20m", "max-file": "5"}
    main = ROLE_SH[ROLE_SH.index("main() {") :]
    seal = main.index("tn_daemon_json | sudo tee /etc/docker/daemon.json", main.index("# Seal:"))
    assert seal > main.index('sudo docker build "${build_args[@]}"')  # sealed after the last registry use
    assert main.index('sudo docker image inspect "$image"', seal) > seal  # and re-checked after the restart


def test_with_a_proxy_nothing_is_fetched_from_the_internet_directly():
    main = ROLE_SH[ROLE_SH.index("main() {") :]
    proxied = main[
        main.index('if [[ -n "${TN_APT_PROXY:-}" ]]; then\n    docker_repo') : main.index(
            "  else\n    curl -fsSL https://"
        )
    ]
    assert 'curl -fsSL --proxy "$TN_APT_PROXY" "$DOCKER_APT_HTTP/gpg"' in proxied
    assert "https://" not in proxied
    assert 'build_args=(--build-arg "http_proxy=${TN_APT_PROXY}" --build-arg "APK_MIRROR=${APK_MIRROR_HTTP}")' in main
    assert '[[ $fpr != "$DOCKER_KEY_FPR" ]]' in main


def test_registry_credentials_never_stay_in_the_image():
    main = ROLE_SH[ROLE_SH.index("main() {") :]
    assert "trap tn_cleanup_registry_login EXIT" in main
    logins = [ln for ln in main.splitlines() if "docker login" in ln]
    assert logins and all("--password-stdin" in ln and not re.search(r"\s(-p|--password)\s", ln) for ln in logins)
    cleanup = ROLE_SH[ROLE_SH.index("tn_cleanup_registry_login() {") : ROLE_SH.index("main() {")]
    assert "rm -f /root/.docker/config.json" in cleanup


def test_packer_hands_the_depot_proxies_and_the_read_user_to_the_role():
    hcl = (ROOT / "infra/vsphere/packer/derived.pkr.hcl").read_text()
    block = hcl[hcl.index('script = "${path.root}/files/linux/roles/greyspace-host.sh"') - 600 :]
    for var in (
        "TN_APT_PROXY=${var.apt_proxy}",
        "TN_DOCKERHUB_PROXY=${local.depot_dockerhub_reg}",
        "TN_QUAY_PROXY=${local.depot_quay_reg}",
        "TN_DEPOT_USER=${var.depot_user}",
        "TN_DEPOT_PASSWORD=${var.depot_password}",
    ):
        assert var in block
    variables = (ROOT / "infra/vsphere/packer/variables.pkr.hcl").read_text()
    assert re.search(r'variable "depot_dockerhub_port" \{[^}]*default\s+= 8082', variables)
    assert re.search(r'variable "depot_quay_port" \{[^}]*default\s+= 8084', variables)


def test_shellcheck_when_available():
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, "-S", "warning", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout


# ── registry_prefetch.py ───────────────────────────────────────────────
def _helper():
    spec = importlib.util.spec_from_file_location("registry_prefetch", PREFETCH / "files/registry_prefetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(("image", "want"), PROXY_CASES)
def test_helper_maps_images_like_the_script(image, want):
    base, path, digest = _helper().proxy_ref(image, "http://d:8082", "http://d:8084")
    host = "d:8084" if base.endswith("8084") else "d:8082"
    assert f"{host}/{path}@{digest}" == want


@pytest.mark.parametrize("image", PROXY_ERRORS)
def test_helper_refuses_what_the_script_refuses(image):
    with pytest.raises(ValueError):
        _helper().proxy_ref(image, "http://d:8082", "http://d:8084")
