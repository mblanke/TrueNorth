#!/usr/bin/env bash
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
# Run through Packer (derived.pkr.hcl, build greyspace-host) with the depot as apt and
# registry mirror where the build network has no internet (TN_APT_PROXY, TN_REGISTRY_MIRROR).
set -euo pipefail
echo "=== greyspace-host role"
export DEBIAN_FRONTEND=noninteractive

if [[ -n "${TN_APT_PROXY:-}" ]]; then
  echo "Acquire::http::Proxy \"${TN_APT_PROXY}\";" | sudo tee /etc/apt/apt.conf.d/90tn-proxy >/dev/null
fi
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg nfs-common open-vm-tools python3 dnsutils iptables

# Docker Engine from Docker's apt repository (Ubuntu's docker.io may predate routed bridges).
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
major="$(docker --version | sed -E 's/Docker version ([0-9]+).*/\1/')"
if (( major < 27 )); then echo "Docker $major is older than 27 (routed bridges)" >&2; exit 1; fi

# Bounded logs: an exercise's NPC traffic must not fill the disk.
echo '{"log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "5"}'"${TN_REGISTRY_MIRROR:+, \"registry-mirrors\": [\"$TN_REGISTRY_MIRROR\"]}"'}' \
  | sudo tee /etc/docker/daemon.json >/dev/null
sudo systemctl enable --now docker
sudo systemctl restart docker

# Pinned images (app/greyspace/config.py IMAGES + npc.IMAGE).
PINNED=(
  "quay.io/frrouting/frr:10.1.1@sha256:8943ad2991f084de2c6a9560fbbff243e7317a7ff9f18b3da01792e2e980a8be"
  "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e"
  "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
  "axllent/mailpit:v1.27.10@sha256:b1f1be18af530d939a11ee8820b379e0c88eeec204d904bfad68862adced3a5a"
  "python:3.12-alpine3.20@sha256:25849f9599e06dfe4d11b552e06f5ac4cc2ad342054eb81f7877e611f6f87c66"
)
for image in "${PINNED[@]}"; do sudo docker pull "$image"; done

# Local images (config.LOCAL_IMAGES), tagged config.LOCAL_TAG.
LOCAL=(resolver probe ntp ca)
for name in "${LOCAL[@]}"; do
  sudo docker build -t "truenorth/greyspace-${name}:gs1" "/tmp/greyspace-images/${name}"
done
sudo mkdir -p /opt/greyspace/images /srv/greyspace/corpus
sudo cp -r /tmp/greyspace-images/. /opt/greyspace/images/
rm -rf /tmp/greyspace-images

# gs-core forwards range traffic onto its Greyspace bridge (bin/gs up adds the routes).
echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/90-greyspace.conf >/dev/null
sudo rm -f /etc/apt/apt.conf.d/90tn-proxy
echo "=== greyspace-host ready: $(sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | wc -l) images"
