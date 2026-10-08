"""Background noise engine: the dial, the planner, and both sides of the router.

What matters most here, in order:

1. Ground truth (which traffic was synthetic) never reaches a student, and never
   crosses tenants.
2. An agent token is scoped to one node in one range and nothing else.
3. The dial means the same thing every time: monotonic, deterministic per seed.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.noise import dial, planner
from app.noise.roster import builtin_roster

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
TARGETS = {
    "web": ["intranet.corp.local"],
    "dns": ["intranet.corp.local", "mail.corp.local"],
    "mail": ["mail.corp.local"],
    "share": ["files.corp.local"],
    "dc": ["dc01.corp.local"],
    "ssh": ["web01.corp.local"],
    "ntp": ["dc01.corp.local"],
    "subnet": ["10.10.20.0/24"],
    "hosts": ["ws01.corp.local"],
}


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _range(db, tenant=DEV_TENANT, name="Noisy Range"):
    from app.models import Range, Template

    tpl = Template(name=f"{name}-tpl", version="1.0", yaml="nodes: []", tenant_id=tenant)
    db.add(tpl)
    db.flush()
    r = Range(name=name, template_id=tpl.id, tenant_id=tenant, state="ready")
    db.add(r)
    db.commit()
    return r


@pytest.fixture
def rng(db_session):
    return _range(db_session)


def _setup(client, rng, nodes=("ws01", "ws02"), level=70, count=12):
    r = client.put(f"/noise/ranges/{rng.id}", json={"enabled": True, "level": level, "targets": TARGETS})
    assert r.status_code == 200, r.text
    agents = client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": n} for n in nodes]})
    assert agents.status_code == 201, agents.text
    roster = client.post(f"/noise/ranges/{rng.id}/personas/roster", json={"count": count})
    assert roster.status_code == 201, roster.text
    return {a["node"]: a["token"] for a in agents.json()}


# ── Dial ───────────────────────────────────────────────────────────────
class TestDial:
    def test_zero_is_silence(self):
        s = dial.settings_for(0)
        assert s.active_fraction == 0 and s.actions_per_hour == 0 and s.lookalike_share == 0

    def test_every_knob_is_monotonic(self):
        prev = dial.settings_for(1)
        for lv in range(2, 101):
            s = dial.settings_for(lv)
            assert s.active_fraction >= prev.active_fraction
            assert s.actions_per_hour > prev.actions_per_hour
            assert s.diurnal_amplitude <= prev.diurnal_amplitude
            assert s.lookalike_share >= prev.lookalike_share
            prev = s

    def test_no_lookalikes_on_a_quiet_range(self):
        assert dial.settings_for(dial.LOOKALIKE_FLOOR).lookalike_share == 0
        assert dial.settings_for(dial.LOOKALIKE_FLOOR + 1).lookalike_share > 0

    def test_level_is_clamped(self):
        assert dial.settings_for(250).level == 100
        assert dial.settings_for(-5).level == 0

    def test_override_precedence_and_pause(self):
        ov = {"zone:dmz": 80, "node:dc01": 20}
        assert dial.resolve_level(40, ov, node="web01", zone="dmz") == 80
        assert dial.resolve_level(40, ov, node="dc01", zone="dmz") == 20
        assert dial.resolve_level(40, ov, node="ws01", zone="lan") == 40
        assert dial.resolve_level(40, ov, node="dc01", paused=True) == 0
        assert dial.resolve_level(40, ov, node="dc01", enabled=False) == 0

    def test_work_day_curve(self):
        assert dial.diurnal(10, 8, 17, 0.8) == 1.0
        assert dial.diurnal(12.5, 8, 17, 0.8) == 0.5
        assert dial.diurnal(3, 8, 17, 0.8) == pytest.approx(0.2)
        assert dial.diurnal(23, 22, 6, 0.8) == 1.0  # night shift wraps midnight


# ── Planner ────────────────────────────────────────────────────────────
def _personas(n=20, lookalikes=True):
    return [
        planner.PersonaSpec(handle=f"u{i}", node="ws01", lookalikes=lookalikes and i % 4 == 0, contacts=("a@x",))
        for i in range(n)
    ]


MONDAY_10AM = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


class TestPlanner:
    def test_same_inputs_same_plan(self):
        ctx = planner.PlanContext(seed=7, level=60, targets=TARGETS)
        a = planner.plan(ctx, _personas(), MONDAY_10AM, 30)
        b = planner.plan(ctx, _personas(), MONDAY_10AM, 30)
        assert a == b and a

    def test_overlapping_fetches_agree(self):
        """An agent that re-fetches mid-window must be told the same thing."""
        ctx = planner.PlanContext(seed=7, level=60, targets=TARGETS)
        whole = planner.plan(ctx, _personas(), MONDAY_10AM, 20)
        later = planner.plan(ctx, _personas(), MONDAY_10AM + timedelta(minutes=7), 13)
        assert later == [a for a in whole if a["at"] >= (MONDAY_10AM + timedelta(minutes=7)).isoformat()]

    def test_different_seed_different_plan(self):
        a = planner.plan(planner.PlanContext(seed=1, level=60, targets=TARGETS), _personas(), MONDAY_10AM, 30)
        b = planner.plan(planner.PlanContext(seed=2, level=60, targets=TARGETS), _personas(), MONDAY_10AM, 30)
        assert a != b

    def test_volume_tracks_the_dial(self):
        counts = [
            len(planner.plan(planner.PlanContext(seed=3, level=lv, targets=TARGETS), _personas(40), MONDAY_10AM, 60))
            for lv in (10, 40, 70, 95)
        ]
        assert counts == sorted(counts) and counts[0] < counts[-1] / 4, counts

    def test_level_zero_plans_nothing(self):
        assert planner.plan(planner.PlanContext(seed=3, level=0, targets=TARGETS), _personas(), MONDAY_10AM, 60) == []

    def test_after_hours_is_quieter(self):
        ctx = planner.PlanContext(seed=3, level=40, targets=TARGETS)
        day = len(planner.plan(ctx, _personas(40), MONDAY_10AM, 60))
        night = len(planner.plan(ctx, _personas(40), MONDAY_10AM.replace(hour=2), 60))
        assert night < day / 2

    def test_raising_the_dial_only_adds_people(self):
        low = {p.handle for p in _personas(50) if planner.is_active(9, p.handle, dial.settings_for(30).active_fraction)}
        high = {
            p.handle for p in _personas(50) if planner.is_active(9, p.handle, dial.settings_for(80).active_fraction)
        }
        assert low < high

    def test_only_flagged_personas_do_lookalikes(self):
        acts = planner.plan(planner.PlanContext(seed=5, level=95, targets=TARGETS), _personas(40), MONDAY_10AM, 60)
        flagged = {p.handle for p in _personas(40) if p.lookalikes}
        looks = [a for a in acts if a["lookalike"]]
        assert looks, "level 95 with flagged personas should produce some lookalikes"
        assert {a["persona"] for a in looks} <= flagged
        assert all(a["kind"] in dial.LOOKALIKES for a in looks)

    def test_no_target_no_action(self):
        acts = planner.plan(planner.PlanContext(seed=5, level=95, targets={"web": ["w"]}), _personas(), MONDAY_10AM, 60)
        assert acts and {a["kind"] for a in acts} == {"web_browse"}

    def test_roster_is_deterministic_and_unique(self):
        a = builtin_roster(60, ["ws01", "ws02"], seed=4)
        assert a == builtin_roster(60, ["ws01", "ws02"], seed=4)
        assert len({p["handle"] for p in a}) == 60
        assert {p["node"] for p in a} == {"ws01", "ws02"}
        assert any(p["lookalikes"] for p in a) and not all(p["lookalikes"] for p in a)


# ── White-cell API ─────────────────────────────────────────────────────
class TestWhiteCell:
    def test_unconfigured_range_reads_as_defaults(self, client, rng):
        body = client.get(f"/noise/ranges/{rng.id}").json()
        assert body["configured"] is False and body["enabled"] is False and body["effective_level"] == 0

    def test_preset_sets_level(self, client, rng):
        body = client.put(f"/noise/ranges/{rng.id}", json={"preset": "busy", "enabled": True}).json()
        assert body["level"] == dial.PRESETS["busy"] and body["effective_level"] == dial.PRESETS["busy"]

    def test_pause_silences_without_losing_the_level(self, client, rng):
        client.put(f"/noise/ranges/{rng.id}", json={"level": 70, "enabled": True})
        body = client.put(f"/noise/ranges/{rng.id}", json={"paused": True}).json()
        assert body["level"] == 70 and body["effective_level"] == 0

    @pytest.mark.parametrize(
        "payload",
        [{"preset": "deafening"}, {"level": 101}, {"overrides": {"dmz": 50}}, {"targets": {"printers": ["p1"]}}],
    )
    def test_bad_input_is_rejected(self, client, rng, payload):
        assert client.put(f"/noise/ranges/{rng.id}", json=payload).status_code == 422

    def test_tokens_are_shown_once(self, client, rng):
        tokens = _setup(client, rng)
        listed = client.get(f"/noise/ranges/{rng.id}/agents").json()
        assert {a["node"] for a in listed} == set(tokens)
        assert all("token" not in a and a["state"] == "pending" for a in listed)

    def test_roster_needs_agents_and_refuses_to_clobber(self, client, rng):
        client.put(f"/noise/ranges/{rng.id}", json={"enabled": True})
        assert client.post(f"/noise/ranges/{rng.id}/personas/roster", json={"count": 3}).status_code == 409
        _setup(client, rng)
        assert client.post(f"/noise/ranges/{rng.id}/personas/roster", json={"count": 3}).status_code == 409
        again = client.post(f"/noise/ranges/{rng.id}/personas/roster", json={"count": 3, "replace": True})
        assert again.status_code == 201 and len(client.get(f"/noise/ranges/{rng.id}/personas").json()) == 3

    def test_plan_preview_matches_what_the_agent_gets(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        preview = client.get(f"/noise/ranges/{rng.id}/plan", params={"node": "ws01", "minutes": 10}).json()
        got = client.get("/noise/agent/plan", params={"minutes": 10}, headers={"X-Noise-Agent-Token": tokens["ws01"]})
        assert got.status_code == 200
        # Same deterministic plan; the agent's request lands a moment later, so compare
        # only the span both requests cover.
        seen, agent_actions = preview["actions"], got.json()["actions"]
        assert seen and agent_actions

        def key(a):
            return (a["at"], a["persona"], a["kind"], a["target"])

        overlap = [key(a) for a in agent_actions if a["at"] <= seen[-1]["at"]]
        assert overlap and set(overlap) <= {key(a) for a in seen}

    def test_duplicate_nodes_in_one_request_are_rejected(self, client, rng):
        r = client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": "a"}, {"node": "a"}]})
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "pool,value",
        [
            ("web", "http://169.254.169.254/latest/meta-data/"),
            ("web", "https://user:pw@intranet.corp.local/"),
            ("web", "file:///etc/passwd"),
            ("web", "localhost"),
            ("dns", "127.0.0.1"),
            ("subnet", "0.0.0.0/0"),
            ("subnet", "10.0.0.0/8"),
            ("subnet", "::/0"),
            ("mail", "not a host!"),
        ],
    )
    def test_targets_that_point_off_range_are_refused(self, client, rng, pool, value):
        assert client.put(f"/noise/ranges/{rng.id}", json={"targets": {pool: [value]}}).status_code == 422

    def test_too_many_targets_are_refused(self, client, rng):
        hosts = [f"h{i}.corp.local" for i in range(65)]
        assert client.put(f"/noise/ranges/{rng.id}", json={"targets": {"web": hosts}}).status_code == 422

    def test_revoke_agent(self, client, rng):
        _setup(client, rng)
        aid = client.get(f"/noise/ranges/{rng.id}/agents").json()[0]["id"]
        assert client.delete(f"/noise/ranges/{rng.id}/agents/{aid}").status_code == 204
        assert client.delete(f"/noise/ranges/{rng.id}/agents/{aid}").status_code == 404
        assert client.delete(f"/noise/ranges/{rng.id}/agents/not-a-uuid").status_code == 404


# ── Agent channel ──────────────────────────────────────────────────────
def _hdr(token):
    return {"X-Noise-Agent-Token": token}


class TestAgentChannel:
    def test_no_or_bad_token_is_refused(self, client, rng):
        _setup(client, rng)
        assert client.get("/noise/agent/plan").status_code == 422  # header required
        assert client.get("/noise/agent/plan", headers=_hdr("nope")).status_code == 401
        assert client.post("/noise/agent/report", json={"results": []}, headers=_hdr("nope")).status_code == 401

    def test_plan_fetch_is_the_heartbeat(self, client, rng):
        tokens = _setup(client, rng)
        client.get("/noise/agent/plan", headers=_hdr(tokens["ws01"]))
        states = {a["node"]: a["state"] for a in client.get(f"/noise/ranges/{rng.id}/agents").json()}
        assert states == {"ws01": "ok", "ws02": "pending"}

    def test_silent_agent_goes_lost(self, client, rng, db_session):
        from app.noise.models import NoiseAgent

        tokens = _setup(client, rng)
        client.get("/noise/agent/plan", headers=_hdr(tokens["ws01"]))
        agent = db_session.query(NoiseAgent).filter_by(node="ws01").one()
        agent.last_seen_at = datetime.now(UTC) - timedelta(minutes=30)
        db_session.commit()
        states = {a["node"]: a["state"] for a in client.get(f"/noise/ranges/{rng.id}/agents").json()}
        assert states["ws01"] == "lost"

    def test_token_only_ever_plans_its_own_node(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        body = client.get("/noise/agent/plan", params={"minutes": 30}, headers=_hdr(tokens["ws02"])).json()
        assert body["node"] == "ws02"
        personas = {p["handle"]: p["node"] for p in client.get(f"/noise/ranges/{rng.id}/personas").json()}
        assert body["actions"] and all(personas[a["persona"]] == "ws02" for a in body["actions"])

    def test_rekeying_revokes_the_old_token(self, client, rng):
        tokens = _setup(client, rng)
        client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": "ws01"}]})
        assert client.get("/noise/agent/plan", headers=_hdr(tokens["ws01"])).status_code == 401

    def _signed(self, client, token, minutes=60):
        body = client.get("/noise/agent/plan", params={"minutes": minutes}, headers=_hdr(token)).json()
        assert body["actions"], "need a non-empty plan to report against"
        return body["actions"]

    def test_plan_is_signed_and_hides_lookalikes(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        actions = self._signed(client, tokens["ws01"])
        assert all(len(a["sig"]) == 64 and "lookalike" not in a for a in actions)

    def test_signed_report_lands_in_ground_truth_and_stats(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        actions = self._signed(client, tokens["ws01"])[:5]
        results = [{**a, "ok": i != 0, "detail": {"pages": 2}} for i, a in enumerate(actions)]
        r = client.post(
            "/noise/agent/report", json={"version": "0.1.0", "results": results}, headers=_hdr(tokens["ws01"])
        )
        assert r.status_code == 200 and r.json() == {"accepted": 5, "rejected": 0, "duplicate": 0}
        truth = client.get(f"/noise/ranges/{rng.id}/activity").json()
        assert len(truth) == 5 and {t["node"] for t in truth} == {"ws01"}
        assert all(t["lookalike"] == (t["kind"] in dial.LOOKALIKES) for t in truth)
        stats = client.get(f"/noise/ranges/{rng.id}/stats").json()
        assert stats["total"] == 5 and stats["failed"] == 1
        agents = client.get(f"/noise/ranges/{rng.id}/agents").json()
        assert next(a for a in agents if a["node"] == "ws01")["version"] == "0.1.0"

    @pytest.mark.parametrize(
        "tamper",
        [
            {"target": "10.99.99.99"},  # the attacker's C2, dressed as noise
            {"persona": "not-a-persona"},
            {"kind": "admin_scan"},  # promote a benign action to a lookalike
            {"at": "2036-01-01T00:00:00+00:00"},  # pin it to the top of the feed
            {"sig": "0" * 64},
        ],
    )
    def test_a_stolen_token_cannot_forge_ground_truth(self, client, rng, tamper):
        tokens = _setup(client, rng, level=95, count=30)
        forged = {**self._signed(client, tokens["ws01"])[0], **tamper}
        r = client.post("/noise/agent/report", json={"results": [forged]}, headers=_hdr(tokens["ws01"]))
        assert r.status_code == 200 and r.json()["rejected"] == 1
        assert client.get(f"/noise/ranges/{rng.id}/activity").json() == []

    def test_another_nodes_plan_cannot_be_reported(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        theirs = self._signed(client, tokens["ws02"])[0]
        r = client.post("/noise/agent/report", json={"results": [theirs]}, headers=_hdr(tokens["ws01"]))
        assert r.json()["rejected"] == 1

    def test_replays_are_recorded_once(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        a = self._signed(client, tokens["ws01"])[0]
        first = client.post("/noise/agent/report", json={"results": [a, a]}, headers=_hdr(tokens["ws01"])).json()
        again = client.post("/noise/agent/report", json={"results": [a]}, headers=_hdr(tokens["ws01"])).json()
        assert first == {"accepted": 1, "rejected": 0, "duplicate": 1}
        assert again == {"accepted": 0, "rejected": 0, "duplicate": 1}
        assert len(client.get(f"/noise/ranges/{rng.id}/activity").json()) == 1

    def test_oversized_detail_is_truncated(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        a = {**self._signed(client, tokens["ws01"])[0], "detail": {"blob": "x" * 50_000}}
        client.post("/noise/agent/report", json={"results": [a]}, headers=_hdr(tokens["ws01"]))
        detail = client.get(f"/noise/ranges/{rng.id}/activity").json()[0]["detail"]
        assert detail["truncated"] is True and "blob" not in detail

    def test_revoked_agent_keeps_its_ground_truth(self, client, rng):
        tokens = _setup(client, rng, level=95, count=30)
        a = self._signed(client, tokens["ws01"])[0]
        client.post("/noise/agent/report", json={"results": [a]}, headers=_hdr(tokens["ws01"]))
        aid = next(x["id"] for x in client.get(f"/noise/ranges/{rng.id}/agents").json() if x["node"] == "ws01")
        client.delete(f"/noise/ranges/{rng.id}/agents/{aid}")
        assert client.get("/noise/agent/plan", headers=_hdr(tokens["ws01"])).status_code == 401
        assert len(client.get(f"/noise/ranges/{rng.id}/activity").json()) == 1
        # Re-registering the node brings it back with a fresh token.
        fresh = client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": "ws01"}]}).json()[0]
        assert client.get("/noise/agent/plan", headers=_hdr(fresh["token"])).status_code == 200

    def test_token_dies_with_its_range(self, client, rng, db_session):
        tokens = _setup(client, rng)
        rng.deleted_at = datetime.now(UTC)
        db_session.commit()
        assert client.get("/noise/agent/plan", headers=_hdr(tokens["ws01"])).status_code == 401

    def test_management_network_restriction(self, client, rng, monkeypatch):
        tokens = _setup(client, rng)
        monkeypatch.setenv("NOISE_AGENT_CIDRS", "10.255.0.0/24")
        outside = {**_hdr(tokens["ws01"]), "X-Forwarded-For": "10.10.20.5"}
        inside = {**_hdr(tokens["ws01"]), "X-Forwarded-For": "10.255.0.9"}
        # Through nginx (a trusted proxy on loopback), the forwarded address decides.
        from fastapi.testclient import TestClient

        via_proxy = TestClient(client.app, client=("127.0.0.1", 40000))
        assert via_proxy.get("/noise/agent/plan", headers=outside).status_code == 403
        assert via_proxy.get("/noise/agent/plan", headers=inside).status_code == 200
        # Straight from an untrusted peer, a forwarded header claiming to be inside is ignored.
        assert client.get("/noise/agent/plan", headers=inside).status_code == 403
        direct = TestClient(client.app, client=("10.255.0.7", 40000))
        assert direct.get("/noise/agent/plan", headers=_hdr(tokens["ws01"])).status_code == 200


# ── Who may see it ─────────────────────────────────────────────────────
class TestAccess:
    @pytest.mark.parametrize("role", [UserRole.student, UserRole.observer])
    def test_students_and_observers_never_see_ground_truth(self, client, rng, role):
        _setup(client, rng)
        with acting_as(role):
            for path in ("", "/agents", "/personas", "/activity", "/stats"):
                assert client.get(f"/noise/ranges/{rng.id}{path}").status_code == 403, path
            assert client.put(f"/noise/ranges/{rng.id}", json={"level": 0}).status_code == 403

    def test_range_ops_cannot_read_ground_truth(self, client, rng):
        _setup(client, rng)
        with acting_as(UserRole.range_ops):
            assert client.get(f"/noise/ranges/{rng.id}/activity").status_code == 403

    def test_instructor_runs_the_dial(self, client, rng):
        with acting_as(UserRole.instructor):
            assert client.put(f"/noise/ranges/{rng.id}", json={"level": 55}).status_code == 200

    def test_another_tenants_range_is_404(self, client, db_session):
        foreign = _range(db_session, tenant=OTHER_TENANT, name="Foreign")
        with acting_as(UserRole.instructor):
            assert client.get(f"/noise/ranges/{foreign.id}").status_code == 404
            assert client.put(f"/noise/ranges/{foreign.id}", json={"level": 1}).status_code == 404
            assert client.get(f"/noise/ranges/{foreign.id}/activity").status_code == 404
