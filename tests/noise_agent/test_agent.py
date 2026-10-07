"""The noise agent against the real controller API, and its activities against localhost.

The agent loop is driven with a fake clock and sleep so a "minute" of noise runs in
milliseconds; the controller is the real FastAPI app through TestClient.
"""

import http.server
import socket
import threading
import time

import pytest
from tn_noise_agent import VERSION
from tn_noise_agent.activities import (
    AgentConfig,
    TargetRefusedError,
    admin_scan,
    dns_lookup,
    guard,
    ssh_admin,
    web_browse,
)
from tn_noise_agent.runner import Runner

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


class FakeClock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class TestClientController:
    """ControllerClient's interface, over FastAPI's TestClient."""

    def __init__(self, client, token):
        self.client, self.headers = client, {"X-Noise-Agent-Token": token}
        self.reports = []

    def plan(self, minutes):
        r = self.client.get("/noise/agent/plan", params={"minutes": minutes}, headers=self.headers)
        r.raise_for_status()
        self.last = r.json()
        return r.json()

    def report(self, version, results):
        self.reports.append(results)
        r = self.client.post("/noise/agent/report", json={"version": version, "results": results}, headers=self.headers)
        r.raise_for_status()
        self.last = r.json()
        return r.json()


@pytest.fixture
def token(client, db_session):
    from app.models import Range, Template

    tpl = Template(name="agent-tpl", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="Agent Range", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(rng)
    db_session.commit()
    targets = {"web": ["intranet.corp.local"], "dns": ["intranet.corp.local"], "mail": ["mail.corp.local"]}
    client.put(f"/noise/ranges/{rng.id}", json={"enabled": True, "level": 100, "targets": targets})
    agents = client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": "ws01"}]}).json()
    client.post(f"/noise/ranges/{rng.id}/personas/roster", json={"count": 40})
    client.rng_id = rng.id
    return agents[0]["token"]


class TestLoop:
    def test_a_cycle_runs_the_plan_and_reports_it(self, client, token):
        clock = FakeClock()
        ctl = TestClientController(client, token)
        runner = Runner(ctl, dry=True, clock=clock, sleep=clock.sleep)
        runner.cycle()
        done = [r for batch in ctl.reports for r in batch]
        assert done, "level 100 with 40 personas should keep one node busy within a minute"
        assert all(r["ok"] and r["detail"] == {"dry_run": True} for r in done)
        assert ctl.last == {"accepted": len(done), "rejected": 0, "duplicate": 0}
        truth = client.get(f"/noise/ranges/{client.rng_id}/activity", params={"limit": 1000}).json()
        assert len(truth) == len(done)
        agent = client.get(f"/noise/ranges/{client.rng_id}/agents").json()[0]
        assert agent["state"] == "ok" and agent["version"] == VERSION

    def test_actions_happen_on_time_not_all_at_once(self, client, token):
        clock = FakeClock()
        start = clock.t
        ctl = TestClientController(client, token)
        Runner(ctl, dry=True, clock=clock, sleep=clock.sleep).cycle()
        stamps = [r["at"] for batch in ctl.reports for r in batch]
        assert stamps == sorted(stamps)
        assert clock.t > start  # it waited for actions to fall due

    def test_overlapping_plans_never_repeat_an_action(self, client, token):
        clock = FakeClock()
        ctl = TestClientController(client, token)
        runner = Runner(ctl, dry=True, clock=clock, sleep=clock.sleep)
        runner.cycle()
        first = sum(len(b) for b in ctl.reports)
        clock.t -= 60  # pretend the next poll re-fetches the same span
        runner.cycle()
        assert sum(len(b) for b in ctl.reports) == first

    def test_a_failing_activity_is_a_result_not_a_crash(self):
        class OneAction:
            def __init__(self):
                self.reports = []

            def plan(self, minutes):
                from datetime import UTC, datetime

                at = datetime.now(UTC).isoformat()
                return {
                    "poll_seconds": 60,
                    "actions": [
                        {"at": at, "persona": "p", "kind": "dns_lookup", "target": "no-such-host.invalid"},
                        {"at": at, "persona": "p", "kind": "teleport", "target": "x"},
                    ],
                }

            def report(self, version, results):
                self.reports.append(results)

        ctl = OneAction()
        Runner(ctl, sleep=lambda s: None).cycle()
        results = ctl.reports[0]
        assert [r["ok"] for r in results] == [False, False]
        assert "unsupported" in results[1]["detail"]["error"]


# ── Real activities against loopback ───────────────────────────────────
CFG = AgentConfig(timeout=2.0)


@pytest.fixture
def intranet():
    pages = {
        "/": b'<a href="/news">News</a> <a href="/staff">Staff</a>',
        "/news": b"news",
        "/staff": b"staff",
    }
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = pages.get(self.path, b"")
            self.send_response(200 if self.path in pages else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


def test_web_browse_follows_links(intranet):
    host, hits = intranet
    assert web_browse({"target": host}, CFG) == {"pages": 3}
    assert hits[0] == "/" and set(hits[1:]) == {"/news", "/staff"}


def test_dns_lookup_resolves():
    assert "127.0.0.1" in dns_lookup({"target": "localhost"}, CFG)["addresses"]


def test_ssh_admin_reads_the_banner():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    port = srv.getsockname()[1]
    import tn_noise_agent.activities as acts

    real = acts._tcp
    acts._tcp = lambda host, _port, timeout, read=False: real(host, port, timeout, read=read)
    try:
        assert ssh_admin({"target": "127.0.0.1"}, CFG)["banner"].startswith("SSH-2.0")
    finally:
        acts._tcp = real
        srv.close()


def test_admin_scan_stays_inside_its_bounds():
    cfg = AgentConfig(max_scan_hosts=3, scan_ports=(1,), allow_loopback=True)
    assert admin_scan({"target": "127.0.0.0/24"}, cfg)["hosts"] == 3


def test_admin_scan_of_a_huge_network_is_cheap():
    """It takes the first few hosts lazily rather than listing a /8 first."""
    cfg = AgentConfig(max_scan_hosts=2, scan_ports=(1,), allow_loopback=True)
    started = time.monotonic()
    assert admin_scan({"target": "127.0.0.0/8"}, cfg)["hosts"] == 2
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("target", ["169.254.169.254", "127.0.0.1", "controller.mgmt"])
def test_runner_refuses_off_range_targets(target):
    class Ctl:
        reports = []

        def plan(self, minutes):
            from datetime import UTC, datetime

            at = datetime.now(UTC).isoformat()
            return {"poll_seconds": 60, "actions": [{"at": at, "persona": "p", "kind": "ssh_admin", "target": target}]}

        def report(self, version, results):
            self.reports.append(results)

    ctl = Ctl()
    Runner(ctl, AgentConfig(forbidden_hosts=frozenset({"controller.mgmt"})), sleep=lambda s: None).cycle()
    result = ctl.reports[0][0]
    assert result["ok"] is False and "TargetRefusedError" in result["detail"]["error"]


def test_guard_refuses_the_management_network():
    with pytest.raises(TargetRefusedError):
        guard("10.255.0.7", AgentConfig(forbidden_nets=("10.255.0.0/24",)))
