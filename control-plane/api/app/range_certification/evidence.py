"""Evidence TrueNorth already holds, turned into section entries (pure functions).

Each returns ``(status, summary, detail)`` for one section, from what the worker recorded
in the range's ``provisioner_output`` (worker/range_alloc.stored_output) and the API's
``network_reservations``. What a later slice adds (S1's external-collision checks, S2's
promoted build ids and checksums, S5's inventory-checked teardown) is not invented here:
until it is recorded the section says ``incomplete`` and why.
"""

from __future__ import annotations

import json

from .sections import FAIL, INCOMPLETE, PASS


def output_of(raw: str | None) -> dict:
    """A range's provisioner_output as a dict ({} when empty or unreadable)."""
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _vms(output: dict) -> list[dict]:
    return [v for v in output.get("vms") or [] if isinstance(v, dict)]


# A router appliance's build summary key (worker vsphere_api: its image may run no VMware
# Tools, so its address is not verified). The only exemption the worker records.
APPLIANCE_KEYS = ("pfsense", "opnsense", "vyos")


def guest_readiness(output: dict, build_error: str | None = None) -> tuple[str, str, dict]:
    """#232: every VM's planned guest addresses as VMware Tools reported them.

    ``fail``: the build failed (``build_error``: the provision operation's failure, whatever
    was built), or a VM is ``not_ready`` or its addresses were contradicted (``ip_verified``
    False). ``output`` must be this build's own (service.build_output drops what an earlier
    build left). ``incomplete``: a check
    still running, a VM left unverified that the worker did not exempt (``ip_verified``
    None on a non-appliance: DHCP, ``verify_ip: false``), or a backend that records no
    readiness. ``pass``: at least one VM verified and every other one an appliance.

    #255: every Windows VM's guest customization (Sysprep) outcome (``vms[].customization``
    .status: ok / failed / running / unverified; worker vsphere_customization). ``failed``
    fails the section; ``running`` (left to the reconcile task), ``unverified``, or a
    Windows VM with no outcome recorded keep it ``incomplete``: a failed Sysprep leaves the
    template's build login on the VM, so only ``ok`` is proof."""
    vms = _vms(output)
    rows = [{"name": v.get("name", ""), "ip_verified": v.get("ip_verified"), "tools_ready": v.get("tools_ready"),
             "planned_ip": v.get("planned_ip", ""), "guest_ips": v.get("guest_ips") or [],
             "not_ready": v.get("not_ready", ""), "ip_check": (v.get("ip_check") or {}).get("status", ""),
             "appliance": any(k in v for k in APPLIANCE_KEYS), "family": v.get("family", ""),
             "customization": _customization(v)}
            for v in vms]
    detail = {"vms": rows, "not_ready": list(output.get("not_ready") or [])}
    if build_error:
        return FAIL, f"the build failed: {build_error[:300]}", detail
    if not vms:
        return INCOMPLETE, "no built VMs recorded", detail
    bad = [r["name"] for r in rows if r["not_ready"] or r["ip_verified"] is False
           or (r["customization"] or {}).get("status") == "failed"] + [
        n for n in detail["not_ready"] if n not in {r["name"] for r in rows}]
    if bad:
        return FAIL, f"{len(bad)} VM(s) not ready: {', '.join(sorted(set(bad)))}", detail
    if not any("ip_verified" in v for v in vms):
        return INCOMPLETE, f"the {output.get('provider', 'unknown')} backend records no guest readiness", detail
    pending = [r["name"] for r in rows if r["ip_check"] == "running"]
    if pending:
        return INCOMPLETE, f"guest network check still running on {', '.join(sorted(pending))}", detail
    pending = [r["name"] for r in rows if (r["customization"] or {}).get("status") == "running"]
    if pending:
        return INCOMPLETE, f"guest customization (Sysprep) check still running on {', '.join(sorted(pending))}", \
            detail
    unverified = sorted(r["name"] for r in rows if r["ip_verified"] is not True and not r["appliance"])
    verified = sum(1 for r in rows if r["ip_verified"] is True)
    if unverified or not verified:
        names = ", ".join(unverified) or "every VM is an appliance"
        return INCOMPLETE, f"guest network not verified by VMware Tools: {names}", detail
    windows = [r for r in rows if r["family"] == "windows"]
    unproven = sorted(r["name"] for r in windows if (r["customization"] or {}).get("status") != "ok")
    if unproven:
        return INCOMPLETE, f"guest customization (Sysprep) success not recorded on {', '.join(unproven)}", detail
    sysprep = f"; Sysprep succeeded on {len(windows)} Windows VM(s)" if windows else ""
    return PASS, f"{verified} of {len(rows)} VM(s) verified by VMware Tools; the rest router appliances{sysprep}", \
        detail


