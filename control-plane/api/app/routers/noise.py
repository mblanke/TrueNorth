"""Background noise router — the white cell's dial, and the agents' control channel.

Two audiences, two auth schemes:

- ``/noise/ranges/{range_id}/...`` is for the white cell (NOISE_READ / NOISE_CONTROL).
  It sets the level, registers agents, manages personas, and reads ground truth.
- ``/noise/agent/...`` is for the in-VM agents. They hold no user account; each
  authenticates with its own token (``X-Noise-Agent-Token``), which is scoped to one
  node in one range. We keep only the token's SHA-256.

Agents reach this over the management network only. Nothing here is meant to be
reachable from a training VLAN, and nothing here is visible to students.

The agent token is the weakest link: it sits on a VM that students attack. So it buys
as little as possible. Every action in a plan carries an HMAC under a per-range key the
agent never sees, and a report is only recorded if it echoes a valid one: a stolen token
can replay the plan back, but cannot get its own traffic labelled as synthetic. Plans
carry no ``lookalike`` flag; the controller derives it from the activity kind.

Set ``NOISE_AGENT_CIDRS`` (comma-separated) to refuse agent calls from anywhere but the
management network; nginx should enforce the same at ``/api/noise/agent/``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser, client_ip
from ..db import get_db
from ..models import AuditLog, Range, RangeState
from ..noise import dial, planner, targets, topology
from ..noise.models import NoiseActivity, NoiseAgent, NoisePersona, NoiseProfile
from ..noise.roster import builtin_roster
from ..rbac import Permission, require_permission
from ..tenancy import get_owned

logger = logging.getLogger("truenorth.api.noise")

router = APIRouter(prefix="/noise", tags=["noise"])

POLL_SECONDS = 60
LOST_AFTER = timedelta(seconds=POLL_SECONDS * 5)
MAX_REPORT = 500
MAX_DETAIL_BYTES = 2048

_read = require_permission(Permission.NOISE_READ)
_control = require_permission(Permission.NOISE_CONTROL)


# ── Schemas ────────────────────────────────────────────────────────────
class ProfileIn(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    level: int | None = Field(None, ge=0, le=100)
    preset: str | None = None
    seed: int | None = None
    utc_offset: int | None = Field(None, ge=-12, le=14)
    overrides: dict[str, int] | None = Field(None, max_length=500)
    targets: dict[str, list[str]] | None = Field(None, max_length=32)


class AgentIn(BaseModel):
    node: str = Field(..., min_length=1, max_length=255)
    zone: str = Field("", max_length=120)


class AgentsIn(BaseModel):
    agents: list[AgentIn] = Field(..., min_length=1, max_length=500)


class RosterIn(BaseModel):
    count: int = Field(20, ge=1, le=2000)
    domain: str = Field("corp.local", min_length=1, max_length=120)
    replace: bool = False


class DeployIn(BaseModel):
    dry_run: bool = False
    # Replace the target pools with what the template implies (default), or keep any
    # the white cell has hand-edited.
    refresh_targets: bool = True


class ActivityReport(BaseModel):
    """One executed action, echoing the planned fields and their signature verbatim."""

    at: str = Field(..., max_length=40)  # the planned time, exactly as the plan gave it
    persona: str = Field("", max_length=64)
    kind: str = Field(..., max_length=64)
    target: str = Field("", max_length=512)
    sig: str = Field(..., max_length=64)
    ok: bool = True
    ran_at: datetime | None = None
    detail: dict = Field(default_factory=dict)


class ReportIn(BaseModel):
    version: str = Field("", max_length=40)
    results: list[ActivityReport] = Field(default_factory=list, max_length=MAX_REPORT)


# ── Helpers ────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sign(key: str, node: str, a: dict) -> str:
    msg = "|".join((node, a["at"], a.get("persona", ""), a["kind"], a.get("target", "")))
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _audit(db: Session, user: CurrentUser, action: str, range_id: uuid.UUID, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id), action=action, resource_type="range", resource_id=str(range_id), detail=detail
        )
    )


def _small(detail: dict) -> dict:
    """Agent-supplied detail, bounded; it is display data, never trusted."""
    raw = json.dumps(detail, default=str)
    return detail if len(raw) <= MAX_DETAIL_BYTES else {"truncated": True, "bytes": len(raw)}


def _aware(t: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as the UTC they were stored as."""
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _range(db: Session, range_id: str, user: CurrentUser) -> Range:
    try:
        return get_owned(db, Range, range_id, user, not_found="range not found")
    except ValueError as exc:  # not a UUID
        raise HTTPException(404, "range not found") from exc


