#!/bin/vbash
# TrueNorth Range - VyOS template build, step 1 of 2 (live ISO, as user vyos).
# boot_command runs this from the build's second CD (TNVYOS) before tn-vyos-install.py.
# Drops the template's NIC MAC pins (hw-id) from the live config and saves it, so the
# config that `install image` copies into the template pins nothing: each clone's NICs are
# pinned by the per-range config instead (worker/vyos_config.py, set ... hw-id <mac>).
# A missing hw-id is not an error ("Nothing to delete").
source /opt/vyatta/etc/functions/script-template
configure
delete interfaces ethernet eth0 hw-id
delete interfaces ethernet eth1 hw-id
commit
save
exit
