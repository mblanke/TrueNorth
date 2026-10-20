# TrueNorth Range — Debian 13 preseed (vSphere). Rendered by templatefile() in
# linux.pkr.hcl (user = ssh_username, password_hash = ssh_password_hash, apt_proxy) and
# served over Packer's HTTP server (http_content).
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string debian13
d-i netcfg/get_domain string range.local
# dPG-TN-BUILD has no gateway (ADR 0012): without this the installer stops and asks.
d-i netcfg/no_default_route boolean true

%{ if apt_proxy != "" ~}
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
# Build-time apt proxy (apt_proxy: TN-DEPOT01 apt-cacher-ng); empty = none. cleanup.sh
# removes it from the installed system.
d-i mirror/http/proxy string ${apt_proxy}
%{ else ~}
# Offline base (no depot yet): install from the ISO only, no network mirror.
d-i apt-setup/use_mirror boolean false
d-i mirror/http/proxy string
d-i apt-setup/services-select multiselect
d-i apt-setup/non-free-firmware boolean true
%{ endif ~}

d-i clock-setup/utc boolean true
d-i time/zone string UTC
d-i clock-setup/ntp boolean true

d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

d-i passwd/root-login boolean false
# Generic build login (ADR 0012 decision 5)
d-i passwd/user-fullname string TrueNorth build user
d-i passwd/username string ${user}
d-i passwd/user-password-crypted password ${password_hash}
d-i user-setup/allow-password-weak boolean true

tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string openssh-server open-vm-tools cloud-init sudo curl wget git net-tools
%{ if apt_proxy != "" ~}
d-i pkgsel/upgrade select full-upgrade
%{ else ~}
d-i pkgsel/upgrade select none
%{ endif ~}

d-i preseed/late_command string \
  in-target systemctl enable ssh ; \
  in-target systemctl enable open-vm-tools ; \
  echo "${user} ALL=(ALL) NOPASSWD:ALL" > /target/etc/sudoers.d/${user} ; \
  chmod 0440 /target/etc/sudoers.d/${user}

d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default
d-i finish-install/reboot_in_progress note
# Reboot (not power off): vsphere-iso waits for the installed system's IP and SSH.