def _profile(db: Session, rng: Range, *, create: bool = False) -> NoiseProfile | None:
    prof = (
        db.query(NoiseProfile).filter(NoiseProfile.range_id == rng.id, NoiseProfile.tenant_id == rng.tenant_id).first()
    )
    if prof is None and create:
        prof = NoiseProfile(range_id=rng.id, tenant_id=rng.tenant_id, overrides={}, targets={})
        db.add(prof)
        db.flush()
    return prof


def _require_profile(db: Session, rng: Range) -> NoiseProfile:
    prof = _profile(db, rng)
    if prof is None:
        raise HTTPException(404, "noise is not configured for this range")
    return prof


def _profile_out(prof: NoiseProfile | None, rng: Range) -> dict:
    if prof is None:
        prof = NoiseProfile(range_id=rng.id, enabled=False, paused=False, level=40, seed=1, utc_offset=0)
        configured = False
    else:
        configured = True
    effective = dial.resolve_level(prof.level, prof.overrides, paused=prof.paused, enabled=prof.enabled)
    s = dial.settings_for(effective)
    return {
        "range_id": str(rng.id),
        "configured": configured,
        "enabled": bool(prof.enabled),
        "paused": bool(prof.paused),
        "level": prof.level,
        "effective_level": effective,
        "seed": prof.seed,
        "utc_offset": prof.utc_offset or 0,
        "overrides": prof.overrides or {},
        "targets": prof.targets or {},
        "pack": prof.pack or "builtin",
        "dial": {
            "active_fraction": s.active_fraction,
            "actions_per_hour": s.actions_per_hour,
            "diurnal_amplitude": s.diurnal_amplitude,
            "lookalike_share": s.lookalike_share,
        },
    }


def _agent_state(agent: NoiseAgent, now: datetime) -> str:
    seen = _aware(agent.last_seen_at)
    if seen is None:
        return "pending"
    return "ok" if now - seen <= LOST_AFTER else "lost"


def _agent_out(agent: NoiseAgent, now: datetime) -> dict:
    seen = _aware(agent.last_seen_at)
    return {
        "id": str(agent.id),
        "node": agent.node,
        "zone": agent.zone,
        "version": agent.agent_version,
        "state": _agent_state(agent, now),
        "last_seen_at": seen.isoformat() if seen else None,
    }


def _persona_out(p: NoisePersona) -> dict:
    return {
        "id": str(p.id),
        "handle": p.handle,
        "display_name": p.display_name,
        "title": p.title,
        "department": p.department,
        "node": p.node,
        "work_start": p.work_start,
        "work_end": p.work_end,
        "habits": p.habits or {},
        "lookalikes": p.lookalikes,
        "attrs": p.attrs or {},
    }


def _specs(personas: list[NoisePersona]) -> list[planner.PersonaSpec]:
    return [
        planner.PersonaSpec(
            handle=p.handle,
            node=p.node,
            work_start=p.work_start,
            work_end=p.work_end,
            habits={k: float(v) for k, v in (p.habits or {}).items()},
            lookalikes=bool(p.lookalikes),
            contacts=tuple((p.attrs or {}).get("contacts", ())),
        )
        for p in personas
    ]