def _customization(vm: dict) -> dict | None:
    """A VM's recorded guest customization outcome (#255), or None when none was recorded."""
    rec = vm.get("customization")
    if not isinstance(rec, dict):
        return None
    return {k: rec.get(k) for k in ("status", "vsphere_status", "source", "detail") if rec.get(k) is not None}


def template_provenance(output: dict, builds: dict[str, dict[str, dict]]) -> tuple[str, str, dict]:
    """S2 (#240): the promoted build each VM was cloned from (``vms[].template_build``:
    catalogue_id, build_id, checksum, promoted), checked against the registered builds
    (``builds``: catalogue_id -> build_id -> {checksum, promoted}; golden_image_builds).

    ``fail``: a VM's build is not registered, its checksum differs from the registered
    one, or VMs of one image run different builds. ``incomplete``: a VM with no build record
    (another backend), or an unpromoted image the transition flag let through. ``pass``:
    every VM on a registered promoted build, one build per image."""
    rows, problems, gaps = [], [], []
    for v in _vms(output):
        tb = v.get("template_build") if isinstance(v.get("template_build"), dict) else None
        name = v.get("name", "")
        row = {"vm": name, "template": v.get("template", ""), "catalogue_id": (tb or {}).get("catalogue_id"),
               "build_id": (tb or {}).get("build_id"), "checksum": (tb or {}).get("checksum"),
               "promoted": (tb or {}).get("promoted"), "registered": None, "promoted_now": None}
        rows.append(row)
        if tb is None:
            gaps.append(f"{name}: no build record")
            continue
        if not tb.get("promoted"):
            gaps.append(f"{name}: unpromoted image allowed by {tb.get('allowed_by') or 'a transition flag'}")
            continue
        known = builds.get(str(tb.get("catalogue_id")), {}).get(str(tb.get("build_id")))
        row["registered"] = known is not None
        if known is None:
            problems.append(f"{name}: build {tb.get('build_id')} of {tb.get('catalogue_id')} is not registered")
            continue
        row["promoted_now"] = bool(known.get("promoted"))
        if str(known.get("checksum") or "") != str(tb.get("checksum") or ""):
            problems.append(f"{name}: checksum differs from registered build {tb.get('build_id')}")
    per_image: dict[str, set] = {}
    for r in rows:
        if r["catalogue_id"] and r["build_id"]:
            per_image.setdefault(r["catalogue_id"], set()).add(r["build_id"])
    problems += [f"{img}: VMs run different builds ({', '.join(sorted(ids))})"
                 for img, ids in sorted(per_image.items()) if len(ids) > 1]
    detail = {"vms": rows}
    if not rows:
        return INCOMPLETE, "no built VMs recorded", detail
    if problems:
        return FAIL, "pinned build mismatch: " + "; ".join(problems), detail
    if gaps:
        return INCOMPLETE, "; ".join(gaps), detail
    pinned = ", ".join(f"{img} {next(iter(ids))}" for img, ids in sorted(per_image.items()))
    return PASS, f"{len(rows)} VM(s) on registered promoted builds: {pinned}", detail


LIVE_CHECKED_BACKENDS = ("vsphere_api",)  # S1: live vDS read before allocation, recheck before creation


