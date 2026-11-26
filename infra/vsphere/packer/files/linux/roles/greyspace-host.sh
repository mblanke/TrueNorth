#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-10-10T01:46:01Z
# TrueNorth derived image: greyspace-host, the image a range's Greyspace gs-core VM is
# cloned from (ADR 0007; worker/greyspace_host.py, GREYSPACE_HOST_TEMPLATE).
#
# Ranges have no internet, so everything the stack runs is in the image:
#   * Docker Engine >= 27 (the gs-core network is a routed bridge,
#     com.docker.network.bridge.gateway_mode_ipv4=routed) with the compose plugin;
#   * nfs-common (the corpus volume, mounted read-only), open-vm-tools (guestinfo and the
#     guest operations the configure stage uses), python3 (bin/gs), dnsutils for operators;
#   * every pinned image of app/greyspace/config.py IMAGES and npc.IMAGE, pulled by digest,
#     and the local images (greyspace/images/*, uploaded to /tmp/greyspace-images by Packer)
#     built and tagged truenorth/greyspace-<name>:gs1.
# tests/contracts/test_greyspace_host_image.py keeps the lists below equal to the code's.
#
# Run through Packer (derived.pkr.hcl, build greyspace-host). On a build network with no
# internet everything comes from TN-DEPOT01 (install/lab/roles/tn_depot), never upstream:
#   TN_APT_PROXY       apt-cacher-ng, http://host:3142. Ubuntu, Docker's apt repository
#                      (Remap-tndocker, asked for over http; the depot fetches it over
#                      https) and its signing key, and the Alpine packages of the local
#                      images (Remap-tnalpine, APK_MIRROR build argument).
#   TN_DOCKERHUB_PROXY Nexus docker-proxy (Docker Hub), host:8082, plain HTTP.
#   TN_QUAY_PROXY      Nexus quay-proxy (quay.io), host:8084, plain HTTP.
#   TN_DEPOT_USER / TN_DEPOT_PASSWORD  the Nexus build-read user (tn-build-read); docker
#                      login for the two proxies. Removed from the image before it is sealed.
# Docker's registry-mirrors would cover Docker Hub only (not quay.io) and cannot carry
# credentials, so every image is pulled explicitly from its depot proxy by digest and tagged
# with the name the stack uses. With the containerd image store (set below, checked) the
# stack's exact "name:tag@sha256:..." reference then resolves locally, offline; with the
# classic store it does not (tested 2026-11-23 on docker:29-dind 29.10 with a pull-through
# registry: inspect, `docker run --pull never`, `docker compose up` and a BuildKit build
# `FROM alpine:3.20@sha256:...` all worked with the network disconnected, after the proxy
# name was removed). The build checks every reference with `docker image inspect` before
# and after the final daemon restart.
set -euo pipefail

# Docker's Release signing key (https://docs.docker.com/engine/install/ubuntu/).
DOCKER_KEY_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

# Pinned images (app/greyspace/config.py IMAGES + npc.IMAGE).
PINNED=(
  "quay.io/frrouting/frr:10.1.1@sha256:8943ad2991f084de2c6a9560fbbff243e7317a7ff9f18b3da01792e2e980a8be"
  "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e"
  "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
  "axllent/mailpit:v1.27.10@sha256:b1f1be18af530d939a11ee8820b379e0c88eeec204d904bfad68862adced3a5a"
  "python:3.12-alpine3.20@sha256:25849f9599e06dfe4d11b552e06f5ac4cc2ad342054eb81f7877e611f6f87c66"
)
# The FROM of every greyspace/images/*/Dockerfile: pulled the same way, so the local
# builds never reach a registry.
ALPINE_BASE="alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
# Local images (config.LOCAL_IMAGES), tagged config.LOCAL_TAG.
LOCAL=(resolver probe ntp ca)
# Asked for through TN_APT_PROXY (plain http; apt-cacher-ng has no CONNECT).
DOCKER_APT_HTTP="http://download.docker.com/linux/ubuntu"
APK_MIRROR_HTTP="http://dl-cdn.alpinelinux.org/alpine"

# tn_strip_digest IMAGE -> IMAGE without "@sha256:..." (the name:tag the stack's reference carries).
tn_strip_digest() {
  printf '%s\n' "${1%@*}"
}