def _plan_for(db: Session, prof: NoiseProfile, node: str, zone: str, start: datetime, minutes: int) -> tuple[int, list]:
    level = dial.resolve_level(
        prof.level, prof.overrides, node=node, zone=zone, paused=prof.paused, enabled=prof.enabled
    )
    personas = db.query(NoisePersona).filter(NoisePersona.profile_id == prof.id, NoisePersona.node == node).all()
    ctx = planner.PlanContext(seed=prof.seed, level=level, utc_offset=prof.utc_offset or 0, targets=prof.targets or {})
    return level, planner.plan(ctx, _specs(personas), start, minutes)


# ── White cell ─────────────────────────────────────────────────────────
@router.get("/presets")
def presets(_user: CurrentUser = Depends(_read)) -> dict:
    """The named dial positions and the activity vocabulary."""
    return {
        "presets": dial.PRESETS,
        "activities": sorted(dial.BASE_MIX),
        "lookalikes": sorted(dial.LOOKALIKES),
        "target_pools": sorted(set(planner.TARGET_POOL.values())),
    }


@router.get("/ranges/{range_id}")
def get_profile(range_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(_read)) -> dict:
    rng = _range(db, range_id, user)
    return _profile_out(_profile(db, rng), rng)


@router.put("/ranges/{range_id}")
def put_profile(
    range_id: str, body: ProfileIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_control)
) -> dict:
    rng = _range(db, range_id, user)
    prof = _profile(db, rng, create=True)
    if body.preset is not None:
        if body.preset not in dial.PRESETS:
            raise HTTPException(422, f"unknown preset {body.preset!r}; one of {sorted(dial.PRESETS)}")
        prof.level = dial.PRESETS[body.preset]
    if body.level is not None:
        prof.level = body.level
    for name in ("enabled", "paused", "seed", "utc_offset"):
        val = getattr(body, name)
        if val is not None:
            setattr(prof, name, val)
    if body.overrides is not None:
        bad = [k for k in body.overrides if not k.startswith(("node:", "zone:")) or k.endswith(":") or len(k) > 260]
        if bad:
            raise HTTPException(422, f"override keys must be 'node:<name>' or 'zone:<name>': {bad}")
        prof.overrides = {k: dial.clamp_level(v) for k, v in body.overrides.items()}
    if body.targets is not None:
        errors = targets.validate(body.targets, set(planner.TARGET_POOL.values()))
        if errors:
            raise HTTPException(422, {"targets": errors})
        prof.targets = {k: list(dict.fromkeys(v)) for k, v in body.targets.items()}
    _audit(db, user, "noise_profile_update", rng.id, body.model_dump_json(exclude_none=True)[:1000])
    db.commit()
    logger.info("noise profile range=%s level=%s enabled=%s paused=%s", rng.id, prof.level, prof.enabled, prof.paused)
    return _profile_out(prof, rng)


@router.get("/ranges/{range_id}/agents")
def list_agents(range_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(_read)) -> list[dict]:
    rng = _range(db, range_id, user)
    prof = _profile(db, rng)
    if prof is None:
        return []
    now = _now()
    agents = (
        db.query(NoiseAgent)
        .filter(NoiseAgent.profile_id == prof.id, NoiseAgent.revoked_at.is_(None))
        .order_by(NoiseAgent.node)
        .all()
    )
    return [_agent_out(a, now) for a in agents]


def _issue_tokens(
    db: Session, prof: NoiseProfile, rng: Range, items: list[tuple[str, str]]
) -> list[tuple[NoiseAgent, str]]:
    """(Re-)key an agent per (node, zone). Old tokens for those nodes stop working."""
    out = []
    for node, zone in items:
        token = secrets.token_urlsafe(32)
        agent = db.query(NoiseAgent).filter(NoiseAgent.profile_id == prof.id, NoiseAgent.node == node).first()
        if agent is None:
            agent = NoiseAgent(profile_id=prof.id, tenant_id=rng.tenant_id, node=node)
            db.add(agent)
        agent.zone = zone
        agent.token_hash = _hash(token)
        agent.last_seen_at = None
        agent.revoked_at = None
        db.flush()
        out.append((agent, token))
    return out