def network_allocation(output: dict, reservations: list, external: list,
                       build_error: str | None = None) -> tuple[str, str, dict]:
    """S1 (#239): the VLANs, MACs and uplink addresses the range holds
    (``network_reservations``), the networks the worker built, and the external-collision
    evidence: VLANs used outside TrueNorth on the same switch (``external`` reservations of
    the same domains, refreshed by a live read before every allocation).

    ``fail``: a VLAN the range holds is also an external one (a collision; a cross-check
    only, since external and range reservations share one unique constraint). ``pass``: a
    vSphere build that finished, holding its VLANs, with no overlap. S1 records no per-build
    "check ran" row, but its checks are not optional on that backend: the live read happens
    before the VLANs are reserved and the port-group recheck stops the build on a collision,
    so a build that finished passed both. ``incomplete`` otherwise (a failed build, or a
    backend without the live checks, such as mock)."""
    held = sorted(({"kind": r.kind, "holder": r.holder, "value": r.value, "domain": r.domain} for r in reservations),
                  key=lambda r: (r["kind"], r["holder"]))
    networks = [{k: n.get(k) for k in ("name", "vlan", "vlan_id", "physical_vlan", "cidr", "portgroup", "switch_mode")
                 if k in n} for n in output.get("networks") or [] if isinstance(n, dict)]
    macs = {nic.get("mac") for v in _vms(output) for nic in v.get("nics") or [] if isinstance(nic, dict) and nic.get("mac")}
    vlan_domains = {r["domain"] for r in held if r["kind"] == "vlan"}
    outside = [r for r in external if r.kind == "vlan" and r.domain in vlan_domains]
    taken = {(r.domain, r.value): r.holder for r in outside}
    overlap = sorted(f"VLAN {r['value']} ({taken[(r['domain'], r['value'])]})" for r in held
                     if r["kind"] == "vlan" and (r["domain"], r["value"]) in taken)
    reads = [r.updated_at for r in outside if getattr(r, "updated_at", None)]
    provider = output.get("provider", "")
    detail = {"reservations": held, "networks": networks, "mac_count": len(macs), "provider": provider,
              "external_collision_check": {
                  "external_vlans_in_domain": sorted({f"{r.domain}:{r.value}" for r in outside}),
                  "last_live_read": max(reads).isoformat() if reads else None,
                  "overlap": overlap,
                  "basis": "S1 live switch read before allocation and port-group recheck (a collision stops the build)"
                  if provider in LIVE_CHECKED_BACKENDS else "no live checks on this backend"}}
    counts = {k: sum(1 for r in held if r["kind"] == k) for k in sorted({r["kind"] for r in held})}
    summary = ", ".join(f"{n} {k}" for k, n in counts.items()) or "no reservations"
    if overlap:
        return FAIL, f"VLAN collision with use outside TrueNorth: {', '.join(overlap)}", detail
    if provider not in LIVE_CHECKED_BACKENDS:
        return INCOMPLETE, f"{summary}; the {provider or 'unknown'} backend makes no live collision check", detail
    if build_error or not output:
        return INCOMPLETE, f"{summary}; the build did not finish, so its collision checks are not proven", detail
    if not vlan_domains:
        return INCOMPLETE, f"{summary}; the range holds no VLAN reservation", detail
    return PASS, f"{summary}; no collision with VLANs used outside TrueNorth (live checks, S1)", detail


TEMPLATE_LOGIN_OK = ("rejected", "not_checked")


