# TrueNorth Range — Kali Linux preseed (vSphere). Ported from the Proxmox seed with the
# guest agent switched from qemu-guest-agent to open-vm-tools for VMware. Rendered by
# templatefile() in linux.pkr.hcl (user = ssh_username, password_hash = ssh_password_hash,
# apt_proxy) and served over Packer's HTTP server (http_content), so the build network
# must be able to reach the Packer host (see README).

# Locale and keyboard
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i console-keymaps-at/keymap select us

# Network
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string kali
d-i netcfg/get_domain string range.local
# dPG-TN-BUILD has no gateway (ADR 0012): without this the installer stops and asks.
d-i netcfg/no_default_route boolean true
d-i netcfg/hostname string kali

# Mirror
%{ if apt_proxy != "" ~}
d-i mirror/country string enter information manually
d-i mirror/http/hostname string http.kali.org
d-i mirror/http/directory string /kali
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

# Clock and timezone
d-i clock-setup/utc boolean true
d-i time/zone string UTC
d-i clock-setup/ntp boolean true

# Partitioning — single ext4 partition, entire disk
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true

# Root account disabled; use the build user
d-i passwd/root-login boolean false

# Generic build login (ADR 0012 decision 5)
d-i passwd/user-fullname string TrueNorth build user
d-i passwd/username string ${user}
d-i passwd/user-password-crypted password ${password_hash}
d-i user-setup/allow-password-weak boolean true
d-i user-setup/encrypt-home boolean false

# Package selection — open-vm-tools instead of qemu-guest-agent
tasksel tasksel/first multiselect standard
# No cloud-init: it is not on the Kali installer ISO (offline build); VMware guest
# customization works through open-vm-tools without it.
d-i pkgsel/include string openssh-server open-vm-tools sudo curl wget git net-tools
%{ if apt_proxy != "" ~}
d-i pkgsel/upgrade select full-upgrade
%{ else ~}
d-i pkgsel/upgrade select none
%{ endif ~}

# Enable SSH and VMware tools
# The installed system must bring its NIC up by DHCP (Packer waits for the IP). Kali's core
# set here has no NetworkManager and no ifupdown config: fall back to systemd-networkd.
d-i preseed/late_command string \
  in-target sh -c 'dpkg -s network-manager >/dev/null 2>&1 || { mkdir -p /etc/systemd/network; printf "[Match]\\nName=en* eth*\\n\\n[Network]\\nDHCP=yes\\n" > /etc/systemd/network/20-tn-dhcp.network; systemctl enable systemd-networkd; }' ; \
  in-target systemctl enable ssh ; \
  in-target systemctl enable open-vm-tools ; \
  echo "${user} ALL=(ALL) NOPASSWD:ALL" > /target/etc/sudoers.d/${user} ; \
  chmod 0440 /target/etc/sudoers.d/${user} ; \
  in-target sed -i 's/^#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config ; \
  in-target sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

# GRUB bootloader
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default

# Avoid the last prompt about install being complete
d-i finish-install/reboot_in_progress note

# Reboot (not power off): vsphere-iso waits for the installed system's IP and SSH.