@router.post("/ranges/{range_id}/agents", status_code=201)
def register_agents(
    range_id: str, body: AgentsIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_control)
) -> list[dict]:
    """Register (or re-key) agents. Each token is returned once and never again.

    Re-registering an existing node issues a fresh token and invalidates the old one,
    which is also how a compromised token is rotated.
    """
    rng = _range(db, range_id, user)
    nodes = [a.node for a in body.agents]
    if dupes := sorted({n for n in nodes if nodes.count(n) > 1}):
        raise HTTPException(422, f"node(s) listed more than once: {dupes}")
    prof = _profile(db, rng, create=True)
    now = _now()
    issued = _issue_tokens(db, prof, rng, [(a.node, a.zone) for a in body.agents])
    out = [{**_agent_out(agent, now), "token": token} for agent, token in issued]
    _audit(db, user, "noise_agents_register", rng.id, ",".join(nodes)[:1000])
    db.commit()
    return out


@router.delete("/ranges/{range_id}/agents/{agent_id}", status_code=204, response_class=Response)
def revoke_agent(range_id: str, agent_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(_control)):
    """Revoke an agent's token. Its ground truth is kept for the AAR."""
    rng = _range(db, range_id, user)
    prof = _require_profile(db, rng)
    try:
        aid = uuid.UUID(agent_id)
    except ValueError as exc:
        raise HTTPException(404, "agent not found") from exc
    agent = (
        db.query(NoiseAgent)
        .filter(
            NoiseAgent.id == aid,
            NoiseAgent.profile_id == prof.id,
            NoiseAgent.tenant_id == rng.tenant_id,
            NoiseAgent.revoked_at.is_(None),
        )
        .first()
    )
    if agent is None:
        raise HTTPException(404, "agent not found")
    agent.revoked_at = _now()
    agent.token_hash = f"revoked-{uuid.uuid4().hex}"  # unique, and no token hashes to it
    _audit(db, user, "noise_agent_revoke", rng.id, agent.node)
    db.commit()


@router.get("/ranges/{range_id}/personas")
def list_personas(range_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(_read)) -> list[dict]:
    rng = _range(db, range_id, user)
    prof = _profile(db, rng)
    if prof is None:
        return []
    rows = db.query(NoisePersona).filter(NoisePersona.profile_id == prof.id).order_by(NoisePersona.handle).all()
    return [_persona_out(p) for p in rows]


@router.post("/ranges/{range_id}/personas/roster", status_code=201)
def generate_roster(
    range_id: str, body: RosterIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_control)
) -> list[dict]:
    """Fill the range with the built-in roster, spread over the registered agents' nodes."""
    rng = _range(db, range_id, user)
    prof = _profile(db, rng, create=True)
    live = db.query(NoiseAgent).filter(NoiseAgent.profile_id == prof.id, NoiseAgent.revoked_at.is_(None))
    nodes = [a.node for a in live.order_by(NoiseAgent.node)]
    if not nodes:
        raise HTTPException(409, "register at least one agent first; personas live on agent nodes")
    if body.replace:
        db.query(NoisePersona).filter(NoisePersona.profile_id == prof.id).delete()
    elif db.query(NoisePersona).filter(NoisePersona.profile_id == prof.id).count():
        raise HTTPException(409, "this range already has personas; pass replace=true to regenerate")
    rows = []
    for p in builtin_roster(body.count, nodes, seed=prof.seed, domain=body.domain):
        row = NoisePersona(profile_id=prof.id, tenant_id=rng.tenant_id, **p)
        db.add(row)
        rows.append(row)
    _audit(db, user, "noise_roster_generate", rng.id, f"count={body.count} replace={body.replace}")
    db.commit()
    return [_persona_out(p) for p in rows]


@router.get("/ranges/{range_id}/plan")
def preview_plan(
    range_id: str,
    node: str = Query(..., min_length=1),
    minutes: int = Query(15, ge=1, le=planner.MAX_PLAN_MINUTES),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_read),
) -> dict:
    """What ``node``'s agent will be told to do next — the same plan the agent gets."""
    rng = _range(db, range_id, user)
    prof = _require_profile(db, rng)
    agent = db.query(NoiseAgent).filter(NoiseAgent.profile_id == prof.id, NoiseAgent.node == node).first()
    level, actions = _plan_for(db, prof, node, agent.zone if agent else "", _now(), minutes)
    return {"node": node, "level": level, "actions": actions}


