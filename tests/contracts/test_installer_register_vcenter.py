"""80-seed registers the installer's vCenter as a hypervisor connection (TN-MGMT01, 2026-10-15).

The installer configured the worker's vCenter but never registered it, so /infrastructure
was empty after an install and the acceptance suite's provisioners check found "no
vSphere connection registered". scripts/register_vcenter_inprocess.py registers it through
the API in-process as the bootstrap administrator; the password reaches it only through
the environment.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
SCRIPT = ROOT / "scripts/register_vcenter_inprocess.py"
SECRET = "s3cret-vc-pw"


def _script():
    spec = importlib.util.spec_from_file_location("register_vcenter_inprocess", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeApi:
    """The routes the script uses, over an in-memory list of the tenant's connections."""

    def __init__(self, conns=None, *, test_ok: bool = True, create_status: int = 201) -> None:
        self.conns = [dict(c) for c in (conns or [])]
        self.test_ok = test_ok
        self.create_status = create_status
        self.calls: list[tuple] = []

    def _find(self, cid):
        return next(c for c in self.conns if c["id"] == cid)

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        assert path == "/hypervisors/connections"
        return 200, [dict(c) for c in self.conns]

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        if path == "/hypervisors/connections":
            if self.create_status != 201:
                return self.create_status, {"detail": "refused"}
            row = {k: v for k, v in json.items() if k != "password"}
            row.update(id=f"c{len(self.conns) + 1}", is_active=True)
            if row["is_primary"]:
                for c in self.conns:
                    c["is_primary"] = False
            self.conns.append(row)
            return 201, dict(row)
        cid, action = path.split("/")[3:5]
        if action == "set-primary":
            for c in self.conns:
                c["is_primary"] = c["id"] == cid
            return 200, {"message": "set as primary"}
        if action == "test":
            if self.test_ok:
                return 200, {"success": True, "message": "Connected to vCenter 8.0", "version": "8.0", "nodes_found": 3}
            return 200, {"success": False, "message": "Connection failed: timed out", "nodes_found": 0}
        if action == "discover":
            if self.test_ok:
                return 200, {"message": "Discovered 3 new hosts (3 total)", "nodes_discovered": 3}
            return 200, {"message": "vSphere discovery failed: timed out", "nodes_discovered": 0}
        raise AssertionError(path)

    def patch(self, path, json=None, **kw):
        self.calls.append(("PATCH", path, json))
        conn = self._find(path.rsplit("/", 1)[1])
        conn.update({k: v for k, v in json.items() if k != "password"})
        return 200, dict(conn)

    def writes(self, method=None):
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method)]


ARGS = dict(
    name="Range vCenter", host="vc.work.lab", port=443, username="svc-truenorth@vsphere.local",
    datacenter="DC-Work", verify_ssl=False, password=SECRET,
)


def _existing(**over):
    row = dict(
        id="c1", name="Range vCenter", hypervisor_type="vsphere", host="vc.work.lab", port=443,
        username="svc-truenorth@vsphere.local", datacenter="DC-Work", verify_ssl=False, is_primary=True,
        is_active=True,
    )
    row.update(over)
    return row


def test_creates_the_connection_as_primary_when_absent(capsys) -> None:
    api = _FakeApi()
    assert _script().register(api, **ARGS) == 0
    creates = [c for c in api.writes("POST") if c[1] == "/hypervisors/connections"]
    assert len(creates) == 1
    body = creates[0][2]
    assert body["hypervisor_type"] == "vsphere" and body["host"] == "vc.work.lab" and body["port"] == 443
    assert body["verify_ssl"] is False and body["datacenter"] == "DC-Work" and body["is_primary"] is True
    assert body["password"] == SECRET
    assert [c[1] for c in api.writes()][1:] == ["/hypervisors/connections/c1/test", "/hypervisors/connections/c1/discover"]
    out = capsys.readouterr().out
    assert "created 'Range vCenter'" in out and "vcenter test: ok" in out and "vcenter discover: ok" in out
    assert SECRET not in out


def test_no_duplicate_when_present_and_nothing_patched(capsys) -> None:
    api = _FakeApi([_existing(host="VC.work.lab")])  # host match ignores case
    assert _script().register(api, **ARGS) == 0
    assert len(api.conns) == 1
    assert not api.writes("PATCH")
    assert [c[1] for c in api.writes()] == ["/hypervisors/connections/c1/test", "/hypervisors/connections/c1/discover"]
    assert "unchanged 'Range vCenter'" in capsys.readouterr().out


