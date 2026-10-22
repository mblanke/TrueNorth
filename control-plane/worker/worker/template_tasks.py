"""Pruning of host-local golden-template copies (ADR 0012 decision 6, ADR 0013 §2).

``infra/vsphere/packer/build.sh replicate`` puts a copy of each build on every range host
as ``<id>@<host>-<yyyymmdd>``; the vSphere provisioner clones the promoted build's copy on
the VM's host and records it as ``vms[].template`` in the range's provisioner_output. This
task removes copies of an image **with a promoted build** that no build record still
needs: never a promoted build's copy, never a copy of a build registered since that
promotion (awaiting its own), never a copy a range that is not destroyed records, and
never anything of an image with no promoted build. There is no "newest copy" rule.

Contract:

* **Dry run by default.** ``prune_template_copies()`` only reports. Deleting needs
  ``dry_run=False`` (an operator: ``celery call worker.template_tasks.prune_template_copies
  --kwargs '{"dry_run": false}'``), or beat with ``TEMPLATE_COPY_PRUNE=delete``.
* **Never races a build.** While any range is ``provisioning`` a deleting run is downgraded
  to a dry run (``deferred``): a build may have pinned a build that was since replaced.
* **Build records decide** (``template_copies.PrunePolicy``): the names a promoted build
  (and any newer build) records are kept, and so is any copy stamped as one of those
  builds whatever its name.
* **Conservative "in use".** The names recorded by every undestroyed range, on any
  vCenter, are kept. A range whose output cannot be read defers deleting, as a build does.
* **Every vCenter.** Each active vSphere hypervisor connection, and the worker's own
  VSPHERE_* environment when ``VSPHERE_URL`` is set, deduplicated by endpoint. One that
  fails is reported; the others still run.
* **Unsupported is explicit.** A backend without ``prune_template_copies`` is reported
  ``unsupported``, never treated as success.

Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from . import db_ops
from .base_tasks import ReliableTask
from .celery_app import app
from .provisioners import CREDENTIALS_HYPERVISOR, get_provisioner
from .provisioners.template_copies import PrunePolicy, names_in_use, parse
from .task_plumbing import db_session as _db_session

logger = logging.getLogger(__name__)

BACKEND = "vsphere_api"


def _output(raw) -> dict | None:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _provisioners(connections: list[dict]) -> list[tuple[str, object]]:
    """(label, provisioner) per distinct endpoint: the environment's, then each connection's."""
    out: list[tuple[str, object]] = []
    seen: set[str] = set()
    sources = ([{}] if os.getenv("VSPHERE_URL", "").strip() else []) + list(connections)
    for creds in sources:
        label = creds.get("host") or "VSPHERE_URL"
        try:
            provisioner = get_provisioner(BACKEND, creds or None, teardown=True)
        except Exception as exc:  # noqa: BLE001 -- reported for this endpoint only
            out.append((label, exc))
            continue
        record = getattr(provisioner, "connection_record", None)
        rec = record() if callable(record) else None
        key = json.dumps(rec if isinstance(rec, dict) else label, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(((rec or {}).get("host") or label, provisioner))
    return out


def _copies(raw) -> dict:
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def prune_policy(build_rows: list[dict], in_use: set[str]) -> PrunePolicy:
    """What pruning keeps, from every registered build (db_ops.golden_image_build_copies).

    Per image with a promoted build: its templates (the image's and each build's, and the
    bases of their copy names) become ``managed``; the promoted build and every build
    registered after it are protected, by copy name and by stamp. Images with no promoted
    build contribute nothing, so none of their copies is ever a candidate."""
    by_image: dict = {}
    for row in build_rows:
        by_image.setdefault(row["image_id"], []).append(row)
    managed: set[str] = set()
    protected: set[str] = set(in_use)
    builds: set[tuple[str, str]] = set()
    for rows in by_image.values():
        promoted = [r for r in rows if r["promoted"]]
        if not promoted:
            continue
        since = promoted[0]["created_at"]
        for r in rows:
            names = [str(n) for n in _copies(r["copies"]).values()]
            managed.update(n for n in (r["image_template"], r["catalogue_id"], r["build_template"]) if n)
            managed.update(c.base for n in names if (c := parse(n)) is not None)
            if r["promoted"] or since is None or r["created_at"] is None or r["created_at"] >= since:
                protected.update(names)
                builds.add((r["catalogue_id"], r["build_id"], r["checksum"]))
    return PrunePolicy(protected=frozenset(protected), managed=frozenset(managed), builds=frozenset(builds))


def run_prune(*, dry_run: bool = True) -> dict:
    """The work of ``prune_template_copies`` (no Celery: callable from an admin action)."""
    with _db_session() as db:
        rows = db_ops.undestroyed_range_outputs(db)
        build_rows = db_ops.golden_image_build_copies(db)
        connections = db_ops.active_connection_creds(db, CREDENTIALS_HYPERVISOR[BACKEND])
    outputs, unreadable, building = [], [], []
    for range_id, state, raw in rows:
        out = _output(raw)
        if out is None:
            unreadable.append(str(range_id))
        else:
            outputs.append(out)
        if str(getattr(state, "value", state)) == "provisioning":
            building.append(str(range_id))
    in_use = names_in_use(outputs)
    policy = prune_policy(build_rows, in_use)
    result: dict = {"requested_dry_run": dry_run, "in_use": sorted(in_use), "endpoints": {},
                    "protected": sorted(policy.protected), "managed": sorted(policy.managed)}
    if not dry_run and (building or unreadable):
        dry_run = True
        why = [f"{len(building)} range(s) provisioning"] if building else []
        why += [f"{len(unreadable)} range output(s) unreadable"] if unreadable else []
        result["deferred"] = "; ".join(why) + "; nothing deleted this run"
    if unreadable:
        result["unreadable_outputs"] = unreadable
    result["dry_run"] = dry_run
    for label, provisioner in _provisioners(connections):
        if isinstance(provisioner, Exception):
            result["endpoints"][label] = {"error": str(provisioner)}
            continue
        prune = getattr(provisioner, "prune_template_copies", None)
        if not callable(prune):
            result["endpoints"][label] = {"unsupported": True}
            continue
        try:
            got = asyncio.run(prune(policy, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 -- the other vCenters still run
            logger.error("[templates] pruning copies on %s failed: %s", label, exc)
            result["endpoints"][label] = {"error": str(exc)}
            continue
        result["endpoints"][label] = got
        logger.info("[templates] %s: %d stale copies%s, %d kept (in use), %d deleted, %d errors", label,
                    len(got.get("deletable", [])), " (dry run)" if dry_run else "", len(got.get("kept_in_use", [])),
                    len(got.get("deleted", [])), len(got.get("errors", [])))
    result["status"] = "partial" if any("error" in e or e.get("errors") for e in result["endpoints"].values()) \
        else "ok"
    return result


@app.task(base=ReliableTask, bind=True, name="worker.template_tasks.prune_template_copies")
def prune_template_copies(self, dry_run: bool = True) -> dict:
    """Remove template copies no build record needs (ADR 0013); reports only unless ``dry_run=False``."""
    return run_prune(dry_run=dry_run)