@router.get("/ranges/{range_id}/activity")
def list_activity(
    range_id: str,
    limit: int = Query(100, ge=1, le=1000),
    lookalike: bool | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_read),
) -> list[dict]:
    """Ground truth, newest first. Which events on the wire were synthetic."""
    rng = _range(db, range_id, user)
    prof = _profile(db, rng)
    if prof is None:
        return []
    q = db.query(NoiseActivity, NoiseAgent.node).join(NoiseAgent, NoiseAgent.id == NoiseActivity.agent_id)
    q = q.filter(NoiseActivity.profile_id == prof.id)
    if lookalike is not None:
        q = q.filter(NoiseActivity.lookalike.is_(lookalike))
    rows = q.order_by(NoiseActivity.at.desc()).limit(limit).all()
    return [
        {
            "at": _aware(a.at).isoformat(),
            "node": node,
            "persona": a.persona,
            "kind": a.kind,
            "target": a.target,
            "lookalike": a.lookalike,
            "ok": a.ok,
            "detail": a.detail or {},
        }
        for a, node in rows
    ]


@router.get("/ranges/{range_id}/stats")
def activity_stats(
    range_id: str,
    minutes: int = Query(60, ge=1, le=24 * 60),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_read),
) -> dict:
    rng = _range(db, range_id, user)
    prof = _profile(db, rng)
    out: dict = {"minutes": minutes, "total": 0, "failed": 0, "lookalikes": 0, "by_kind": {}, "agents": {}}
    if prof is None:
        return out
    now = _now()
    since = now - timedelta(minutes=minutes)
    rows = (
        db.query(NoiseActivity.kind, NoiseActivity.ok, NoiseActivity.lookalike)
        .filter(NoiseActivity.profile_id == prof.id, NoiseActivity.at >= since)
        .all()
    )
    for kind, ok, look in rows:
        out["total"] += 1
        out["failed"] += 0 if ok else 1
        out["lookalikes"] += 1 if look else 0
        out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
    for a in db.query(NoiseAgent).filter(NoiseAgent.profile_id == prof.id, NoiseAgent.revoked_at.is_(None)):
        state = _agent_state(a, now)
        out["agents"][state] = out["agents"].get(state, 0) + 1
    return out


