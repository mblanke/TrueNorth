#!/usr/bin/env python3
"""Print the cloud-init guestinfo the worker gives a Linux range VM, as govc -e arguments.

scripts/lab/test-clone.sh uses it so a Linux test clone is configured by the SAME code path
as a range VM: the worker's ``vsphere_infra.linux_guestinfo`` (vsphere_api.py
``_customize_sync`` calls it; there is no vSphere Linux guest customization). It imports that
module from this checkout as part of a stand-in package for its directory, so its sibling
imports (vsphere_tags, vsphere_vlans, ...) resolve but the provisioners package's
``__init__`` (every provisioner and its SDK) does not run. It needs PyYAML and nothing else
(pyVmomi is replaced by a two-attribute stand-in for ``vim.option.OptionValue``).

    python3 scripts/lab/linux_guestinfo.py --name tntc-esx02 --ip 10.30.31.241 \
        --mac 00:50:56:aa:bb:cc
    guestinfo.metadata=<base64>
    guestinfo.metadata.encoding=base64
    guestinfo.userdata=<base64>
    guestinfo.userdata.encoding=base64
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROVISIONERS = REPO / "control-plane/worker/worker/provisioners"
PACKAGE = "tn_lab_provisioners"


def _load_infra():
    if PACKAGE not in sys.modules:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [str(PROVISIONERS)]  # a namespace for the siblings; no __init__ runs
        sys.modules[PACKAGE] = pkg
    mod = importlib.import_module(f"{PACKAGE}.vsphere_infra")
    if mod.vim is None:  # no pyVmomi here: the worker's code only builds OptionValue(key, value)
        mod.vim = types.SimpleNamespace(option=types.SimpleNamespace(OptionValue=types.SimpleNamespace))
    return mod


def guestinfo(name: str, ip: str, mac: str, prefix: int = 24) -> list[tuple[str, str]]:
    infra = _load_infra()
    vm_def = {"name": name, "node_id": name, "nics": [{"ip": ip, "prefix": prefix}]}
    return [(o.key, o.value) for o in infra.linux_guestinfo(vm_def, [mac])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True, help="hostname (the worker's node_id)")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--prefix", type=int, default=24)
    ap.add_argument("--mac", required=True, help="MAC of the clone's first NIC")
    a = ap.parse_args(argv)
    for key, value in guestinfo(a.name, a.ip, a.mac, a.prefix):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