def credentials(output: dict, accounts: list) -> tuple[str, str, dict]:
    """S3 (#241): the golden template's password must be refused on every VM
    (``vms[].template_login``: rejected / accepted / inconclusive / not_checked, the last
    for appliances), and the range's own accounts set: no change pending, every
    Student-visible account applied by this build (a rebuild gives Students new passwords,
    so applied means rotated). Scenario accounts keep their authored passwords by design.

    ``fail``: the template password still logs in on a VM. ``incomplete``: a check
    inconclusive or missing, a change pending, or a Student account not applied yet."""
    vms = _vms(output)
    logins = [{"vm": v.get("name", ""), **{k: (v.get("template_login") or {}).get(k)
                                            for k in ("status", "username", "detail")}} for v in vms]
    rows = [{"machine": a.machine, "username": a.username, "student_visible": bool(a.student_visible),
             "generated": bool(a.generated), "scenario": bool(a.scenario), "applied": bool(a.applied),
             "pending": a.sealed_previous is not None or bool(a.pending_operation),
             "rotated_at": a.rotated_at.isoformat() if a.rotated_at else None} for a in accounts]
    detail = {"template_login": logins, "accounts": sorted(rows, key=lambda r: (r["machine"], r["username"]))}
    if not vms:
        return INCOMPLETE, "no built VMs recorded", detail
    accepted = sorted(r["vm"] for r in logins if r["status"] == "accepted")
    if accepted:
        return FAIL, f"the golden template's password still logs in on {', '.join(accepted)}", detail
    unproven = sorted(r["vm"] for r in logins if r["status"] not in TEMPLATE_LOGIN_OK)
    if unproven:
        return INCOMPLETE, f"template-password check not proven on {', '.join(unproven)}", detail
    if not any(r["status"] == "rejected" for r in logins):
        return INCOMPLETE, "no VM's template-password check was made (appliances only)", detail
    pending = sorted(f"{r['machine']}/{r['username']}" for r in rows if r["pending"])
    if pending:
        return INCOMPLETE, f"a credential change is still pending on {', '.join(pending)}", detail
    unset = sorted(f"{r['machine']}/{r['username']}" for r in rows
                   if r["student_visible"] and not r["scenario"] and not r["applied"])
    if unset:
        return INCOMPLETE, f"Student account(s) not set by this build yet: {', '.join(unset)}", detail
    students = sum(1 for r in rows if r["student_visible"] and not r["scenario"])
    return PASS, (f"template password refused on {sum(1 for r in logins if r['status'] == 'rejected')} VM(s); "
                  f"{students} Student account(s) set with this build's passwords"), detail


TEARDOWN_LISTS = ("deleted", "remaining", "unidentified", "not_checked", "confirmed_deleted", "confirm_skipped", "gaps")


def teardown(destroyed: bool, error: str | None, still_held: list,
             check: dict | None = None) -> tuple[str, str, dict]:
    """A destroy's outcome, with the worker's verification of it (S5).

    ``check``: the range's ``teardown`` check (app/range_inventory, written by the worker
    from the backend's report): ``{"status", "summary", "report"}``, the report listing what
    was deleted by recorded id, which recorded ids remain (VMs, disk files, port groups, the
    folder), what carries the range's mark but was not deleted (unidentified), and what
    could not be checked. Its grade is the section's: ``pass`` only when zero recorded ids
    remain and nothing is unidentified; ``fail`` when a recorded id remains. None: the
    backend does not verify its teardown (mock, Proxmox): ``incomplete``, saying so.
    ``fail`` too when the destroy itself failed. ``still_held``: reservations the worker had
    not released by then (the API releases them next)."""
    leftovers = [{"kind": r.kind, "holder": r.holder, "value": r.value} for r in still_held]
    report = (check or {}).get("report") if isinstance((check or {}).get("report"), dict) else {}
    detail = {"destroyed": destroyed, "error": error, "reservations_left": leftovers,
              "inventory_verified": None if check is None else check.get("status") == PASS,
              "inventory": None if check is None else {
                  "status": check.get("status"), "legacy": bool(report.get("legacy")),
                  "verified_at": report.get("verified_at"),
                  **{k: report.get(k) or [] for k in TEARDOWN_LISTS}}}
    if not destroyed:
        left = len(report.get("remaining") or [])
        return FAIL, (f"destroy failed: {(error or 'no reason recorded')[:300]}"
                      + (f"; {left} recorded item(s) remain" if left else "")), detail
    extra = f"; {len(leftovers)} reservation(s) still held, released by the API" if leftovers else ""
    if check is None:
        return INCOMPLETE, f"recorded destroyed{extra}; the backend did not verify the teardown against a recorded " \
                           "inventory", detail
    status = check.get("status") if check.get("status") in (PASS, FAIL, INCOMPLETE) else INCOMPLETE
    return status, f"recorded destroyed; {check.get('summary') or 'no summary'}{extra}", detail


