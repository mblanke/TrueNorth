"""The vSphere console reaches the range's own vCenter, with that connection's login, and
only for the range's own VM (#131 review 2, should-fix 3).

It used to log in with VSPHERE_* always and ask for a WebMKS ticket on the bare recorded
``vm-NN``: on a tenant's range, a screen on some other vCenter's VM.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app import models as m
from app.console_backends import ConsoleError
from app.console_backends import vsphere_webmks as console_mod
from app.models import HypervisorConnection
from app.secretbox import seal

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER = "99999999-0000-0000-0000-000000000000"


class FakeMachine:
    def __init__(self, name, annotation):
        self.name = name
        self.config = SimpleNamespace(annotation=annotation)
        self.AcquireTicket = MagicMock(return_value=SimpleNamespace(host="esx1.test", port=443, ticket="T"))


@pytest.fixture
def vcenter(monkeypatch):
    state = SimpleNamespace(machines={}, connect=MagicMock(return_value=SimpleNamespace(_stub=None)))
    monkeypatch.setattr(console_mod, "SmartConnect", state.connect)
    monkeypatch.setattr(console_mod, "Disconnect", MagicMock())
    monkeypatch.setattr(console_mod, "vim", SimpleNamespace(VirtualMachine=lambda vm_id, stub: state.machines[vm_id]))
    monkeypatch.setenv("VSPHERE_URL", "https://vc-platform.test")
    monkeypatch.setenv("VSPHERE_USERNAME", "platform-svc")
    monkeypatch.setenv("VSPHERE_PASSWORD", "platform-secret")
    return state


def _conn(db, host="vc-a.test", password="pw-a", tenant=TENANT, **kw):
    conn = HypervisorConnection(id=uuid.uuid4(), tenant_id=tenant, name=host, hypervisor_type="vsphere", host=host,
                                port=443, username="svc-a", password_encrypted=seal(password) if password else None,
                                is_active=True, **kw)
    db.add(conn)
    db.flush()
    return conn


def _range(db, output, tenant=TENANT):
    tmpl = m.Template(name=f"t-{uuid.uuid4().hex[:6]}", yaml="vms: []", tenant_id=tenant)
    db.add(tmpl)
    db.flush()
    rng = m.Range(name="r", template_id=tmpl.id, tenant_id=tenant, provisioner_backend="vsphere_api",
                  state=m.RangeState.ready, provisioner_output=json.dumps(output))
    db.add(rng)
    db.flush()
    return rng


def _tag(rid):
    return f"TrueNorth range VM. Deleted with its range. truenorth-range:{rid}"


def _open(db, rng, vm):
    return console_mod.VsphereWebMKSConsole().open(vm, db=db, rng=rng)


VM = {"name": "abcdef12-web", "vm_id": "vm-1"}


def test_the_recorded_connection_and_its_own_login_are_used(db_session, vcenter):
    conn = _conn(db_session)
    rng = _range(db_session, {"vms": [VM], "connection": {"id": str(conn.id), "host": "https://vc-a.test"}})
    vcenter.machines["vm-1"] = FakeMachine("x-web", _tag(rng.id))
    out = _open(db_session, rng, VM)
    assert out["url"] == "wss://esx1.test:443/ticket/T"
    kw = vcenter.connect.call_args.kwargs
    assert (kw["host"], kw["user"], kw["pwd"]) == ("vc-a.test", "svc-a", "pw-a")


def test_another_ranges_vm_under_the_recorded_id_gets_no_ticket(db_session, vcenter):
    conn = _conn(db_session)
    rng = _range(db_session, {"vms": [VM], "connection": {"id": str(conn.id), "host": "https://vc-a.test"}})
    vcenter.machines["vm-1"] = FakeMachine("prod-dc", _tag(OTHER))
    with pytest.raises(ConsoleError, match="not this range's"):
        _open(db_session, rng, VM)
    vcenter.machines["vm-1"].AcquireTicket.assert_not_called()


def test_an_untagged_vm_needs_its_recorded_range_name(db_session, vcenter):
    rng = _range(db_session, {"vms": [VM]})
    vm = {"name": f"{str(rng.id)[:8]}-web", "vm_id": "vm-1"}
    vcenter.machines["vm-1"] = FakeMachine(vm["name"], None)
    assert _open(db_session, rng, vm)["kind"] == "webmks"
    vcenter.machines["vm-1"] = FakeMachine("finance-db", None)
    with pytest.raises(ConsoleError):
        _open(db_session, rng, vm)


@pytest.mark.parametrize("change", ["deleted", "repointed"])
def test_a_recorded_connection_gone_or_repointed_fails_closed(db_session, vcenter, change):
    conn = _conn(db_session)
    rng = _range(db_session, {"vms": [VM], "connection": {"id": str(conn.id), "host": "https://vc-a.test"}})
    _conn(db_session, host="vc-other.test", is_primary=True)  # there IS another: it must not be used
    if change == "deleted":
        db_session.delete(conn)
    else:
        conn.host = "vc-elsewhere.test"
    db_session.flush()
    with pytest.raises(ConsoleError):
        _open(db_session, rng, VM)
    vcenter.connect.assert_not_called()


def test_a_connection_without_a_password_never_gets_the_platforms(db_session, vcenter):
    conn = _conn(db_session, host="attacker.example", password=None)
    rng = _range(db_session, {"vms": [VM], "connection": {"id": str(conn.id), "host": "https://attacker.example"}})
    with pytest.raises(ConsoleError, match="password"):
        _open(db_session, rng, VM)
    vcenter.connect.assert_not_called()


def test_an_unrecorded_range_uses_its_tenants_connection_else_the_platforms(db_session, vcenter):
    rng = _range(db_session, {"vms": [VM]})
    vcenter.machines["vm-1"] = FakeMachine("x", _tag(rng.id))
    _open(db_session, rng, VM)
    assert vcenter.connect.call_args.kwargs["host"] == "vc-platform.test"
    _conn(db_session, host="vc-t.test")
    _open(db_session, rng, VM)
    assert vcenter.connect.call_args.kwargs["host"] == "vc-t.test"


def test_another_tenants_connection_is_never_used(db_session, vcenter):
    other_tenant = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
    db_session.add(m.Tenant(id=other_tenant, name="o", slug=f"o-{uuid.uuid4().hex[:6]}"))
    db_session.flush()
    theirs = _conn(db_session, host="vc-theirs.test", tenant=other_tenant)
    rng = _range(db_session, {"vms": [VM], "connection": {"id": str(theirs.id), "host": "https://vc-theirs.test"}})
    with pytest.raises(ConsoleError):
        _open(db_session, rng, VM)
