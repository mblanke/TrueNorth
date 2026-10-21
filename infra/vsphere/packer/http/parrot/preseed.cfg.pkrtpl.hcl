# TrueNorth Range — Parrot Security 7.3 preseed (vSphere). Rendered by templatefile() in
# linux.pkr.hcl (user = ssh_username, password_hash = ssh_password_hash, apt_proxy) and
# served over Packer's HTTP server (http_content).
# NOTE: the Parrot "security" ISO is a LIVE image that installs via the Calamares GUI
# installer, which does NOT honour a debian-installer preseed. This file works only if
# you boot the ISO's text/"Install" (debian-installer) entry. If your ISO ships Calamares
# only, treat parrot as a keystroke/manual build (see README) — it is marked fragile.
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string parrot
d-i netcfg/get_domain string range.local

d-i mirror/country string manual
d-i mirror/http/hostname string deb.parrot.sh
d-i mirror/http/directory string /parrot
# Build-time apt proxy (apt_proxy: TN-DEPOT01 apt-cacher-ng); empty = none. Only http://
# mirrors go through it (no CONNECT). cleanup.sh removes it from the installed system.
d-i mirror/http/proxy string ${apt_proxy}

d-i clock-setup/utc boolean true
d-i time/zone string UTC

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
d-i pkgsel/include string openssh-server open-vm-tools sudo curl wget git net-tools
d-i pkgsel/upgrade select full-upgrade

# cloud-init is REQUIRED (the worker configures Linux VMs only through its VMware
# datasource); tried here, then by Packer (install-cloud-init.sh), else the build fails.
d-i preseed/late_command string \
  in-target systemctl enable ssh ; \
  in-target systemctl enable open-vm-tools ; \
  in-target sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y cloud-init || true' ; \
  echo "${user} ALL=(ALL) NOPASSWD:ALL" > /target/etc/sudoers.d/${user} ; \
  chmod 0440 /target/etc/sudoers.d/${user}

d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default
d-i finish-install/reboot_in_progress note
d-i debian-installer/exit/poweroff boolean true