def test_patches_drift_but_not_the_password_by_default(capsys) -> None:
    api = _FakeApi([_existing(username="old@vsphere.local", datacenter="DC-Old", verify_ssl=True)])
    assert _script().register(api, **ARGS) == 0
    (patch,) = api.writes("PATCH")
    assert patch[1] == "/hypervisors/connections/c1"
    assert patch[2] == {"username": "svc-truenorth@vsphere.local", "datacenter": "DC-Work", "verify_ssl": False}
    assert len(api.conns) == 1
    out = capsys.readouterr().out
    assert "updated 'Range vCenter' (datacenter, username, verify_ssl)" in out and SECRET not in out


def test_update_password_only_when_asked(capsys) -> None:
    api = _FakeApi([_existing()])
    assert _script().register(api, **ARGS, update_password=True) == 0
    (patch,) = api.writes("PATCH")
    assert patch[2] == {"password": SECRET}
    assert SECRET not in capsys.readouterr().out


def test_made_primary_when_the_tenant_has_none() -> None:
    api = _FakeApi([_existing(is_primary=False)])
    assert _script().register(api, **ARGS) == 0
    assert ("POST", "/hypervisors/connections/c1/set-primary", None) in api.calls
    assert api.conns[0]["is_primary"] is True


def test_another_primary_is_left_alone() -> None:
    other = _existing(id="c9", name="Other vCenter", host="vc.other.lab", is_primary=True)
    api = _FakeApi([other])
    assert _script().register(api, **ARGS) == 0
    create = next(c for c in api.writes("POST") if c[1] == "/hypervisors/connections")
    assert create[2]["is_primary"] is False
    assert not any(c[1].endswith("/set-primary") for c in api.calls)
    assert api.conns[0]["is_primary"] is True


def test_a_failed_test_warns_but_the_connection_is_registered(capsys) -> None:
    api = _FakeApi(test_ok=False)
    assert _script().register(api, **ARGS) == 0
    assert len(api.conns) == 1
    out = capsys.readouterr().out
    assert "vcenter test: WARNING failed" in out and "vcenter discover: WARNING failed" in out


def test_a_refused_create_or_no_password_fails() -> None:
    mod = _script()
    assert mod.register(_FakeApi(create_status=409), **ARGS) == 1
    api = _FakeApi()
    assert mod.register(api, **{**ARGS, "password": ""}) == 1
    assert not api.writes()


# ── The installer task ─────────────────────────────────────────────────────


def _task(name: str) -> dict:
    tasks = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_the_task_passes_the_password_only_through_the_environment_with_no_log() -> None:
    task = _task("Seed — register the vCenter as a hypervisor connection")
    assert task["no_log"] is True
    assert task["environment"] == {"TN_VCENTER_PASSWORD": "{{ vault_vsphere_password | default('') }}"}
    argv = task["ansible.builtin.command"]["argv"]
    assert "'-e', 'TN_VCENTER_PASSWORD'," in argv  # by name, no value
    assert "vault_vsphere_password" not in argv.split("'--update-password'")[0]
    assert "TN_VCENTER_PASSWORD=" not in argv
    assert "scripts/register_vcenter_inprocess.py" in argv and ":/srcapp:ro'" in argv and "'--no-deps'" in argv
    assert "'--admin', tn_bootstrap_admin_upn" in argv
    assert "['--insecure']" in argv and "tn_vsphere_verify_ssl" in argv
    when = " ".join(task["when"].split())
    assert "tn_register_vcenter_connection" in when and "tn_uses_vcenter | default(true) | bool" in when
    assert "tn_vcenter_host | default('') | length > 0" in when
    # Its output is shown by a separate task; the install fails only on rc != 0 (registration).
    assert task["failed_when"] is False
    check = _task("Seed — the vCenter connection is registered")
    assert check["ansible.builtin.assert"]["that"] == "tn_vcenter_conn.rc == 0"


def test_toggle_defaults() -> None:
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert defaults["tn_register_vcenter_connection"] is True
    assert defaults["tn_vcenter_connection_name"] == "Range vCenter"
    assert defaults["tn_vcenter_connection_update_password"] is False


def test_the_script_reads_only_its_password_variable_and_never_takes_it_as_an_argument() -> None:
    # Unlike the content loader (tests/contracts/test_installer_shipped_content.py: no env
    # lookups at all), this script needs one secret. It reads exactly that one variable.
    src = SCRIPT.read_text()
    assert re.findall(r"os\.(?:environ|getenv)\b[^\n]*", src) == ['os.environ.get(PASSWORD_ENV, "")  # the only variable read; never argv, never printed']
    assert 'PASSWORD_ENV = "TN_VCENTER_PASSWORD"' in src
    assert "--password" not in src
    assert "from _inprocess_api import connect" in src and "Authorization" not in src and "KEYCLOAK" not in src
