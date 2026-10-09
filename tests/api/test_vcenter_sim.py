"""tools/vcenter-sim serves what the vSphere dashboards and the installer read.

The real dashboard backend (hypervisor_backends/vsphere.py) runs against the simulator:
the connection test passes and discovery records its hosts with their VM counts.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools/vcenter-sim/vcenter_sim.py"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the TLS pair")


def _load_sim():
    spec = importlib.util.spec_from_file_location("vcenter_sim", SIM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sim_base(tmp_path_factory):
    import ssl
    from http.server import ThreadingHTTPServer

    tmp = tmp_path_factory.mktemp("vcsim")
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    sim = _load_sim()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sim.Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _session(client: httpx.Client, base: str) -> dict:
    r = client.post(f"https://{base}/api/session", auth=("u", "p"))
    assert r.status_code == 201
    return {"vmware-api-session-id": r.json()}


def test_requires_a_session(sim_base) -> None:
    with httpx.Client(verify=False) as c:
        assert c.get(f"https://{sim_base}/api/vcenter/host").status_code == 401
        assert c.post(f"https://{sim_base}/api/session").status_code == 401


def test_installer_lookups_resolve(sim_base) -> None:
    with httpx.Client(verify=False) as c:
        h = _session(c, sim_base)
        for kind, name in [("datacenter", "DC0"), ("cluster", "DC0_C0"), ("datastore", "LocalDS_0")]:
            r = c.get(f"https://{sim_base}/api/vcenter/{kind}", params={"names": name}, headers=h)
            assert [o["name"] for o in r.json()] == [name]
        dvpg = c.get(f"https://{sim_base}/api/vcenter/network", params={"types": "DISTRIBUTED_PORTGROUP"}, headers=h)
        assert len(dvpg.json()) == 1
        # It builds nothing: the installer's privilege probe reads 403.
        assert c.post(f"https://{sim_base}/api/vcenter/vm", json={"spec": {}}, headers=h).status_code == 403
        assert c.delete(f"https://{sim_base}/api/session", headers=h).status_code == 204


def test_dashboard_backend_reads_the_simulator(sim_base, monkeypatch) -> None:
    from app.hypervisor_backends import vsphere

    monkeypatch.setattr(vsphere, "unseal", lambda _: "p")
    conn = SimpleNamespace(host=sim_base, username="u", password_encrypted="x", verify_ssl=False)
    added = []

    class _Query:
        def filter(self, *a):
            return self

        def first(self):
            return None

    db = SimpleNamespace(commit=lambda: None, add=added.append, query=lambda *_: _Query())

    result = vsphere.check_connection(conn, db)
    assert result.success, result.message
    assert result.nodes_found == 4

    out = vsphere.discover("conn-1", conn, db)
    assert out["nodes_discovered"] == 4
    counts = {n.node_name: (n.status, n.vm_count) for n in added}
    assert counts["10.10.0.11"] == ("online", 5)
    assert counts["10.10.0.14"] == ("offline", 0)
