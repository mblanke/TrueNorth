#cloud-config
# TrueNorth Range — Ubuntu 24.04 autoinstall seed for the vSphere ubuntu-lts build.
# Rendered by templatefile() in linux.pkr.hcl (user = ssh_username, password_hash =
# ssh_password_hash, apt_proxy) and delivered on a second CD labelled "cidata"
# (cd_content + cd_label="cidata"), so the only kernel arg the build needs is `autoinstall`.
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  storage:
    layout:
      name: lvm
%{ if apt_proxy != "" ~}
  # Build-time apt proxy (TN-DEPOT01 apt-cacher-ng). curtin writes it to
  # /etc/apt/apt.conf.d/90curtin-aptproxy; cleanup.sh removes it before templating.
  apt:
    proxy: "${apt_proxy}"
%{ endif ~}
  identity:
    hostname: ubuntu-lts
    username: ${user}
    password: "${password_hash}"
  ssh:
    install-server: true
    allow-pw: true
  packages:
    - open-vm-tools
    - cloud-init
  late-commands:
    - "echo '${user} ALL=(ALL) NOPASSWD:ALL' > /target/etc/sudoers.d/${user}"
    - "chmod 440 /target/etc/sudoers.d/${user}"