def _template_dict(rng: Range) -> dict:
    import yaml

    raw = rng.template.yaml if rng.template else ""
    try:
        loaded = yaml.safe_load(raw or "") or {}
    except yaml.YAMLError as exc:
        raise HTTPException(422, f"range template is not valid YAML: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def _dispatch(task_name: str, *args) -> str | None:
    from ..celery_client import dispatch

    return dispatch(task_name, *args)


@router.post("/ranges/{range_id}/deploy")
def deploy(range_id: str, body: DeployIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_control)) -> dict:
    """Put noise on a provisioned range, from its template's ``noise:`` block.

    Derives the target pools from the template's servers, registers an agent on every
    agent node (fresh tokens), creates the built-in roster if there is none, and hands
    the worker an Ansible run that installs the agent over the management network.
    ``dry_run`` shows all of that without changing anything.

    Windows nodes are listed under ``skipped`` until the Windows agent exists.
    """
    rng = _range(db, range_id, user)
    template = _template_dict(rng)
    block = topology.noise_block(template)
    if not block.get("enabled"):
        raise HTTPException(409, "the range template has no `noise:` block with `enabled: true`")
    nodes = topology.agent_nodes(template)
    linux = [n for n in nodes if n["platform"] == "linux"]
    skipped = [
        {"node": n["node"], "reason": f"{n['platform']} agent not available yet"} for n in nodes if n not in linux
    ]
    derived = topology.derive_targets(template)
    pools: dict[str, list[str]] = {}
    dropped: list[str] = []
    for pool, values in derived.items():
        for v in values[: targets.MAX_TARGETS_PER_POOL]:
            if why := targets.problem(pool, v):
                dropped.append(f"{pool}: {v}: {why}")
            else:
                pools.setdefault(pool, []).append(v)
    mgmt = {**topology.DEFAULT_MGMT, **(block.get("mgmt") or {})}
    controller_url = str(mgmt.get("controller_url") or os.getenv("NOISE_CONTROLLER_URL", ""))
    summary = {
        "dry_run": body.dry_run,
        "agents": [{k: n[k] for k in ("node", "zone", "platform", "ip", "mgmt_ip")} for n in linux],
        "skipped": skipped,
        "targets": pools,
        "dropped_targets": dropped,
        "controller_url": controller_url,
        "mgmt_cidr": str(mgmt["cidr"]),
    }
    if body.dry_run:
        return summary
    if rng.state not in (RangeState.ready, RangeState.running):
        raise HTTPException(409, f"range is {rng.state.value}; deploy noise once it is ready")
    if not linux:
        raise HTTPException(409, "the template has no Linux agent nodes (workstation/usersim/traffic_generator)")
    if not controller_url.startswith("https://"):
        raise HTTPException(
            422,
            "set noise.mgmt.controller_url in the template (or NOISE_CONTROLLER_URL) to the API's https URL "
            "as reached from the management network",
        )

    # Everything below is undone if the hand-off fails, so old tokens stay valid.
    savepoint = db.begin_nested()
    prof = _profile(db, rng, create=True)
    if not prof.enabled:  # first deploy: take the template's starting point
        if block.get("preset") in dial.PRESETS:
            prof.level = dial.PRESETS[block["preset"]]
        elif isinstance(block.get("level"), int):
            prof.level = dial.clamp_level(block["level"])
        if isinstance(block.get("seed"), int):
            prof.seed = block["seed"]
        prof.enabled = True
    if body.refresh_targets or not prof.targets:
        prof.targets = pools
    issued = _issue_tokens(db, prof, rng, [(n["node"], n["zone"]) for n in linux])
    if not db.query(NoisePersona).filter(NoisePersona.profile_id == prof.id).count():
        count = int(block.get("personas") or 3 * len(linux))
        domain = str(block.get("domain") or "corp.local")
        for p in builtin_roster(count, [n["node"] for n in linux], seed=prof.seed, domain=domain):
            db.add(NoisePersona(profile_id=prof.id, tenant_id=rng.tenant_id, **p))
    tokens = {agent.node: token for agent, token in issued}
    inventory = {
        "range_id": str(rng.id),
        "controller_url": controller_url,
        "mgmt_cidr": str(mgmt["cidr"]),
        "domain": str(block.get("domain") or "corp.local"),
        "agents": [{"node": n["node"], "mgmt_ip": n["mgmt_ip"], "token": tokens[n["node"]]} for n in linux],
    }
    task_id = _dispatch("deploy_noise_agents", inventory)
    if task_id is None:
        savepoint.rollback()
        raise HTTPException(503, "the task queue is unavailable; nothing was changed")
    savepoint.commit()
    _audit(db, user, "noise_deploy", rng.id, f"agents={len(linux)} task={task_id}")
    db.commit()
    return {**summary, "task_id": task_id}


# ── Agent channel ──────────────────────────────────────────────────────
def _agent_cidrs() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = os.getenv("NOISE_AGENT_CIDRS", "")
    return [ipaddress.ip_network(c.strip(), strict=False) for c in raw.split(",") if c.strip()]


