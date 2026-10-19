# TrueNorth Range — Rocky Linux 10 kickstart (vSphere).
# Rendered by templatefile() in linux.pkr.hcl (user = ssh_username, password_hash =
# ssh_password_hash) and delivered on a CD labelled OEMDRV (cd_content + cd_label="OEMDRV");
# Anaconda auto-detects ks.cfg on an OEMDRV volume. The boot arg
# inst.ks=hd:LABEL=OEMDRV:/ks.cfg makes it explicit. Packages come from the DVD; the
# build proxy for later dnf steps (apt_proxy) is set by files/linux/apt-proxy.sh.
text
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --bootproto=dhcp --device=link --activate --hostname=rocky

# Disk
clearpart --all --initlabel
autopart --type=lvm
bootloader --location=mbr

# Accounts (generic build login, ADR 0012 decision 5; guest customization resets on clone)
rootpw --lock
user --name=${user} --iscrypted --password=${password_hash} --groups=wheel
firewall --enabled --ssh
selinux --enforcing
firstboot --disable
reboot

%packages
@^minimal-environment
openssh-server
open-vm-tools
cloud-init
curl
wget
git
net-tools
%end

%post --log=/root/ks-post.log
echo "${user} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${user}
chmod 0440 /etc/sudoers.d/${user}
systemctl enable sshd
systemctl enable vmtoolsd
%end