# Range certification S4 (#242): the per-zone isolation evidence the worker probed after
# the build was ready (worker/range_isolation.py, provisioner_output ``isolation``).
ISOLATION_RESULTS = ("proven_unreachable", "reachable", "inconclusive", "connect_failed_only")
_ZONE_KEYS = ("zone", "cidr", "gateway", "status", "probe", "selftest", "ipv6", "verdict", "reasons", "step_errors")
_CHECK_KEYS = ("check", "target", "expect", "result", "passed", "note")


def _zone_gap(z: dict) -> str | None:
    """Why a zone that leaked nothing still is not proven isolated, or None."""
    if z.get("status") != "probed":
        why = "; ".join(z.get("reasons") or [])
        return f"zone {z.get('zone')}: {z.get('status') or 'not probed'}" + (f" ({why})" if why else "")
    weak = [f"{c.get('check')} {c.get('target')} {c.get('result')}" for c in z["checks"]
            if (c.get("expect") == "unreachable" and c.get("result") != "proven_unreachable")
            or (c.get("expect") == "reachable" and c.get("result") != "reachable")]
    if weak or not z["checks"]:
        return f"zone {z.get('zone')}: " + (", ".join(weak) or "no checks recorded")
    return None


def isolation(output: dict) -> tuple[str, str, dict]:
    """S4 (#242): isolation evidence per security zone.

    Graded again here from each check's result class, never taken from the worker's
    verdict: ``fail`` when any check that must fail was ``reachable`` (a zone reaches
    something outside its permitted exceptions); ``pass`` only when every zone was probed,
    every check that must fail is ``proven_unreachable``, every permitted exception
    connected and site targets were configured; else ``incomplete``, naming the zones and
    checks that fall short (``connect_failed_only`` and ``inconclusive`` are never proof).
    The mode (warn / enforce) is recorded; it decides only whether the build was blocked,
    never this grade."""
    rec = output.get("isolation") if isinstance(output.get("isolation"), dict) else None
    if rec is None:
        return INCOMPLETE, "no isolation probes recorded for this build (S4 probes every zone once it is ready)", {}
    rows = [{**{k: z.get(k) for k in _ZONE_KEYS if k in z},
             "checks": [{k: c.get(k) for k in _CHECK_KEYS if k in c} for c in z.get("checks") or []
                        if isinstance(c, dict)]}
            for z in rec.get("zones") or [] if isinstance(z, dict)]
    settings = rec.get("settings") if isinstance(rec.get("settings"), dict) else None
    notes = [str(n) for n in rec.get("notes") or []]
    detail = {"mode": rec.get("mode"), "started_at": rec.get("started_at"), "ran_at": rec.get("ran_at"),
              "worker_verdict": rec.get("verdict"), "settings": settings, "zones": rows, "notes": notes}
    mode = f" ({rec.get('mode') or 'warn'} mode)"
    leaks = []
    for z in rows:
        bad = [f"{c.get('check')} {c.get('target')}" for c in z["checks"]
               if c.get("expect") == "unreachable" and c.get("result") == "reachable"]
        if bad:
            leaks.append(f"zone {z.get('zone')}: {', '.join(bad)}")
    if leaks:
        return FAIL, f"not isolated{mode}: reachable outside its exceptions: {'; '.join(leaks)}", detail
    gaps = [g for g in map(_zone_gap, rows) if g]
    if not rows:
        gaps.append("no range networks recorded")
    if settings is None:
        gaps.append("the probes did not run" + (f": {'; '.join(notes)}" if notes else ""))
    elif not settings.get("targets"):
        gaps.append("no site probe targets configured (tn_range_isolation_probe_targets)")
    if gaps:
        probed = sum(1 for z in rows if z.get("status") == "probed")
        return INCOMPLETE, f"isolation not proven{mode}, {probed} of {len(rows)} zone(s) probed: {'; '.join(gaps)}", \
            detail
    return PASS, (f"isolated{mode}: {len(rows)} zone(s) probed, every check that must fail proven unreachable, "
                  "permitted exceptions connected"), detail