def noise_agent_identity(
    request: Request,
    x_noise_agent_token: str = Header(..., alias="X-Noise-Agent-Token"),
    db: Session = Depends(get_db),
) -> tuple[NoiseAgent, NoiseProfile]:
    """The calling agent and its profile, by token. 401 unless the token is live and its
    range still exists; 403 from outside NOISE_AGENT_CIDRS when that is set."""
    cidrs = _agent_cidrs()
    if cidrs:
        try:
            ip = ipaddress.ip_address(client_ip(request) or "")
        except ValueError:
            ip = None
        if ip is None or not any(ip in c for c in cidrs):
            raise HTTPException(403, "agent calls are accepted from the management network only")
    row = (
        db.query(NoiseAgent, NoiseProfile)
        .join(NoiseProfile, NoiseProfile.id == NoiseAgent.profile_id)
        .join(Range, Range.id == NoiseProfile.range_id)
        .filter(
            NoiseAgent.token_hash == _hash(x_noise_agent_token),
            NoiseAgent.revoked_at.is_(None),
            Range.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(401, "unknown agent token")
    return row[0], row[1]


@router.get("/agent/plan")
def agent_plan(
    minutes: int = Query(10, ge=1, le=planner.MAX_PLAN_MINUTES),
    ident: tuple[NoiseAgent, NoiseProfile] = Depends(noise_agent_identity),
    db: Session = Depends(get_db),
) -> dict:
    """The calling node's actions for the next ``minutes``. Doubles as the heartbeat.

    Each action is signed; ``lookalike`` is withheld (the agent has no use for it, and a
    stolen token should not learn which upcoming activity is cover).
    """
    agent, prof = ident
    now = _now()
    agent.last_seen_at = now
    db.commit()
    level, planned = _plan_for(db, prof, agent.node, agent.zone, now, minutes)
    actions = []
    for a in planned:
        a = {k: v for k, v in a.items() if k != "lookalike"}
        a["sig"] = _sign(prof.plan_key, agent.node, a)
        actions.append(a)
    return {"node": agent.node, "level": level, "poll_seconds": POLL_SECONDS, "actions": actions}


@router.post("/agent/report")
def agent_report(
    body: ReportIn,
    ident: tuple[NoiseAgent, NoiseProfile] = Depends(noise_agent_identity),
    db: Session = Depends(get_db),
) -> dict:
    """Record what the agent did. Only signed, planned actions are accepted; each is
    recorded once. ``lookalike`` is derived from the kind, never taken from the agent."""
    agent, prof = ident
    verified: dict[tuple, tuple[ActivityReport, datetime]] = {}
    rejected = 0
    for r in body.results:
        try:
            at = datetime.fromisoformat(r.at)
        except ValueError:
            rejected += 1
            continue
        good = hmac.compare_digest(_sign(prof.plan_key, agent.node, r.model_dump()), r.sig)
        if not good or r.kind not in dial.ACTIVITY_KINDS:
            rejected += 1
            continue
        at = _aware(at).astimezone(UTC)
        verified.setdefault((at, r.persona, r.kind), (r, at))
    duplicate = len(body.results) - rejected - len(verified)
    if verified:
        seen = {
            (_aware(a).astimezone(UTC), p, k)
            for a, p, k in db.query(NoiseActivity.at, NoiseActivity.persona, NoiseActivity.kind).filter(
                NoiseActivity.agent_id == agent.id,
                NoiseActivity.at >= min(k[0] for k in verified) - timedelta(seconds=1),
                NoiseActivity.at <= max(k[0] for k in verified) + timedelta(seconds=1),
            )
        }
        for key, (r, at) in verified.items():
            if key in seen:
                duplicate += 1
                continue
            detail = dict(_small(r.detail))
            if r.ran_at:
                detail["ran_at"] = r.ran_at.isoformat()
            db.add(
                NoiseActivity(
                    profile_id=agent.profile_id,
                    agent_id=agent.id,
                    tenant_id=agent.tenant_id,
                    at=at,
                    persona=r.persona,
                    kind=r.kind,
                    target=r.target,
                    lookalike=r.kind in dial.LOOKALIKES,
                    ok=r.ok,
                    detail=detail,
                )
            )
    if rejected:
        logger.warning("noise agent %s (%s) sent %d unsigned/forged result(s)", agent.id, agent.node, rejected)
    agent.last_seen_at = _now()
    if body.version:
        agent.agent_version = body.version
    db.commit()
    return {"accepted": len(body.results) - rejected - duplicate, "rejected": rejected, "duplicate": duplicate}
