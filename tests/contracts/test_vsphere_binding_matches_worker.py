"""The API's copy of the worker's vCenter rules must equal the worker's.

The API may not import the worker (ADR 0001/0003). The browser console resolves a range's
vCenter (app/hypervisor_binding.py) and its endpoint URL (app/hypervisor_ports.py) the way
the worker does (worker/connection_binding.py, provisioners/vsphere_api.py). If they
drift, the console opens a screen on a different vCenter, or a different VM, from the one
the worker built.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from types import SimpleNamespace  # noqa: E402

from app import hypervisor_backends, hypervisor_binding, hypervisor_ports  # noqa: E402
from app.hypervisor_backends.vsphere import base_url  # noqa: E402
from worker import provisioners  # noqa: E402
from worker.provisioners import vsphere_api  # noqa: E402

CASES = [("vc.test", None), ("vc.test", 443), ("vc.test", 8443), ("vc.test", 8006), ("vc.test:9443", 1),
         ("https://vc.test/", "8443"), ("fd00::10", None), ("fd00::10", 8443), ("[fd00::10]", 8443),
         ("[fd00::10]:9443", 1), ("HTTPS://VC.test", None), ("10.0.0.5", 8443), (" vc.test/ ", None)]


@pytest.mark.parametrize(("host", "port"), CASES)
def test_the_api_and_the_worker_build_the_same_endpoint(host, port):
    """The API's vSphere backend (base_url: connection test, discovery, console) and the
    worker (endpoint_url: builds and every later operation) reach one URL for a row."""
    row = SimpleNamespace(host=host, port=port, name="c")
    assert base_url(row) == vsphere_api.endpoint_url(host, port)
    assert hypervisor_binding.connection_url(row) == vsphere_api.endpoint_url(host, port)
    url = vsphere_api.endpoint_url(host, port)
    assert hypervisor_ports.endpoint_key(url) == vsphere_api.endpoint_key(url)


@pytest.mark.parametrize("host", ["https://fd00::10", "vc.test:x", "https://"])
def test_both_refuse_the_same_unusable_addresses(host):
    with pytest.raises(ValueError):
        vsphere_api.endpoint_url(host)
    with pytest.raises(hypervisor_binding.BindingError):
        hypervisor_binding.connection_url(SimpleNamespace(host=host, port=None, name="c"))


def test_the_console_reads_the_annotation_the_worker_writes():
    rid = "abcdef12-3456-7890-abcd-ef1234567890"
    tag = vsphere_api.range_annotation(rid)
    assert hypervisor_binding.owns(rid, "x", "y", tag)
    assert not hypervisor_binding.owns("99999999-0000-0000-0000-000000000000", "x", "y", tag)
    assert hypervisor_binding.owns(rid, f"{rid[:8]}-web", f"{rid[:8]}-web", None)  # pre-tag, by name


def test_the_api_guards_exactly_the_backends_the_worker_binds():
    """A connection family's provisioner_backends (the API's 409 guard) are the worker's
    CREDENTIALS_HYPERVISOR backends for it."""
    by_kind: dict[str, set[str]] = {}
    for backend, kind in provisioners.CREDENTIALS_HYPERVISOR.items():
        by_kind.setdefault(kind, set()).add(backend)
    for kind in hypervisor_backends.supported_hypervisor_types():
        api = set(hypervisor_backends.get_hypervisor_backend(kind).provisioner_backends)
        assert api == by_kind.get(kind, set()), kind
