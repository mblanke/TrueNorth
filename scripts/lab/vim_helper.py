#!/usr/bin/env python3
"""TrueNorth lab - the two vCenter changes govc has no flag for.

  vim_helper.py pg-security --name dPG-TN-SVC   # promiscuous / forged / MAC changes = reject
  vim_helper.py vapp-off    --vm TN-DEPOT01     # drop the OVF/vApp config a clone inherits

Connection and credentials come from the same environment as govc (GOVC_URL,
GOVC_USERNAME, GOVC_PASSWORD, GOVC_INSECURE). Nothing secret is printed.

Why vapp-off: tmpl-ubuntu-2404 was imported from Ubuntu's cloud-image OVA, so it carries
a vApp ProductSection with transport com.vmware.guestInfo. vCenter then publishes
guestinfo.ovfEnv at power-on, cloud-init's ds-identify finds the OVF datasource, and OVF
sorts ahead of VMware in Ubuntu's datasource_list. The clone would boot with the OVA's
empty defaults and ignore guestinfo.metadata/userdata (static IP, tnadmin key).
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import time
from urllib.parse import urlparse

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
except ImportError:  # pragma: no cover - reported by lib.sh before we get here
    sys.exit("pyVmomi is not installed")


def connect():
    url = os.environ.get("GOVC_URL", "https://192.168.1.10/sdk")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    user = os.environ.get("GOVC_USERNAME") or parsed.username
    pwd = os.environ.get("GOVC_PASSWORD") or parsed.password
    if not user or not pwd:
        sys.exit("GOVC_USERNAME / GOVC_PASSWORD are not set")
    ctx = None
    if os.environ.get("GOVC_INSECURE", "1").lower() in ("1", "true", "yes"):
        ctx = ssl._create_unverified_context()  # lab VCSA: self-signed certificate
    return SmartConnect(host=parsed.hostname, port=parsed.port or 443, user=user, pwd=pwd, sslContext=ctx)


def find(si, vimtype, name):
    view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vimtype], True)
    try:
        hits = [o for o in view.view if o.name == name]
    finally:
        view.Destroy()
    if len(hits) != 1:
        sys.exit(f"expected exactly one {vimtype.__name__} named {name!r}, found {len(hits)}")
    return hits[0]


def wait(task, what):
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        time.sleep(1)
    if task.info.state != vim.TaskInfo.State.success:
        sys.exit(f"{what} failed: {task.info.error.msg if task.info.error else task.info.state}")


def pg_security(si, name):
    pg = find(si, vim.dvs.DistributedVirtualPortgroup, name)
    sp = pg.config.defaultPortConfig.securityPolicy
    now = {k: getattr(sp, k).value for k in ("allowPromiscuous", "macChanges", "forgedTransmits")}
    if not any(now.values()) and sp.inherited is False:
        print(f"  ok    {name}: security policy already explicit reject/reject/reject")
        return
    off = vim.BoolPolicy(inherited=False, value=False)
    spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
        configVersion=pg.config.configVersion,
        defaultPortConfig=vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
            securityPolicy=vim.dvs.VmwareDistributedVirtualSwitch.SecurityPolicy(
                inherited=False, allowPromiscuous=off, macChanges=off, forgedTransmits=off)))
    wait(pg.ReconfigureDVPortgroup_Task(spec), f"reconfigure {name}")
    print(f"  set   {name}: promiscuous/MAC changes/forged transmits -> reject (was {now})")


def vapp_off(si, name):
    vm = find(si, vim.VirtualMachine, name)
    if vm.config.vAppConfig is None:
        print(f"  ok    {name}: no vApp/OVF config")
        return
    if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOff:
        sys.exit(f"{name} is powered on; vApp config can only be removed while it is off")
    wait(vm.ReconfigVM_Task(vim.vm.ConfigSpec(vAppConfigRemoved=True)), f"remove vApp config from {name}")
    print(f"  set   {name}: vApp/OVF config removed (cloud-init will use guestinfo.metadata)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pg-security").add_argument("--name", required=True)
    sub.add_parser("vapp-off").add_argument("--vm", required=True)
    args = ap.parse_args()
    si = connect()
    try:
        if args.cmd == "pg-security":
            pg_security(si, args.name)
        else:
            vapp_off(si, args.vm)
    finally:
        Disconnect(si)


if __name__ == "__main__":
    main()
