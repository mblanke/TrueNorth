"""Integration test: range lifecycle against live services.

Create -> provision -> snapshot -> restore -> destroy, driven through the real API.

Every assertion here is against the contract the API actually publishes. The earlier
version of this file drifted off it in three ways at once — it created ranges with a
null `template_id` (which `RangeIn` requires), posted to `/ranges/{id}/snapshot` and
`/ranges/{id}/restore` (the routes are `/snapshots` and
`/snapshots/{snapshot_id}/restore`), and polled for a `provisioned` state that is not in
`RangeState` at all; the terminal state after provisioning is `ready`. None of that
surfaced because the suite was gated behind an env flag nobody set.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 60

# `provisioned` is not a RangeState — see app.models.RangeState.
READY = "ready"


def _poll_range_state(client, range_id: str, target: str) -> dict:
    """Poll GET /ranges/{id} until state == target or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}")
        assert resp.status_code == 200, f"GET /ranges/{range_id} => {resp.status_code}"
        last = resp.json()
        if last["state"] == target:
            return last
        if last["state"] == "failed":
            pytest.fail(f"range {range_id} entered 'failed' while waiting for '{target}'")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Range {range_id} did not reach '{target}' within {POLL_TIMEOUT}s "
        f"(last state: {last['state'] if last else 'unknown'})"
    )


def _settled_state(client, range_id: str, settle: int = 20) -> str:
    """The range's state once an async task has had time to land.

    Returns early on `failed`, which is the outcome a broken worker task produces.
    """
    deadline = time.time() + settle
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}")
        assert resp.status_code == 200
        state = resp.json()["state"]
        if state == "failed":
            return state
        time.sleep(POLL_INTERVAL)
    return state


def _poll_snapshot_ready(client, range_id: str, snapshot_id: str) -> dict:
    """Poll until the snapshot reaches `ready`, which is what restore requires."""
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}/snapshots")
        assert resp.status_code == 200
        for snap in resp.json():
            if snap["id"] == snapshot_id:
                last = snap
                if snap["snapshot_state"] == "ready":
                    return snap
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Snapshot {snapshot_id} not ready within {POLL_TIMEOUT}s "
        f"(last state: {last['snapshot_state'] if last else 'unknown'})"
    )


@pytest.fixture(scope="class")
def lifecycle_range(request, api_client, range_template):
    """One range shared by the ordered steps in this class, destroyed at the end."""
    resp = api_client.post(
        "/ranges", json={"name": "integ-range-lifecycle", "template_id": range_template}
    )
    assert resp.status_code in (200, 201), f"POST /ranges => {resp.status_code} {resp.text}"
    range_id = resp.json()["id"]
    yield range_id
    api_client.delete(f"/ranges/{range_id}")


class TestRangeLifecycle:
    """End-to-end range lifecycle against live services."""

    def test_create_range(self, api_client, lifecycle_range):
        resp = api_client.get(f"/ranges/{lifecycle_range}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == lifecycle_range
        assert data["state"] == "created"
        assert data["template_id"]

    def test_create_range_requires_a_template(self, api_client):
        """`template_id` is required and non-nullable — a range with no template has
        nothing to provision from, so this must be rejected rather than accepted."""
        resp = api_client.post("/ranges", json={"name": "integ-no-template"})
        assert resp.status_code == 422

    def test_provision_range(self, api_client, lifecycle_range):
        resp = api_client.post(f"/ranges/{lifecycle_range}/provision")
        assert resp.status_code in (200, 202), resp.text
        _poll_range_state(api_client, lifecycle_range, READY)

    def test_health_check(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("healthy", "ok")

    def test_snapshot_range(self, api_client, lifecycle_range):
        resp = api_client.post(
            f"/ranges/{lifecycle_range}/snapshots",
            json={"name": "integ-snapshot", "description": "Integration test snapshot"},
        )
        assert resp.status_code in (200, 201, 202), resp.text
        snapshot_id = resp.json()["id"]
        self.__class__._snapshot_id = snapshot_id

        listed = api_client.get(f"/ranges/{lifecycle_range}/snapshots")
        assert listed.status_code == 200
        assert snapshot_id in [s["id"] for s in listed.json()]

    def test_snapshot_requires_a_name(self, api_client, lifecycle_range):
        """An unnamed snapshot cannot be identified later, so `SnapshotIn` requires one."""
        resp = api_client.post(f"/ranges/{lifecycle_range}/snapshots", json={})
        assert resp.status_code == 422

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "restore is broken on the mock backend: worker.tasks.restore_snapshot calls "
            "provisioner.restore(), which MockProvisioner does not implement "
            "(AttributeError) — nor does any other provisioner: vsphere, hyperv, "
            "proxmox and terraform all implement snapshot() and none implement "
            "restore(), so this endpoint cannot succeed on any backend. The retrying "
            "task also stamps 'failed' over whatever terminal state the range reached, "
            "which is why restore runs on a range of its own here."
        ),
    )
    def test_restore_range(self, api_client, make_range, range_template):
        """Restore runs on its own range.

        Sharing the lifecycle range would let the retrying restore task stamp `failed`
        over the `destroyed` state the teardown test asserts — the failure would then
        land on destroy, which is not where the defect is.
        """
        range_id = make_range("integ-range-restore")
        assert api_client.post(f"/ranges/{range_id}/provision").status_code in (200, 202)
        _poll_range_state(api_client, range_id, READY)

        snap = api_client.post(
            f"/ranges/{range_id}/snapshots", json={"name": "integ-restore-snapshot"}
        )
        assert snap.status_code in (200, 201, 202), snap.text
        snapshot_id = snap.json()["id"]
        # Restore refuses a snapshot that is not `ready` (ranges.py:384) — taking one is
        # asynchronous, so the test has to wait for it rather than assume it landed.
        _poll_snapshot_ready(api_client, range_id, snapshot_id)

        resp = api_client.post(f"/ranges/{range_id}/snapshots/{snapshot_id}/restore")
        assert resp.status_code in (200, 202), resp.text
        # Polling for READY alone proves nothing here — the range is already READY, so
        # the assertion passes whether or not the restore ran. Wait for the task to
        # settle and require that it did not fail.
        assert _settled_state(api_client, range_id) == READY

    def test_destroy_range(self, api_client, lifecycle_range):
        resp = api_client.post(f"/ranges/{lifecycle_range}/destroy")
        assert resp.status_code in (200, 202, 204), resp.text
        _poll_range_state(api_client, lifecycle_range, "destroyed")