# tn_proxy_ref IMAGE HUB QUAY -> the same manifest, by digest, through the depot proxy for
# its registry: <hub>/<path>@sha256:... for Docker Hub (official images under library/),
# <quay>/<path>@sha256:... for quay.io. Any other registry, or no digest, is an error: the
# depot proxies only these two, and only a digest pins what the stack runs.
tn_proxy_ref() {
  local image=$1 hub=$2 quay=$3 name digest first rest
  if [[ $image != *@sha256:* ]]; then echo "tn_proxy_ref: $image is not pinned by digest" >&2; return 1; fi
  digest=${image##*@}
  name=${image%@*}
  if [[ ${name##*/} == *:* ]]; then name=${name%:*}; fi  # drop the tag (never a registry port)
  first=${name%%/*}
  if [[ $name == */* && ( $first == *.* || $first == *:* || $first == localhost ) ]]; then
    rest=${name#*/}
  else
    first=docker.io
    rest=$name
  fi
  case $first in
    docker.io | registry-1.docker.io | index.docker.io)
      [[ -n $hub ]] || { echo "tn_proxy_ref: no Docker Hub proxy for $image" >&2; return 1; }
      [[ $rest == */* ]] || rest="library/$rest"
      printf '%s/%s@%s\n' "$hub" "$rest" "$digest" ;;
    quay.io)
      [[ -n $quay ]] || { echo "tn_proxy_ref: no quay.io proxy for $image" >&2; return 1; }
      printf '%s/%s@%s\n' "$quay" "$rest" "$digest" ;;
    *)
      echo "tn_proxy_ref: $image is on $first; the depot proxies only Docker Hub and quay.io" >&2
      return 1 ;;
  esac
}

# tn_daemon_json [INSECURE_REGISTRY...] -> /etc/docker/daemon.json content. Bounded logs (an
# exercise's NPC traffic must not fill the disk) and the containerd image store, which the
# digest references need. Insecure registries only while building: the depot proxies are
# plain HTTP on the isolated build network, and the sealed image names none.
tn_daemon_json() {
  local regs="" r
  for r in "$@"; do regs+="${regs:+, }\"$r\""; done
  printf '{"log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "5"}, "features": {"containerd-snapshotter": true}%s}\n' \
    "${regs:+, \"insecure-registries\": [$regs]}"
}

# tn_key_fingerprint FILE -> fingerprint of the one primary key in an (armored) OpenPGP key
# file; nothing when the file holds no key or more than one (all of it would be trusted).
tn_key_fingerprint() {
  gpg --show-keys --with-colons "$1" 2>/dev/null \
    | awk -F: '$1 == "pub" { n++; want = 1; next } $1 == "fpr" && want { f = $10; want = 0 } END { if (n == 1) print f }'
}

tn_cleanup_registry_login() {
  sudo rm -f /root/.docker/config.json
  if [[ -n "${TN_DOCKERHUB_PROXY:-}" ]]; then sudo docker logout "$TN_DOCKERHUB_PROXY" >/dev/null 2>&1 || true; fi
  if [[ -n "${TN_QUAY_PROXY:-}" ]]; then sudo docker logout "$TN_QUAY_PROXY" >/dev/null 2>&1 || true; fi
  sudo rm -f /root/.docker/config.json
}

main() {
  echo "=== greyspace-host role"
  export DEBIAN_FRONTEND=noninteractive
  local hub="${TN_DOCKERHUB_PROXY:-}" quay="${TN_QUAY_PROXY:-}"
  if [[ -n $hub || -n $quay ]] && [[ -z $hub || -z $quay ]]; then
    echo "TN_DOCKERHUB_PROXY and TN_QUAY_PROXY go together (both depot proxies or neither)" >&2; exit 1
  fi

  if [[ -n "${TN_APT_PROXY:-}" ]]; then
    echo "Acquire::http::Proxy \"${TN_APT_PROXY}\";" | sudo tee /etc/apt/apt.conf.d/90tn-proxy >/dev/null
  fi
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg nfs-common open-vm-tools python3 dnsutils iptables

  # Docker Engine from Docker's apt repository (Ubuntu's docker.io may predate routed
  # bridges). With a depot: the key and the repository through apt-cacher-ng, over http
  # (the depot fetches them over https); never straight to the internet.
  local docker_repo="https://download.docker.com/linux/ubuntu" key
  key="$(mktemp)"
  if [[ -n "${TN_APT_PROXY:-}" ]]; then
    docker_repo=$DOCKER_APT_HTTP
    curl -fsSL --proxy "$TN_APT_PROXY" "$DOCKER_APT_HTTP/gpg" -o "$key"
  else
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$key"
  fi
  local fpr
  fpr="$(tn_key_fingerprint "$key")"
  if [[ $fpr != "$DOCKER_KEY_FPR" ]]; then
    echo "Docker's apt key has fingerprint '${fpr}', expected ${DOCKER_KEY_FPR}" >&2; exit 1
  fi
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "$key"
  rm -f "$key"
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${docker_repo} ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  # The image keeps Docker's canonical https source (a range never reaches it anyway).
  sudo sed -i "s#${DOCKER_APT_HTTP}#https://download.docker.com/linux/ubuntu#" /etc/apt/sources.list.d/docker.list
  local major
  major="$(docker --version | sed -E 's/Docker version ([0-9]+).*/\1/')"
  if (( major < 27 )); then echo "Docker $major is older than 27 (routed bridges)" >&2; exit 1; fi

  if [[ -n $hub ]]; then
    tn_daemon_json "$hub" "$quay" | sudo tee /etc/docker/daemon.json >/dev/null
  else
    tn_daemon_json | sudo tee /etc/docker/daemon.json >/dev/null
  fi
  sudo systemctl enable --now docker
  sudo systemctl restart docker
  if ! sudo docker info --format '{{json .DriverStatus}}' | grep -q 'io.containerd.snapshotter'; then
    echo "Docker is not using the containerd image store: pinned references would not resolve offline" >&2; exit 1
  fi

  trap tn_cleanup_registry_login EXIT
  if [[ -n $hub && -n "${TN_DEPOT_USER:-}" ]]; then
    local reg
    for reg in "$hub" "$quay"; do
      printf '%s' "${TN_DEPOT_PASSWORD:-}" | sudo docker login --username "$TN_DEPOT_USER" --password-stdin "$reg" >/dev/null
    done
  fi

  local image src
  for image in "${PINNED[@]}" "$ALPINE_BASE"; do
    if [[ -n $hub ]]; then
      src="$(tn_proxy_ref "$image" "$hub" "$quay")"
      sudo docker pull "$src"
      sudo docker tag "$src" "$(tn_strip_digest "$image")"
      sudo docker rmi "$src" >/dev/null
    else
      sudo docker pull "$image"
    fi
    sudo docker image inspect "$image" >/dev/null || { echo "$image does not resolve locally" >&2; exit 1; }
  done
  tn_cleanup_registry_login

  local name build_args=()
  if [[ -n "${TN_APT_PROXY:-}" ]]; then
    build_args=(--build-arg "http_proxy=${TN_APT_PROXY}" --build-arg "APK_MIRROR=${APK_MIRROR_HTTP}")
  fi
  for name in "${LOCAL[@]}"; do
    sudo docker build "${build_args[@]}" -t "truenorth/greyspace-${name}:gs1" "/tmp/greyspace-images/${name}"
  done
  sudo mkdir -p /opt/greyspace/images /srv/greyspace/corpus
  sudo cp -r /tmp/greyspace-images/. /opt/greyspace/images/
  rm -rf /tmp/greyspace-images

  # Seal: no build-network registries in the image, then every reference must still
  # resolve after a daemon restart, exactly as the stack names it.
  tn_daemon_json | sudo tee /etc/docker/daemon.json >/dev/null
  sudo systemctl restart docker
  for image in "${PINNED[@]}" "$ALPINE_BASE"; do
    sudo docker image inspect "$image" >/dev/null || { echo "$image does not resolve after the restart" >&2; exit 1; }
  done
  for name in "${LOCAL[@]}"; do
    sudo docker image inspect "truenorth/greyspace-${name}:gs1" >/dev/null
  done

  # gs-core forwards range traffic onto its Greyspace bridge (bin/gs up adds the routes).
  echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/90-greyspace.conf >/dev/null
  sudo rm -f /etc/apt/apt.conf.d/90tn-proxy
  echo "=== greyspace-host ready: $(sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | wc -l) images"
}

# Sourced by tests (tests/contracts/test_greyspace_host_image.py) for the helpers only.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
